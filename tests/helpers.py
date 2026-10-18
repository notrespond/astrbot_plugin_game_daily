"""Shared test helpers (no network, no AstrBot runtime)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

from astrbot_plugin_game_daily.config import PluginConfig  # noqa: E402
from astrbot_plugin_game_daily.models import NewsItem  # noqa: E402
from astrbot_plugin_game_daily.news_sources import FetchResult, SourceSpec  # noqa: E402


def make_config(tmp_path: Path, **overrides) -> PluginConfig:
    raw = {
        "rss_sources": [],
        "ai_api_key": "",
        "poster_width": 1200,
        "poster_height": 675,
        "output_dir": str(tmp_path / "posters"),
        "max_news": 6,
        "max_per_source": 2,
        "max_source_items": 8,
        "max_candidates": 60,
        "ai_max_candidates": 30,
    }
    raw.update(overrides)
    return PluginConfig.from_mapping(raw, tmp_path)


def make_item(
    index: int,
    *,
    title: str | None = None,
    summary: str | None = None,
    category: str = "综合",
    source_id: str = "test",
    source: str = "测试来源",
    published_at: datetime | None = None,
    source_weight: float = 1.0,
) -> NewsItem:
    return NewsItem(
        source_id=source_id,
        source=source,
        title=title or f"测试新闻标题 {index}",
        link=f"https://example.com/news/{index}",
        published_at=published_at,
        summary=summary if summary is not None else f"这是第 {index} 条测试新闻的摘要内容。",
        category=category,
        source_weight=source_weight,
        raw_title=title or f"测试新闻标题 {index}",
    )


class FakeFetcher:
    def __init__(self, results: list[FetchResult]):
        self.results = results
        self.calls = 0

    async def fetch(self, sources, now=None, limit_per_source=None):
        self.calls += 1
        results = self.results
        if limit_per_source is not None:
            for result in results:
                result.items = result.items[:limit_per_source]
        return results


def ok_result(source_id: str, name: str, items: list[NewsItem]) -> FetchResult:
    return FetchResult(source_id=source_id, name=name, items=items)


def failed_result(source_id: str, name: str, error: str = "timeout") -> FetchResult:
    return FetchResult(source_id=source_id, name=name, error=error)


def sample_spec() -> SourceSpec:
    return SourceSpec("test", "测试来源", "综合", 1.0, url="https://example.com/feed")


def utc(year=2026, month=10, day=18, hour=0, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
