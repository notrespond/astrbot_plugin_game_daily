from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .helpers import (
    FakeFetcher,
    failed_result,
    make_config,
    make_item,
    ok_result,
)

from astrbot_plugin_game_daily.ai_service import AIEnhanceResult, AIItemResult
from astrbot_plugin_game_daily.news_service import DailyReportService
from astrbot_plugin_game_daily.poster import PosterRenderer
from astrbot_plugin_game_daily.storage import Storage


class FakeAI:
    def __init__(self, result: AIEnhanceResult | None = None):
        self.result = result
        self.calls = 0

    async def enhance(self, items):
        self.calls += 1
        if self.result is None:
            return AIEnhanceResult(used=False, errors=["AI disabled for test"])
        for item in items:
            item.original_id = items.index(item) + 1
        return self.result

    async def ping(self):
        return False, "not used"


def build_service(tmp: Path, results, ai=None, **overrides):
    cfg = make_config(tmp, ai_api_key="test-key", **overrides)
    storage = Storage(cfg.db_path)
    now = datetime(2026, 10, 18, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    poster = PosterRenderer(cfg)
    service = DailyReportService(
        cfg,
        storage,
        FakeFetcher(results),
        ai or FakeAI(),
        poster,
        clock=lambda: now,
    )
    return cfg, storage, service, now


class NewsServiceTests(unittest.TestCase):
    def test_time_filter_missing_time_and_future(self):
        now_utc = datetime(2026, 10, 18, 0, 30, tzinfo=timezone.utc)
        fresh = make_item(1, source_id="a", published_at=now_utc - timedelta(hours=1))
        old = make_item(2, source_id="b", published_at=now_utc - timedelta(hours=30))
        missing = make_item(3, source_id="c", published_at=None)
        future = make_item(4, source_id="d", published_at=now_utc + timedelta(hours=5))
        with tempfile.TemporaryDirectory() as tmp:
            cfg, storage, service, _ = build_service(
                Path(tmp), [ok_result("a", "A", [fresh, old, missing, future])]
            )
            result = asyncio.run(service.generate(make_poster=False))
            self.assertEqual(
                {item.title for item in result.items},
                {fresh.title, missing.title, future.title},
            )
            self.assertTrue(any(item.published_at is None for item in result.items))
            storage.close()

            cfg, storage, service, _ = build_service(
                Path(tmp),
                [ok_result("a", "A", [fresh, old, missing, future])],
                include_missing_time=False,
            )
            result = asyncio.run(
                service.generate(
                    use_sent_filter=False, make_poster=False
                )
            )
            self.assertEqual([item.title for item in result.items], [fresh.title])
            storage.close()

    def test_local_dedupe_by_url_and_title(self):
        now = datetime(2026, 10, 18, 0, 30, tzinfo=timezone.utc)
        first = make_item(
            1,
            title="同一事件的报道",
            source_id="a",
            published_at=now,
        )
        second = make_item(
            2,
            title="同一事件的报道",
            source_id="b",
            published_at=now,
        )
        second.link = first.link + "?utm_source=rss"
        with tempfile.TemporaryDirectory() as tmp:
            _, storage, service, _ = build_service(
                Path(tmp), [ok_result("a", "A", [first, second])]
            )
            result = asyncio.run(
                service.generate(use_sent_filter=False, make_poster=False)
            )
            self.assertEqual(len(result.items), 1)
            self.assertEqual(result.stats.total_after_local_dedupe, 1)
            self.assertEqual(result.items[0].display_source.count("测试来源"), 1)
            storage.close()

    def test_persistent_dedupe_prevents_second_push(self):
        now = datetime(2026, 10, 18, 0, 30, tzinfo=timezone.utc)
        items = [make_item(i, source_id=f"source-{i}", published_at=now) for i in range(1, 5)]
        with tempfile.TemporaryDirectory() as tmp:
            _, storage, service, _ = build_service(
                Path(tmp), [ok_result("a", "A", items)]
            )
            first = asyncio.run(
                service.generate(use_sent_filter=True, make_poster=False)
            )
            self.assertEqual(len(first.items), 4)
            asyncio.run(service.mark_sent(first))
            second = asyncio.run(
                service.generate(use_sent_filter=True, make_poster=False)
            )
            self.assertEqual(second.status, "no_news")
            self.assertEqual(second.stats.total_after_sent_filter, 0)
            storage.close()

    def test_no_news_when_sources_ok_but_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, storage, service, _ = build_service(
                Path(tmp), [ok_result("a", "A", [])]
            )
            result = asyncio.run(
                service.generate(
                    use_sent_filter=False,
                    make_poster=True,
                    poster_when_empty=True,
                )
            )
            self.assertEqual(result.status, "no_news")
            self.assertIsNotNone(result.poster_path)
            self.assertTrue(Path(result.poster_path).is_file())
            storage.close()

    def test_all_sources_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, storage, service, _ = build_service(
                Path(tmp),
                [
                    failed_result("a", "A", "timeout"),
                    failed_result("b", "B", "403"),
                ],
            )
            result = asyncio.run(service.generate(make_poster=False))
            self.assertEqual(result.status, "failed")
            self.assertTrue(result.fatal_source_error)
            self.assertIn("所有新闻源均不可用", " ".join(result.errors))
            storage.close()

    def test_partial_source_failure_keeps_working(self):
        now = datetime(2026, 10, 18, 0, 30, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            _, storage, service, _ = build_service(
                Path(tmp),
                [
                    failed_result("a", "A", "timeout"),
                    ok_result(
                        "b",
                        "B",
                        [make_item(1, source_id="b", published_at=now)],
                    ),
                ],
            )
            result = asyncio.run(
                service.generate(use_sent_filter=False, make_poster=False)
            )
            self.assertEqual(result.status, "ok")
            self.assertEqual(len(result.items), 1)
            self.assertEqual(result.stats.sources_ok, 1)
            self.assertFalse(result.fatal_source_error)
            storage.close()

    def test_ai_failure_falls_back_to_rule_mode(self):
        now = datetime(2026, 10, 18, 0, 30, tzinfo=timezone.utc)
        items = [make_item(i, source_id=f"s{i}", published_at=now) for i in range(1, 5)]
        ai = FakeAI(AIEnhanceResult(used=False, errors=["AI timeout"]))
        with tempfile.TemporaryDirectory() as tmp:
            _, storage, service, _ = build_service(
                Path(tmp), [ok_result("a", "A", items)], ai=ai
            )
            result = asyncio.run(
                service.generate(use_sent_filter=False, make_poster=False)
            )
            self.assertEqual(result.status, "ok")
            self.assertFalse(result.ai_used)
            self.assertEqual(len(result.items), 4)
            self.assertEqual(result.items[0].ai_title, "")
            self.assertIn("AI timeout", " ".join(result.errors))
            storage.close()

    def test_ai_success_applies_titles_and_merges_duplicates(self):
        now = datetime(2026, 10, 18, 0, 30, tzinfo=timezone.utc)
        items = [make_item(i, source_id=f"s{i}", published_at=now) for i in range(1, 5)]
        ai = FakeAI(
            AIEnhanceResult(
                used=True,
                items={
                    1: AIItemResult(1, "中文标题一", "中文摘要一，信息完整。", "更新", 90),
                    2: AIItemResult(2, "中文标题二", "中文摘要二，信息完整。", "发售", 80, 1),
                    3: AIItemResult(3, "中文标题三", "中文摘要三，信息完整。", "行业", 70),
                    4: AIItemResult(4, "中文标题四", "中文摘要四，信息完整。", "电竞", 60),
                },
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            _, storage, service, _ = build_service(
                Path(tmp), [ok_result("a", "A", items)], ai=ai
            )
            result = asyncio.run(
                service.generate(use_sent_filter=False, make_poster=False)
            )
            self.assertTrue(result.ai_used)
            titles = {item.display_title for item in result.items}
            self.assertIn("中文标题一", titles)
            self.assertNotIn("中文标题二", titles)
            self.assertEqual(len(result.items), 3)
            self.assertIn("测试来源", result.items[0].display_source)
            storage.close()

    def test_selection_limits_and_category_diversity(self):
        now = datetime(2026, 10, 18, 0, 30, tzinfo=timezone.utc)
        items = [
            make_item(
                i,
                title=f"新闻 {i}",
                category="综合",
                source_id=f"s{i}",
                published_at=now - timedelta(minutes=i),
            )
            for i in range(1, 11)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            _, storage, service, _ = build_service(
                Path(tmp), [ok_result("a", "A", items)]
            )
            result = asyncio.run(
                service.generate(use_sent_filter=False, make_poster=False)
            )
            self.assertEqual(len(result.items), 6)
            self.assertEqual(result.stats.total_selected, 6)
            storage.close()

    def test_poster_generated_for_real_items(self):
        now = datetime(2026, 10, 18, 0, 30, tzinfo=timezone.utc)
        items = [make_item(i, source_id=f"s{i}", published_at=now) for i in range(1, 7)]
        with tempfile.TemporaryDirectory() as tmp:
            _, storage, service, _ = build_service(
                Path(tmp), [ok_result("a", "A", items)]
            )
            result = asyncio.run(
                service.generate(use_sent_filter=False, make_poster=True)
            )
            self.assertIsNotNone(result.poster_path)
            self.assertTrue(Path(result.poster_path).is_file())
            self.assertGreater(Path(result.poster_path).stat().st_size, 10_000)
            storage.close()

    def test_exclusive_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, storage, service, _ = build_service(
                Path(tmp), [ok_result("a", "A", [])]
            )

            async def work():
                async with service.exclusive("manual"):
                    await asyncio.sleep(0.2)

            async def scenario():
                task = asyncio.create_task(work())
                await asyncio.sleep(0.02)
                with self.assertRaises(Exception):
                    async with service.exclusive("scheduled"):
                        pass
                await task

            asyncio.run(scenario())
            storage.close()


if __name__ == "__main__":
    unittest.main()
