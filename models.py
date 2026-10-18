"""Shared data models for the Game Daily plugin.

This module deliberately has no AstrBot imports so it can be unit-tested
outside of an AstrBot runtime.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "yclid",
    "mc_cid",
    "mc_eid",
    "ref_src",
    "spm",
    "share_token",
    "from_source",
    "from_spmid",
    "vd_source",
}

_TRACKING_QUERY_PREFIXES = ("utm_", "pk_", "mtm_")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def normalize_url(url: str) -> str:
    """Normalize a URL for de-duplication.

    Tracking parameters and fragments are removed, but functional query
    parameters are preserved.  Invalid/non-HTTP URLs are returned in a lightly
    cleaned form instead of being dropped.
    """

    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return raw

    host = parts.netloc.lower()
    if host.endswith(":80") and parts.scheme == "http":
        host = host[:-3]
    elif host.endswith(":443") and parts.scheme == "https":
        host = host[:-4]

    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/") or "/"

    query: list[tuple[str, str]] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered in _TRACKING_QUERY_KEYS:
            continue
        if any(lowered.startswith(prefix) for prefix in _TRACKING_QUERY_PREFIXES):
            continue
        query.append((key, value))

    return urlunsplit(
        (parts.scheme.lower(), host, path, urlencode(query, doseq=True), "")
    )


def normalize_title(title: str) -> str:
    """Normalize a title for comparison (not for display)."""

    text = unicodedata.normalize("NFKC", title or "")
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
    text = text.lower()
    text = re.sub(r"[\W_]+", "", text, flags=re.UNICODE)
    return text


def normalize_content(title: str, summary: str) -> str:
    text = f"{normalize_title(title)}|{normalize_title(summary[:800])}"
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()


def content_fingerprint(title: str, summary: str, link: str) -> str:
    payload = "\n".join(
        (
            normalize_title(title),
            normalize_title(summary[:800]),
            normalize_url(link),
        )
    )
    return hashlib.sha1(payload.encode("utf-8", "ignore")).hexdigest()


@dataclass(slots=True)
class NewsItem:
    """A single collected news entry.

    ``published_at`` is timezone-aware UTC, or ``None`` when the source did not
    provide a timestamp.  A missing timestamp is never invented.
    """

    source_id: str
    source: str
    title: str
    link: str
    published_at: datetime | None
    summary: str
    category: str
    source_weight: float = 1.0
    collected_at: datetime = field(default_factory=utcnow)
    raw_title: str = ""
    published_missing: bool = False
    importance: float = 0.0
    ai_title: str = ""
    ai_summary: str = ""
    ai_used: bool = False
    merged_sources: list[str] = field(default_factory=list)
    merged_links: list[str] = field(default_factory=list)
    original_id: int = 0

    def __post_init__(self) -> None:
        if self.published_at is not None:
            self.published_at = ensure_aware(self.published_at)
        else:
            self.published_missing = True
        self.raw_title = self.raw_title or self.title
        self.link = self.link.strip()
        if not self.merged_sources:
            self.merged_sources = [self.source]

    @property
    def display_title(self) -> str:
        return self.ai_title.strip() or self.title

    @property
    def display_summary(self) -> str:
        return self.ai_summary.strip() or self.summary

    @property
    def display_source(self) -> str:
        seen: list[str] = []
        for name in self.merged_sources:
            if name and name not in seen:
                seen.append(name)
        return " / ".join(seen)

    @property
    def is_official(self) -> bool:
        return self.source_id in {
            "steam_api",
            "steam_store_news",
            "steam_new_releases",
            "steam_latest_releases",
            "xbox_wire",
            "playstation_blog",
        } or "官方" in self.source

    @property
    def published_text(self) -> str:
        if self.published_at is None:
            return "时间未标注"
        return self.published_at.astimezone().strftime("%m-%d %H:%M")

    @property
    def fingerprint(self) -> str:
        return content_fingerprint(self.raw_title, self.summary, self.link)

    @property
    def title_key(self) -> str:
        normalized = normalize_title(self.raw_title)
        # Very short generic English titles ("Update", "News") are too
        # ambiguous for cross-source title matching.  A short Chinese title
        # such as "停服公告" can still be specific, so allow >=4 CJK chars.
        has_cjk = re.search(r"[\u3400-\u9fff]", normalized) is not None
        minimum = 4 if has_cjk else 8
        if len(normalized) < minimum:
            return ""
        return f"title:{normalized}"

    @property
    def url_key(self) -> str:
        return f"url:{normalize_url(self.link)}"

    @property
    def content_key(self) -> str:
        return f"content:{normalize_content(self.raw_title, self.summary)}"

    def dedupe_keys(self) -> list[str]:
        keys = [self.title_key, self.content_key]
        if self.link:
            keys.append(self.url_key)
        return [key for key in keys if key]

    def merge_sources_from(self, other: "NewsItem") -> None:
        for name in other.merged_sources or [other.source]:
            if name and name not in self.merged_sources:
                self.merged_sources.append(name)
        if other.link and other.link not in self.merged_links:
            self.merged_links.append(other.link)
        if len(other.summary) > len(self.summary):
            self.summary = other.summary
        self.importance = max(self.importance, other.importance)

    def as_ai_payload(self, item_id: int, summary_limit: int = 700) -> dict:
        return {
            "id": item_id,
            "title": self.raw_title,
            "summary": self.summary[:summary_limit],
            "source": self.display_source,
            "published": (
                self.published_at.isoformat()
                if self.published_at is not None
                else None
            ),
            "link": self.link,
            "category": self.category,
            "official": self.is_official,
        }


@dataclass(slots=True)
class SourceReport:
    source_id: str
    name: str
    ok: bool
    item_count: int = 0
    error: str = ""
    warnings: list[str] = field(default_factory=list)

    def status_text(self) -> str:
        if self.ok:
            return "成功"
        return f"失败（{self.error or '未知错误'}）"


@dataclass(slots=True)
class ReportStats:
    total_collected: int = 0
    total_in_window: int = 0
    total_after_local_dedupe: int = 0
    total_after_sent_filter: int = 0
    total_ai_processed: int = 0
    total_selected: int = 0
    sources_total: int = 0
    sources_ok: int = 0
    sources_failed: int = 0
    ai_used: bool = False
    ai_candidates: int = 0

    def summary_text(self) -> str:
        return (
            f"采集 {self.total_collected} 条 · 时间窗内 {self.total_in_window} 条 · "
            f"去重后 {self.total_after_local_dedupe} 条 · 展示 {self.total_selected} 条"
        )


@dataclass(slots=True)
class ReportResult:
    status: str
    items: list[NewsItem]
    stats: ReportStats
    source_reports: list[SourceReport]
    started_at: datetime
    finished_at: datetime
    poster_path: str | None = None
    caption: str = ""
    errors: list[str] = field(default_factory=list)
    ai_used: bool = False
    sent_targets: list[str] = field(default_factory=list)
    failed_targets: list[dict] = field(default_factory=list)
    fatal_source_error: bool = False

    @property
    def ok(self) -> bool:
        return self.status in {"ok", "no_news"}

    @property
    def has_news(self) -> bool:
        return bool(self.items)
