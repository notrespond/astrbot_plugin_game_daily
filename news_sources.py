"""News source adapters (RSS/Atom and the official Steam news API).

Only public, documented endpoints are used.  A failing source is isolated: it
never aborts the other sources.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo
from xml.etree import ElementTree as ET

import aiohttp

from .config import PluginConfig
from .models import NewsItem, normalize_url, utcnow

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SourceSpec:
    id: str
    name: str
    category: str
    weight: float
    kind: str = "rss"
    url: str = ""
    appids: tuple[str, ...] = ()


# Curated public sources.  Users can disable any ID via ``disabled_sources``
# and add/override RSS feeds through ``rss_sources``.
DEFAULT_SOURCES: tuple[SourceSpec, ...] = (
    SourceSpec(
        "steam_store_news",
        "Steam 商店新闻",
        "综合",
        1.4,
        url="https://store.steampowered.com/feeds/news.xml",
    ),
    SourceSpec(
        "steam_new_releases",
        "Steam 新发售",
        "发售",
        1.4,
        url="https://store.steampowered.com/feeds/newreleases.xml",
    ),
    SourceSpec(
        "steam_latest_releases",
        "Steam 最新发售",
        "发售",
        1.25,
        url="https://store.steampowered.com/feeds/latestreleases.xml",
    ),
    SourceSpec(
        "xbox_wire",
        "Xbox 官方新闻",
        "主机",
        1.45,
        url="https://news.xbox.com/en-us/feed/",
    ),
    SourceSpec(
        "playstation_blog",
        "PlayStation 官方博客",
        "主机",
        1.45,
        url="https://blog.playstation.com/feed/",
    ),
    SourceSpec(
        "nintendo_life",
        "Nintendo Life",
        "主机",
        1.1,
        url="https://www.nintendolife.com/feeds/news",
    ),
    SourceSpec(
        "pcgamer",
        "PC Gamer",
        "PC",
        1.15,
        url="https://www.pcgamer.com/rss/",
    ),
    SourceSpec(
        "eurogamer",
        "Eurogamer",
        "综合",
        1.2,
        url="https://www.eurogamer.net/feed",
    ),
    SourceSpec(
        "rps",
        "Rock Paper Shotgun",
        "PC",
        1.05,
        url="https://www.rockpapershotgun.com/feed",
    ),
    SourceSpec(
        "gematsu",
        "Gematsu",
        "综合",
        1.2,
        url="https://www.gematsu.com/feed",
    ),
    SourceSpec(
        "gamesindustry",
        "GamesIndustry.biz",
        "行业",
        1.35,
        url="https://www.gamesindustry.biz/feed",
    ),
    SourceSpec(
        "mcvuk",
        "MCV UK",
        "行业",
        1.1,
        url="https://www.mcvuk.com/feed/",
    ),
    SourceSpec(
        "vgc",
        "VGC",
        "综合",
        1.05,
        url="https://www.videogameschronicle.com/feed/",
    ),
    SourceSpec(
        "espn_esports",
        "ESPN Esports",
        "电竞",
        1.1,
        url="https://www.espn.com/espn/rss/esports/news",
    ),
    SourceSpec(
        "esportsinsider",
        "Esports Insider",
        "电竞",
        1.05,
        url="https://esportsinsider.com/feed",
    ),
    SourceSpec(
        "indiegamesplus",
        "Indie Games Plus",
        "独立",
        1.0,
        url="https://indiegamesplus.com/feed",
    ),
    SourceSpec(
        "steam_api",
        "Steam 官方公告",
        "更新",
        1.6,
        kind="steam_api",
        url="https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/",
    ),
)

STEAM_APP_LABELS = {
    "730": "CS2",
    "570": "Dota 2",
    "578080": "PUBG",
    "1172470": "Apex 英雄",
    "1091500": "赛博朋克 2077",
    "1245620": "艾尔登法环",
    "2358720": "黑神话：悟空",
    "413150": "星露谷物语",
    "1086940": "博德之门 3",
    "292030": "巫师 3",
}


def build_sources(config: PluginConfig) -> list[SourceSpec]:
    """Merge built-in sources with user RSS entries and disabled IDs."""

    disabled = {item.lower() for item in config.disabled_sources}
    enabled_custom_urls = {
        normalize_url(custom.url).lower()
        for custom in config.rss_sources
        if custom.enabled and custom.url
    }
    disabled_custom_urls = {
        normalize_url(custom.url).lower()
        for custom in config.rss_sources
        if not custom.enabled and custom.url
    } - enabled_custom_urls
    sources: list[SourceSpec] = []
    known_urls: dict[str, int] = {}

    def add(spec: SourceSpec) -> None:
        if spec.id.lower() in disabled:
            return
        normalized = normalize_url(spec.url).lower()
        if normalized in known_urls:
            index = known_urls[normalized]
            current = sources[index]
            sources[index] = SourceSpec(
                current.id,
                current.name,
                current.category,
                max(current.weight, spec.weight),
                current.kind,
                current.url,
                current.appids,
            )
            return
        known_urls[normalized] = len(sources)
        sources.append(spec)

    for spec in DEFAULT_SOURCES:
        if spec.kind == "steam_api":
            if config.enable_steam_news_api and config.steam_appids:
                add(
                    SourceSpec(
                        spec.id,
                        spec.name,
                        spec.category,
                        spec.weight,
                        "steam_api",
                        spec.url,
                        tuple(config.steam_appids),
                    )
                )
        else:
            add(spec)

    for custom in config.rss_sources:
        if not custom.enabled:
            continue
        normalized = normalize_url(custom.url).lower()
        if normalized in known_urls:
            index = known_urls[normalized]
            current = sources[index]
            sources[index] = SourceSpec(
                current.id,
                custom.name or current.name,
                custom.category or current.category,
                custom.weight,
                current.kind,
                custom.url,
                current.appids,
            )
            continue
        spec = SourceSpec(
            custom.id,
            custom.name,
            custom.category,
            custom.weight,
            "rss",
            custom.url,
        )
        known_urls[normalized] = len(sources)
        if custom.id.lower() not in disabled:
            sources.append(spec)
    if disabled_custom_urls:
        sources = [
            source
            for source in sources
            if normalize_url(source.url).lower() not in disabled_custom_urls
        ]
    return sources


def source_ids() -> list[str]:
    return [spec.id for spec in DEFAULT_SOURCES]


# ----------------------------------------------------------------------
# Text cleaning
# ----------------------------------------------------------------------
class _HTMLTextExtractor(HTMLParser):
    _BLOCK_TAGS = {
        "p",
        "br",
        "div",
        "li",
        "ul",
        "ol",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "tr",
        "td",
    }
    _SKIP_TAGS = {"script", "style", "noscript", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        lowered = tag.lower()
        if lowered in self._SKIP_TAGS:
            self._skip_depth += 1
        elif lowered in self._BLOCK_TAGS:
            self._parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif lowered in self._BLOCK_TAGS:
            self._parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts)


def clean_html_text(value: str, limit: int = 1600) -> str:
    if not value:
        return ""
    text = html.unescape(str(value))
    # Some feeds escape twice.
    if "&lt;" in text or "&amp;" in text:
        text = html.unescape(text)
    parser = _HTMLTextExtractor()
    try:
        parser.feed(text)
        parser.close()
        text = parser.get_text()
    except Exception:
        text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def parse_datetime_value(
    value: str | int | float | None, default_tz: ZoneInfo
) -> tuple[datetime | None, bool]:
    """Return ``(utc_datetime, missing)``.  Never invents a timestamp."""

    if value is None:
        return None, True
    if isinstance(value, (int, float)):
        try:
            number = float(value)
            if number <= 0:
                return None, True
            return datetime.fromtimestamp(number, tz=timezone.utc), False
        except (OverflowError, OSError, ValueError):
            return None, True
    text = str(value).strip()
    if not text:
        return None, True
    if re.fullmatch(r"\d{10,13}", text):
        try:
            number = float(text)
            if len(text) >= 13:
                number /= 1000.0
            return datetime.fromtimestamp(number, tz=timezone.utc), False
        except (OverflowError, OSError, ValueError):
            pass
    try:
        parsed = parsedate_to_datetime(text)
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=default_tz)
            return parsed.astimezone(timezone.utc), False
    except (TypeError, ValueError, OverflowError):
        pass
    iso = text.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=default_tz)
        return parsed.astimezone(timezone.utc), False
    except (TypeError, ValueError, OverflowError):
        pass
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%a, %d %b %Y %H:%M:%S",
    ):
        try:
            parsed = datetime.strptime(text, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=default_tz)
            return parsed.astimezone(timezone.utc), False
        except ValueError:
            continue
    return None, True


def _local_name(tag: Any) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1].lower()


def _element_text(element: ET.Element) -> str:
    if element.text and element.text.strip():
        return element.text.strip()
    if len(element):
        return "".join(element.itertext()).strip()
    return ""


def _first_child(element: ET.Element, names: set[str]) -> ET.Element | None:
    for child in list(element):
        if _local_name(child.tag) in names:
            return child
    return None


def _child_text(element: ET.Element, names: set[str]) -> str:
    child = _first_child(element, names)
    return _element_text(child) if child is not None else ""


def _extract_link(entry: ET.Element, base_url: str) -> str:
    for child in list(entry):
        if _local_name(child.tag) != "link":
            continue
        href = (child.get("href") or "").strip()
        rel = (child.get("rel") or "").strip().lower()
        if href and rel in ("", "alternate"):
            return urljoin(base_url, href)
        text = (child.text or "").strip()
        if text:
            return urljoin(base_url, text)
    guid = _child_text(entry, {"guid", "id"})
    if guid.lower().startswith(("http://", "https://")):
        return guid
    return ""


def _extract_date_text(entry: ET.Element) -> str | None:
    for name in ("pubdate", "published", "updated", "date", "created", "issued"):
        child = _first_child(entry, {name})
        if child is not None:
            text = _element_text(child)
            if text:
                return text
    return None


def _extract_categories(entry: ET.Element) -> list[str]:
    result: list[str] = []
    for child in list(entry):
        if _local_name(child.tag) != "category":
            continue
        text = _element_text(child) or (child.get("term") or "").strip()
        if text:
            result.append(text[:80])
    return result


def _sanitize_xml_text(text: str) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)


def _parse_xml(data: bytes) -> ET.Element:
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        pass
    text = None
    declared = re.search(rb"encoding\s*=\s*[\"']([^\"']+)[\"']", data[:300])
    encodings = []
    if declared:
        try:
            encodings.append(declared.group(1).decode("ascii", "ignore"))
        except Exception:
            pass
    encodings.extend(["utf-8", "gb18030", "latin-1"])
    for encoding in dict.fromkeys(encodings):
        try:
            text = data.decode(encoding, errors="replace")
            break
        except (LookupError, UnicodeError):
            continue
    if text is None:
        text = data.decode("utf-8", errors="replace")
    text = text.lstrip("\ufeff \t\r\n")
    text = re.sub(
        r"^\s*<\?xml[^>]*\?>", "", text, count=1, flags=re.IGNORECASE
    )
    text = _sanitize_xml_text(text)
    return ET.fromstring(text)


def parse_rss_feed(
    data: bytes,
    spec: SourceSpec,
    default_tz: ZoneInfo,
    now: datetime | None = None,
) -> list[NewsItem]:
    """Parse RSS 2.0, RSS 1.0/RDF and Atom into ``NewsItem`` objects."""

    collected = now or utcnow()
    root = _parse_xml(data)
    base_url = spec.url
    seen: set[str] = set()
    items: list[NewsItem] = []

    entries: list[ET.Element] = []
    for element in root.iter():
        if _local_name(element.tag) in ("item", "entry"):
            entries.append(element)
    if not entries:
        return []

    # Resolve relative links against the feed/channel link when possible.
    channel = None
    for element in root.iter():
        if _local_name(element.tag) == "channel":
            channel = element
            break
    if channel is None and _local_name(root.tag) == "feed":
        channel = root
    if channel is not None:
        channel_link = _extract_link(channel, spec.url)
        if channel_link:
            base_url = channel_link

    for entry in entries:
        title = clean_html_text(_child_text(entry, {"title"}), limit=400)
        if not title:
            continue
        link = _extract_link(entry, base_url)
        if not link:
            continue
        link = normalize_url(link)
        if link in seen:
            continue
        seen.add(link)
        summary = clean_html_text(
            _child_text(
                entry,
                {
                    "description",
                    "summary",
                    "content",
                    "encoded",
                    "content:encoded",
                },
            ),
            limit=1600,
        )
        published_raw = _extract_date_text(entry)
        published_at, missing = parse_datetime_value(published_raw, default_tz)
        if published_at is not None and published_at > collected:
            # Clock skew or a future date.  Treat it as now rather than
            # displaying a date that has not happened yet.
            published_at = collected
        items.append(
            NewsItem(
                source_id=spec.id,
                source=spec.name,
                title=title,
                link=link,
                published_at=published_at,
                summary=summary,
                category=spec.category,
                source_weight=spec.weight,
                collected_at=collected,
                raw_title=title,
                published_missing=missing,
            )
        )
    return items


# ----------------------------------------------------------------------
# Async fetching
# ----------------------------------------------------------------------
@dataclass(slots=True)
class FetchResult:
    source_id: str
    name: str
    items: list[NewsItem] = field(default_factory=list)
    error: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error


class RequestPacer:
    def __init__(self, interval: float) -> None:
        self.interval = max(0.0, interval)
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        if self.interval <= 0:
            return
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            delay = self.interval - (now - self._last)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = loop.time()


class NewsFetcher:
    def __init__(self, config: PluginConfig, logger=None) -> None:
        self.config = config
        self.log = logger or log
        self.pacer = RequestPacer(config.request_interval)

    async def fetch(
        self,
        sources: list[SourceSpec],
        now: datetime | None = None,
        limit_per_source: int | None = None,
    ) -> list[FetchResult]:
        if not sources:
            return []
        collected = now or utcnow()
        timeout = aiohttp.ClientTimeout(
            total=self.config.request_timeout,
            sock_connect=min(10, self.config.request_timeout),
            sock_read=self.config.request_timeout,
        )
        connector = aiohttp.TCPConnector(
            limit=max(2, self.config.max_concurrency * 2),
            ttl_dns_cache=300,
        )
        headers = {
            "User-Agent": self.config.user_agent,
            "Accept": (
                "application/rss+xml, application/atom+xml, "
                "application/xml, text/xml, application/json;q=0.9, */*;q=0.8"
            ),
        }
        semaphore = asyncio.Semaphore(max(1, self.config.max_concurrency))

        async with aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            headers=headers,
            trust_env=self.config.use_proxy_env,
        ) as session:

            async def worker(spec: SourceSpec) -> FetchResult:
                async with semaphore:
                    try:
                        result = await self._fetch_one(session, spec, collected)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - isolate sources
                        self.log.warning(
                            "新闻源 %s 获取失败: %s", spec.id, self.config.redact(str(exc))
                        )
                        result = FetchResult(
                            source_id=spec.id,
                            name=spec.name,
                            error=self.config.redact(str(exc))[:500],
                        )
                    if limit_per_source is not None:
                        result.items = result.items[: max(0, limit_per_source)]
                    return result

            results = await asyncio.gather(*(worker(spec) for spec in sources))
        return list(results)

    async def _fetch_one(
        self, session: aiohttp.ClientSession, spec: SourceSpec, now: datetime
    ) -> FetchResult:
        if spec.kind == "steam_api":
            return await self._fetch_steam_api(session, spec, now)
        data = await self._request_bytes(session, spec.url)
        items = await asyncio.to_thread(
            parse_rss_feed, data, spec, self.config.timezone, now
        )
        return FetchResult(source_id=spec.id, name=spec.name, items=items)

    async def _fetch_steam_api(
        self, session: aiohttp.ClientSession, spec: SourceSpec, now: datetime
    ) -> FetchResult:
        results: list[NewsItem] = []
        errors: list[str] = []
        for appid in spec.appids:
            params = {
                "appid": appid,
                    "count": "8",
                "maxlength": "900",
                "format": "json",
            }
            try:
                data = await self._request_bytes(
                    session,
                    "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/",
                    params=params,
                )
                payload = json.loads(data.decode("utf-8", errors="replace"))
                appnews = payload.get("appnews") or {}
                entries = appnews.get("newsitems") or []
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - isolate one appid
                errors.append(f"{appid}: {self.config.redact(str(exc))[:160]}")
                continue
            label = STEAM_APP_LABELS.get(appid, f"App {appid}")
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                feed_name = str(
                    entry.get("feedname") or entry.get("feedlabel") or ""
                ).strip()
                try:
                    feed_type = int(entry.get("feed_type"))
                except (TypeError, ValueError):
                    feed_type = 0
                # The endpoint also aggregates third-party posts.  Keep only
                # Valve's official community announcements.
                if feed_type != 1 and feed_name != "steam_community_announcements":
                    continue
                title = clean_html_text(str(entry.get("title") or ""), limit=400)
                if not title:
                    continue
                contents = clean_html_text(
                    str(entry.get("contents") or ""), limit=1600
                )
                timestamp = entry.get("date")
                published_at, missing = parse_datetime_value(
                    timestamp, self.config.timezone
                )
                if published_at is not None and published_at > now:
                    published_at = now
                link = str(entry.get("url") or "").strip()
                if not link:
                    gid = entry.get("gid")
                    if gid:
                        link = (
                            f"https://store.steampowered.com/news/app/{appid}/view/{gid}"
                        )
                if not link:
                    continue
                results.append(
                    NewsItem(
                        source_id=spec.id,
                        source=f"Steam 公告·{label}",
                        title=title,
                        link=normalize_url(link),
                        published_at=published_at,
                        summary=contents,
                        category=spec.category,
                        source_weight=spec.weight,
                        collected_at=now,
                        raw_title=title,
                        published_missing=missing,
                    )
                )
        if not results and errors:
            return FetchResult(
                source_id=spec.id,
                name=spec.name,
                error="; ".join(errors)[:500],
            )
        warnings = [f"部分 appid 获取失败: {err}" for err in errors]
        return FetchResult(
            source_id=spec.id,
            name=spec.name,
            items=results,
            warnings=warnings,
        )

    async def _request_bytes(
        self,
        session: aiohttp.ClientSession,
        url: str,
        params: dict | None = None,
    ) -> bytes:
        last_error: Exception | None = None
        attempts = self.config.retry_times + 1
        for attempt in range(attempts):
            await self.pacer.wait()
            try:
                async with session.get(url, params=params) as response:
                    status = response.status
                    if status in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                        retry_after = response.headers.get("Retry-After", "")
                        delay = 1.5 * (attempt + 1)
                        try:
                            delay = min(10.0, max(delay, float(retry_after)))
                        except (TypeError, ValueError):
                            pass
                        await asyncio.sleep(delay)
                        continue
                    if status != 200:
                        raise RuntimeError(f"HTTP {status}")
                    content_length = response.headers.get("Content-Length")
                    if content_length:
                        try:
                            if int(content_length) > self.config.max_response_bytes:
                                raise RuntimeError("响应体超过大小限制")
                        except ValueError:
                            pass
                    # StreamReader.read(n) may return a partial chunk; use
                    # read() so the whole (bounded) feed is parsed.
                    data = await response.read()
                    if len(data) > self.config.max_response_bytes:
                        raise RuntimeError("响应体超过大小限制")
                    if not data:
                        raise RuntimeError("响应体为空")
                    head = data[:2000].lower()
                    if b"<!entity" in head:
                        raise RuntimeError("拒绝包含实体的 XML")
                    return data
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    break
                await asyncio.sleep(1.2 * (attempt + 1))
        raise RuntimeError(
            f"请求失败（{urlsplit(url).netloc}）: {last_error}"
        )


def fetch_result_reports(results: list[FetchResult]):
    """Convert fetch results to ``SourceReport`` objects."""

    from .models import SourceReport

    return [
        SourceReport(
            source_id=result.source_id,
            name=result.name,
            ok=result.ok,
            item_count=len(result.items),
            error=result.error,
            warnings=list(result.warnings),
        )
        for result in results
    ]
