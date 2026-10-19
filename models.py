"""Shared data models for the Game Daily plugin.

This module deliberately has no AstrBot imports so it can be unit-tested
outside of an AstrBot runtime.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass, field
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
    image_url: str = ""
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
class SteamDeal:
    app_id: int
    name: str
    discount_percent: int
    original_price_cents: int
    final_price_cents: int
    currency: str = "CNY"
    image_url: str = ""
    genres: list[str] = field(default_factory=list)

    @property
    def original_price_text(self) -> str:
        return _price_text(self.original_price_cents, self.currency)

    @property
    def final_price_text(self) -> str:
        return _price_text(self.final_price_cents, self.currency)

    @property
    def discount_text(self) -> str:
        return f"-{max(0, min(100, self.discount_percent))}%"

    @property
    def genres_text(self) -> str:
        values = [value for value in self.genres if value][:3]
        return " · ".join(values) if values else "Steam 折扣"


def _price_text(cents: int, currency: str) -> str:
    symbol = {"CNY": "¥", "USD": "$", "EUR": "€"}.get(currency, "")
    value = max(0, int(cents)) / 100.0
    if abs(value - round(value)) < 0.005:
        amount = f"{round(value):d}"
    else:
        amount = f"{value:.2f}"
    return f"{symbol}{amount}"


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
    deals: list[SteamDeal] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in {"ok", "no_news"}

    @property
    def has_news(self) -> bool:
        return bool(self.items)


def _item_to_dict(item: NewsItem) -> dict:
    return {
        "source_id": item.source_id,
        "source": item.source,
        "title": item.title,
        "link": item.link,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "summary": item.summary,
        "category": item.category,
        "source_weight": item.source_weight,
        "collected_at": item.collected_at.isoformat(),
        "raw_title": item.raw_title,
        "image_url": item.image_url,
        "published_missing": item.published_missing,
        "importance": item.importance,
        "ai_title": item.ai_title,
        "ai_summary": item.ai_summary,
        "ai_used": item.ai_used,
        "merged_sources": list(item.merged_sources),
        "merged_links": list(item.merged_links),
        "original_id": item.original_id,
    }


def _item_from_dict(data: dict) -> NewsItem:
    published = data.get("published_at")
    collected = data.get("collected_at")
    return NewsItem(
        source_id=str(data.get("source_id") or ""),
        source=str(data.get("source") or ""),
        title=str(data.get("title") or ""),
        link=str(data.get("link") or ""),
        published_at=datetime.fromisoformat(published) if published else None,
        summary=str(data.get("summary") or ""),
        category=str(data.get("category") or "综合"),
        source_weight=float(data.get("source_weight") or 1.0),
        collected_at=(
            datetime.fromisoformat(collected) if collected else utcnow()
        ),
        raw_title=str(data.get("raw_title") or data.get("title") or ""),
        image_url=str(data.get("image_url") or ""),
        published_missing=bool(data.get("published_missing", False)),
        importance=float(data.get("importance") or 0.0),
        ai_title=str(data.get("ai_title") or ""),
        ai_summary=str(data.get("ai_summary") or ""),
        ai_used=bool(data.get("ai_used", False)),
        merged_sources=[
            str(value) for value in (data.get("merged_sources") or [])
        ],
        merged_links=[str(value) for value in (data.get("merged_links") or [])],
        original_id=int(data.get("original_id") or 0),
    )


def prepared_report_to_dict(result: ReportResult, tz) -> dict:
    started = result.started_at.astimezone(tz)
    return {
        "prepared_date": started.date().isoformat(),
        "prepared_at": result.started_at.isoformat(),
        "status": result.status,
        "items": [_item_to_dict(item) for item in result.items],
        "stats": asdict(result.stats),
        "source_reports": [asdict(report) for report in result.source_reports],
        "started_at": result.started_at.isoformat(),
        "finished_at": result.finished_at.isoformat(),
        "poster_path": result.poster_path,
        "caption": result.caption,
        "errors": list(result.errors),
        "ai_used": result.ai_used,
        "fatal_source_error": result.fatal_source_error,
        "deals": [asdict(deal) for deal in result.deals],
    }


def prepared_report_from_dict(data: dict) -> ReportResult:
    stats_data = data.get("stats") or {}
    stats = ReportStats(
        **{
            key: value
            for key, value in stats_data.items()
            if key in ReportStats.__dataclass_fields__
        }
    )
    reports = [
        SourceReport(
            **{
                key: value
                for key, value in report.items()
                if key in SourceReport.__dataclass_fields__
            }
        )
        for report in (data.get("source_reports") or [])
        if isinstance(report, dict)
    ]
    deals = [
        SteamDeal(
            **{
                key: value
                for key, value in deal.items()
                if key in SteamDeal.__dataclass_fields__
            }
        )
        for deal in (data.get("deals") or [])
        if isinstance(deal, dict)
    ]
    return ReportResult(
        status=str(data.get("status") or "failed"),
        items=[
            _item_from_dict(item)
            for item in (data.get("items") or [])
            if isinstance(item, dict)
        ],
        stats=stats,
        source_reports=reports,
        started_at=datetime.fromisoformat(str(data.get("started_at"))),
        finished_at=datetime.fromisoformat(str(data.get("finished_at"))),
        poster_path=data.get("poster_path"),
        caption=str(data.get("caption") or ""),
        errors=[str(value) for value in (data.get("errors") or [])],
        ai_used=bool(data.get("ai_used", False)),
        fatal_source_error=bool(data.get("fatal_source_error", False)),
        deals=deals,
    )
