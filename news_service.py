"""Collection, filtering, de-duplication, ranking and poster orchestration."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import AsyncIterator

from .ai_service import AIEnhanceResult, AIService
from .config import DEFAULT_CATEGORIES, PluginConfig
from .models import (
    NewsItem,
    ReportResult,
    ReportStats,
    SourceReport,
    normalize_title,
)
from .news_sources import NewsFetcher, build_sources, fetch_result_reports
from .poster import PosterError, PosterRenderer
from .storage import Storage

log = logging.getLogger(__name__)


class RunBusyError(RuntimeError):
    """Another scheduled/manual report is already running."""


# ----------------------------------------------------------------------
# Rule-based classification / scoring
# ----------------------------------------------------------------------
_CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "免费/福利",
        (
            "free weekend",
            "free-to-play",
            "free to play",
            "giveaway",
            "free game",
            "free on",
            "免费",
            "领取",
            "喜加一",
            "赠送",
            "福利",
            "白嫖",
        ),
    ),
    (
        "折扣",
        (
            "sale",
            "discount",
            "deal",
            "% off",
            "price drop",
            "特惠",
            "折扣",
            "促销",
            "史低",
            "降价",
        ),
    ),
    (
        "评测",
        (
            "review",
            "reviews",
            "hands-on",
            "impressions",
            "preview",
            "评测",
            "试玩报告",
            "上手",
        ),
    ),
    (
        "电竞",
        (
            "esports",
            "tournament",
            "championship",
            "league",
            "major",
            "grand final",
            "赛事",
            "冠军",
            "战队",
            "联赛",
            "世界杯",
            "总决赛",
            "电竞",
        ),
    ),
    (
        "发售",
        (
            "release date",
            "releases",
            "release",
            "launch",
            "out now",
            "early access",
            "demo",
            "发售",
            "上线",
            "开售",
            "抢先体验",
            "试玩",
            "定档",
        ),
    ),
    (
        "更新",
        (
            "patch",
            "hotfix",
            "update",
            "season",
            "dlc",
            "expansion",
            "roadmap",
            "更新",
            "补丁",
            "赛季",
            "资料片",
            "版本",
            "维护",
        ),
    ),
    (
        "行业",
        (
            "acquisition",
            "acquire",
            "layoff",
            "layoffs",
            "lawsuit",
            "legal",
            "court",
            "jury",
            "dispute",
            "antitrust",
            "bankrupt",
            "bankruptcy",
            "shut down",
            "closure",
            "earnings",
            "revenue",
            "studio",
            "publisher",
            "收购",
            "裁员",
            "财报",
            "起诉",
            "诉讼",
            "破产",
            "倒闭",
            "关停",
            "工作室",
            "发行商",
            "营收",
            "上市",
        ),
    ),
    (
        "主机",
        (
            "playstation",
            "xbox",
            "nintendo",
            "switch",
            "ps5",
            "ps4",
            "任天堂",
            "索尼",
            "微软",
            "主机",
        ),
    ),
    (
        "独立",
        ("indie", "independent", "独立游戏", "独立"),
    ),
    (
        "PC",
        ("steam", "epic games", "pc ", "windows", "掌机", "pc版"),
    ),
)

_HIGH_KEYWORDS = (
    "official",
    "announce",
    "release date",
    "delay",
    "delayed",
    "shutdown",
    "cancel",
    "acquisition",
    "layoff",
    "free",
    "major",
    "championship",
    "官方",
    "宣布",
    "延期",
    "停服",
    "取消",
    "收购",
    "裁员",
    "免费",
    "冠军",
    "总决赛",
)
_MEDIUM_KEYWORDS = (
    "update",
    "patch",
    "dlc",
    "season",
    "launch",
    "early access",
    "sale",
    "discount",
    "更新",
    "补丁",
    "资料片",
    "赛季",
    "上线",
    "折扣",
    "特惠",
    "试玩",
)

_SITE_SUFFIXES = (
    "pc gamer",
    "eurogamer",
    "gematsu",
    "gamespot",
    "ign",
    "polygon",
    "vgc",
    "nintendo life",
    "gamesindustry.biz",
    "mcv uk",
    "rock paper shotgun",
    "hltv",
    "espn esports",
    "esports insider",
    "xbox wire",
    "playstation blog",
)


def _contains_keyword(text: str, keyword: str) -> bool:
    if not keyword:
        return False
    if keyword.isascii():
        pattern = r"(?<![a-z0-9])" + re.escape(keyword.strip()) + r"(?![a-z0-9])"
        return re.search(pattern, text) is not None
    return keyword in text


def categorize(title: str, summary: str, source_category: str = "综合") -> str:
    # A clear signal in the title wins over incidental words in the summary
    # (e.g. a bankruptcy story that happens to mention a release).
    title_text = title.lower()
    for category, keywords in _CATEGORY_RULES:
        for keyword in keywords:
            if _contains_keyword(title_text, keyword):
                return category
    text = f"{title}\n{summary}".lower()
    for category, keywords in _CATEGORY_RULES:
        for keyword in keywords:
            if _contains_keyword(text, keyword):
                return category
    if source_category in DEFAULT_CATEGORIES:
        return source_category
    return "综合"


def _strip_site_suffix(title: str, source_name: str = "") -> str:
    text = title.strip()
    suffixes = list(_SITE_SUFFIXES)
    if source_name:
        suffixes.insert(0, source_name.lower())
    for suffix in suffixes:
        pattern = re.compile(
            r"\s+[-–—|]\s*" + re.escape(suffix) + r"\s*$", re.IGNORECASE
        )
        text = pattern.sub("", text)
    return text.strip()


def _rule_score(item: NewsItem, now: datetime, config: PluginConfig) -> float:
    score = 24.0
    if item.published_at is not None:
        age_hours = max(0.0, (now - item.published_at).total_seconds() / 3600.0)
        span = max(1, config.news_window_hours)
        freshness = max(0.0, 1.0 - age_hours / span)
        score += 30.0 * freshness
    else:
        score += 14.0
    score += max(0.0, min(20.0, (item.source_weight - 1.0) * 28.0))
    text = f"{item.title}\n{item.summary}".lower()
    high_hits = sum(1 for word in _HIGH_KEYWORDS if word in text)
    medium_hits = sum(1 for word in _MEDIUM_KEYWORDS if word in text)
    score += min(28.0, high_hits * 14.0 + medium_hits * 6.0)
    if item.category in config.categories_preferred:
        score += 8.0
    if item.category in {"发售", "更新", "免费/福利", "电竞"}:
        score += 4.0
    if len(item.summary) >= 80:
        score += 4.0
    return max(0.0, min(100.0, score))


def _recency_key(item: NewsItem, now: datetime) -> float:
    if item.published_at is None:
        return now.timestamp() - 3600.0
    return item.published_at.timestamp()


def _dedupe_items(
    items: list[NewsItem], now: datetime, logger: logging.Logger
) -> tuple[list[NewsItem], int]:
    """Exact-key and near-title de-duplication, merging sources."""

    ordered = sorted(
        items,
        key=lambda item: (
            item.source_weight,
            item.importance,
            _recency_key(item, now),
        ),
        reverse=True,
    )
    kept: list[NewsItem] = []
    by_url: dict[str, NewsItem] = {}
    by_title: dict[str, NewsItem] = {}
    by_content: dict[str, NewsItem] = {}
    dropped = 0

    for item in ordered:
        title_key = item.title_key
        url_key = item.url_key
        content_key = item.content_key
        existing = by_url.get(url_key) or by_content.get(content_key)
        if existing is None and title_key:
            existing = by_title.get(title_key)
        if existing is not None:
            existing.merge_sources_from(item)
            dropped += 1
            continue
        normalized = normalize_title(item.raw_title)
        if len(normalized) >= 8:
            near = None
            for candidate in kept:
                candidate_title = normalize_title(candidate.raw_title)
                if len(candidate_title) < 8:
                    continue
                ratio = SequenceMatcher(
                    None, normalized, candidate_title, autojunk=False
                ).ratio()
                if ratio >= 0.9:
                    near = candidate
                    break
            if near is not None:
                near.merge_sources_from(item)
                dropped += 1
                continue
        kept.append(item)
        by_url[url_key] = item
        if title_key:
            by_title[title_key] = item
        by_content[content_key] = item
    logger.debug("批次内去重：%s -> %s", len(items), len(kept))
    return kept, dropped


def _merge_ai_duplicates(
    items: list[NewsItem], ai_result: AIEnhanceResult
) -> list[NewsItem]:
    by_original_id = {item.original_id: item for item in items}
    remove: set[int] = set()
    for item in items:
        result = ai_result.items.get(item.original_id)
        if result is None or result.duplicate_of is None:
            continue
        target = by_original_id.get(result.duplicate_of)
        if target is None or target is item:
            continue
        if target.original_id in remove:
            continue
        target.merge_sources_from(item)
        target.ai_used = target.ai_used or item.ai_used
        remove.add(item.original_id)
    if remove:
        return [item for item in items if item.original_id not in remove]
    return items


def _select_items(
    items: list[NewsItem], config: PluginConfig, now: datetime
) -> list[NewsItem]:
    ordered = sorted(
        items,
        key=lambda item: (
            item.importance,
            _recency_key(item, now),
            item.source_weight,
        ),
        reverse=True,
    )
    selected: list[NewsItem] = []
    per_source: dict[str, int] = {}
    per_category: dict[str, int] = {}
    skipped: list[NewsItem] = []

    for item in ordered:
        if len(selected) >= config.max_news:
            break
        if per_source.get(item.source_id, 0) >= config.max_per_source:
            skipped.append(item)
            continue
        if per_category.get(item.category, 0) >= 3:
            skipped.append(item)
            continue
        selected.append(item)
        per_source[item.source_id] = per_source.get(item.source_id, 0) + 1
        per_category[item.category] = per_category.get(item.category, 0) + 1

    if len(selected) < config.max_news:
        for item in skipped:
            if len(selected) >= config.max_news:
                break
            selected.append(item)
    return selected[: config.max_news]


def _format_caption(
    items: list[NewsItem], now: datetime, empty_text: str
) -> str:
    if not items:
        return f"游戏日报 · {now.strftime('%m月%d日')}\n{empty_text}"
    lines = [f"游戏日报 · {now.strftime('%m月%d日')} · {len(items)} 条精选"]
    for index, item in enumerate(items, start=1):
        title = item.display_title.strip()
        if len(title) > 48:
            title = title[:47].rstrip() + "…"
        lines.append(f"{index}. [{item.category}] {title}")
    lines.append("详情见海报，内容以原文为准。")
    return "\n".join(lines)


@dataclass(slots=True)
class SelfTestResult:
    source_reports: list[SourceReport]
    ai_ok: bool
    ai_message: str
    poster_path: str | None
    poster_error: str
    sample_count: int
    stats: ReportStats

    def summary(self) -> str:
        ok_sources = sum(1 for report in self.source_reports if report.ok)
        lines = [
            f"新闻源：{ok_sources}/{len(self.source_reports)} 成功",
        ]
        for report in self.source_reports[:12]:
            lines.append(f"- {report.name}：{report.status_text()}（{report.item_count} 条）")
        if len(self.source_reports) > 12:
            lines.append(f"- 其余 {len(self.source_reports) - 12} 个来源略")
        lines.append(f"AI：{self.ai_message}")
        if self.poster_path:
            lines.append(f"海报：{self.poster_path}（{self.stats.total_selected} 条示例）")
        else:
            lines.append(f"海报：生成失败 {self.poster_error}")
        return "\n".join(lines)


class DailyReportService:
    def __init__(
        self,
        config: PluginConfig,
        storage: Storage,
        fetcher: NewsFetcher,
        ai: AIService,
        poster: PosterRenderer,
        logger=None,
        clock=None,
        deals_fetcher=None,
    ) -> None:
        self.config = config
        self.storage = storage
        self.fetcher = fetcher
        self.ai = ai
        self.poster = poster
        self.deals_fetcher = deals_fetcher
        self.log = logger or log
        self._clock = clock or (lambda: datetime.now(config.timezone))

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=self.config.timezone)
        return value.astimezone(self.config.timezone)

    @asynccontextmanager
    async def exclusive(self, mode: str) -> AsyncIterator[None]:
        owner = f"{mode}:{uuid.uuid4().hex}"
        acquired = await asyncio.to_thread(
            self.storage.try_acquire_lock, "daily_run", owner, 1800
        )
        if not acquired:
            raise RunBusyError("已有日报任务正在运行，请稍后再试。")
        try:
            yield
        finally:
            try:
                await asyncio.to_thread(self.storage.release_lock, "daily_run", owner)
            except Exception:
                self.log.warning("释放运行锁失败", exc_info=True)

    async def generate(
        self,
        mode: str = "manual",
        use_sent_filter: bool = True,
        make_poster: bool = True,
        poster_when_empty: bool = False,
    ) -> ReportResult:
        started_at = self._now()
        now_utc = started_at.astimezone(timezone.utc)
        stats = ReportStats()
        errors: list[str] = []
        source_reports: list[SourceReport] = []
        sources = build_sources(self.config)
        stats.sources_total = len(sources)

        if not sources:
            errors.append("没有启用任何新闻源，请检查 disabled_sources / RSS 配置。")
            result = ReportResult(
                status="failed",
                items=[],
                stats=stats,
                source_reports=[],
                started_at=started_at,
                finished_at=self._now(),
                errors=errors,
                fatal_source_error=True,
                caption=_format_caption([], started_at, self.config.empty_report_text),
            )
            return result

        self.log.info("开始采集游戏资讯：%s 个来源", len(sources))
        try:
            fetch_results = await self.fetcher.fetch(sources)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            message = self.config.redact(f"采集失败：{type(exc).__name__}: {exc}")
            self.log.error(message, exc_info=True)
            result = ReportResult(
                status="failed",
                items=[],
                stats=stats,
                source_reports=[],
                started_at=started_at,
                finished_at=self._now(),
                errors=[message],
                fatal_source_error=True,
                caption=_format_caption([], started_at, self.config.empty_report_text),
            )
            return result

        source_reports = fetch_result_reports(fetch_results)
        stats.sources_ok = sum(1 for report in source_reports if report.ok)
        stats.sources_failed = stats.sources_total - stats.sources_ok
        for report in source_reports:
            if not report.ok:
                errors.append(f"{report.name}: {report.error}")
            for warning in report.warnings:
                errors.append(f"{report.name}: {warning}")
        await asyncio.to_thread(self.storage.record_source_health, source_reports)

        raw_items: list[NewsItem] = []
        for fetch_result in fetch_results:
            raw_items.extend(fetch_result.items)
        stats.total_collected = len(raw_items)

        if stats.sources_ok == 0:
            errors.append("所有新闻源均不可用，本次日报未生成。")
            result = ReportResult(
                status="failed",
                items=[],
                stats=stats,
                source_reports=source_reports,
                started_at=started_at,
                finished_at=self._now(),
                errors=errors,
                fatal_source_error=True,
                caption=_format_caption([], started_at, self.config.empty_report_text),
            )
            return result

        filtered: list[NewsItem] = []
        cutoff = now_utc - timedelta(hours=self.config.news_window_hours)
        excluded = [word.lower() for word in self.config.excluded_keywords if word]
        for item in raw_items:
            item.title = _strip_site_suffix(item.title, item.source)
            if not item.title:
                continue
            item.raw_title = item.title
            item.summary = (item.summary or "").strip()
            if item.summary and normalize_title(item.summary) == normalize_title(
                item.title
            ):
                item.summary = ""
            if item.published_at is None:
                if not self.config.include_missing_time:
                    continue
                item.published_missing = True
            elif item.published_at > now_utc + timedelta(hours=2):
                item.published_at = None
                item.published_missing = True
                if not self.config.include_missing_time:
                    continue
            elif item.published_at < cutoff:
                continue
            text = f"{item.title}\n{item.summary}".lower()
            if any(word in text for word in excluded):
                continue
            item.category = categorize(item.title, item.summary, item.category)
            filtered.append(item)

        stats.total_in_window = len(filtered)

        # Cap each feed before ranking so a single prolific source cannot
        # flood the candidate pool.
        grouped: dict[str, list[NewsItem]] = {}
        for item in filtered:
            grouped.setdefault(item.source_id, []).append(item)
        capped: list[NewsItem] = []
        for group in grouped.values():
            group.sort(
                key=lambda item: (
                    _recency_key(item, now_utc),
                    item.source_weight,
                ),
                reverse=True,
            )
            capped.extend(group[: self.config.max_source_items])

        for item in capped:
            item.importance = _rule_score(item, now_utc, self.config)

        deduped, _dropped_local = _dedupe_items(capped, now_utc, self.log)
        stats.total_after_local_dedupe = len(deduped)

        if use_sent_filter:
            deduped, dropped_sent = await asyncio.to_thread(
                self.storage.filter_unsent, deduped
            )
            if dropped_sent:
                self.log.info("跳过已发送新闻 %s 条", dropped_sent)
        stats.total_after_sent_filter = len(deduped)

        deduped.sort(
            key=lambda item: (
                item.importance,
                _recency_key(item, now_utc),
                item.source_weight,
            ),
            reverse=True,
        )
        candidates = deduped[: self.config.max_candidates]
        ai_result: AIEnhanceResult | None = None

        if self.config.ai_available and candidates:
            ai_pool = candidates[: self.config.ai_max_candidates]
            stats.ai_candidates = len(ai_pool)
            ai_result = await self.ai.enhance(ai_pool)
            stats.ai_used = ai_result.used
            if ai_result.errors:
                errors.extend(f"AI: {message}" for message in ai_result.errors)
            for item in ai_pool:
                outcome = ai_result.items.get(item.original_id)
                if outcome is None:
                    continue
                if outcome.title:
                    item.ai_title = outcome.title
                if outcome.summary and len(outcome.summary) >= 8:
                    item.ai_summary = outcome.summary
                if outcome.category in DEFAULT_CATEGORIES:
                    item.category = outcome.category
                if outcome.importance is not None:
                    item.importance = max(
                        0.0,
                        min(
                            100.0,
                            outcome.importance * 0.55 + item.importance * 0.45,
                        ),
                    )
                item.ai_used = True
            if ai_result.used:
                candidates = _merge_ai_duplicates(candidates, ai_result)
                stats.total_ai_processed = sum(
                    1 for item in candidates if item.ai_used
                )
        elif self.config.ai_enabled and not self.config.ai_available:
            errors.append("AI 未配置 API Key，已使用规则模式。")

        selected = _select_items(candidates, self.config, now_utc)
        stats.total_selected = len(selected)
        empty = not selected

        deals = []
        if make_poster and selected and self.deals_fetcher is not None:
            try:
                fetched_deals = await self.deals_fetcher.fetch(limit=12)
                deals = await asyncio.to_thread(
                    self.storage.select_deals,
                    fetched_deals,
                    4,
                    self.config.steam_deal_cooldown_days,
                    started_at,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.log.warning(
                    "Steam 折扣获取失败: %s", self.config.redact(str(exc))
                )

        poster_path = None
        if make_poster and (selected or poster_when_empty):
            try:
                prefix = "gamedaily" if mode != "preview" else "preview"
                poster_path = await asyncio.to_thread(
                    self.poster.render,
                    selected,
                    stats,
                    started_at,
                    empty,
                    prefix,
                    bool(ai_result and ai_result.used),
                    deals,
                )
            except PosterError as exc:
                message = self.config.redact(str(exc))
                errors.append(message)
                self.log.error("海报生成失败: %s", message)
            except Exception as exc:  # noqa: BLE001
                message = self.config.redact(
                    f"海报生成失败：{type(exc).__name__}: {exc}"
                )
                errors.append(message)
                self.log.error(message, exc_info=True)

        if empty:
            status = "no_news"
        else:
            status = "ok"

        result = ReportResult(
            status=status,
            items=selected,
            stats=stats,
            source_reports=source_reports,
            started_at=started_at,
            finished_at=self._now(),
            poster_path=poster_path,
            caption=_format_caption(
                selected, started_at, self.config.empty_report_text
            ),
            errors=errors,
            ai_used=bool(ai_result and ai_result.used),
            deals=deals,
        )
        self.log.info(
            "采集完成：%s，状态=%s，AI=%s",
            result.stats.summary_text(),
            status,
            "on" if result.ai_used else "fallback",
        )
        return result

    async def finalize_run(self, result: ReportResult, mode: str) -> None:
        if mode not in {"scheduled", "manual"}:
            return
        try:
            await asyncio.to_thread(self.storage.record_run, result, mode)
        except Exception:
            self.log.warning("保存运行记录失败", exc_info=True)

    async def mark_sent(self, result: ReportResult) -> None:
        if result.items:
            await asyncio.to_thread(self.storage.mark_sent, result.items)
        if result.deals:
            try:
                await asyncio.to_thread(
                    self.storage.mark_deals_shown, result.deals
                )
            except Exception:
                self.log.warning("记录折扣轮换历史失败", exc_info=True)

    async def purge_old_records(self) -> None:
        try:
            removed = await asyncio.to_thread(
                self.storage.purge_sent, self.config.dedupe_retention_days
            )
            if removed:
                self.log.info("清理过期去重记录 %s 条", removed)
            await asyncio.to_thread(self.poster.cleanup)
        except Exception:
            self.log.warning("清理旧数据失败", exc_info=True)

    async def self_test(self) -> SelfTestResult:
        sources = build_sources(self.config)
        reports: list[SourceReport] = []
        sample: list[NewsItem] = []
        if sources:
            results = await self.fetcher.fetch(sources, limit_per_source=1)
            reports = fetch_result_reports(results)
            for result in results:
                sample.extend(result.items[:1])
        ai_ok, ai_message = await self.ai.ping()
        sample = sample[:6]
        stats = ReportStats(
            total_collected=sum(report.item_count for report in reports),
            total_in_window=len(sample),
            total_after_local_dedupe=len(sample),
            total_after_sent_filter=len(sample),
            total_selected=len(sample),
            sources_total=len(reports),
            sources_ok=sum(1 for report in reports if report.ok),
            sources_failed=sum(1 for report in reports if not report.ok),
        )
        poster_path = None
        poster_error = ""
        try:
            poster_path = await asyncio.to_thread(
                self.poster.render,
                sample,
                stats,
                self._now(),
                not sample,
                "preview-test",
                False,
            )
        except Exception as exc:  # noqa: BLE001
            poster_error = self.config.redact(
                f"{type(exc).__name__}: {exc}"
            )[:300]
        return SelfTestResult(
            source_reports=reports,
            ai_ok=ai_ok,
            ai_message=ai_message
            if ai_ok
            else f"{ai_message}（规则模式仍可用）",
            poster_path=poster_path,
            poster_error=poster_error,
            sample_count=len(sample),
            stats=stats,
        )


def text_fallback(result: ReportResult, config: PluginConfig) -> str:
    """Plain-text digest used when poster rendering fails."""

    if not result.items:
        return result.caption or config.empty_report_text
    lines = [f"游戏日报 · {len(result.items)} 条精选"]
    for index, item in enumerate(result.items, start=1):
        source = item.display_source or item.source
        lines.append(f"{index}. [{item.category}] {item.display_title}")
        summary = item.display_summary.strip()
        if summary:
            if len(summary) > 120:
                summary = summary[:119].rstrip() + "…"
            lines.append(f"   {summary}")
        lines.append(f"   来源：{source} · {item.link}")
    lines.append("内容以原文为准。")
    return "\n".join(lines)
