from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from .helpers import make_item

from astrbot_plugin_game_daily.models import (
    ReportResult,
    ReportStats,
    SourceReport,
    utcnow,
)
from astrbot_plugin_game_daily.storage import Storage


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.storage.close()
        self.tmp.cleanup()

    def test_mark_and_filter_sent(self):
        items = [make_item(1), make_item(2)]
        kept, dropped = self.storage.filter_unsent(items)
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped, 0)
        self.storage.mark_sent(items[:1])
        kept, dropped = self.storage.filter_unsent(items)
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, 1)
        self.assertEqual(kept[0].title, items[1].title)

    def test_sent_matching_by_any_key(self):
        first = make_item(1)
        self.storage.mark_sent([first])
        same_url = make_item(
            2,
            title="Different title",
            summary="Different summary",
        )
        same_url.link = first.link + "?utm_source=test"
        kept, dropped = self.storage.filter_unsent([same_url])
        self.assertEqual(dropped, 1)
        self.assertEqual(kept, [])

    def test_purge_old_sent_records(self):
        item = make_item(1)
        self.storage.mark_sent([item], now=utcnow() - timedelta(days=40))
        self.assertEqual(len(self.storage.find_sent_keys(item.dedupe_keys())), 3)
        removed = self.storage.purge_sent(30)
        self.assertEqual(removed, 3)
        self.assertEqual(self.storage.find_sent_keys(item.dedupe_keys()), set())

    def test_record_and_read_run(self):
        now = utcnow()
        item = make_item(1)
        item.importance = 88.0
        result = ReportResult(
            status="ok",
            items=[item],
            stats=ReportStats(
                total_collected=10,
                total_in_window=6,
                total_after_local_dedupe=4,
                total_selected=1,
                sources_total=2,
                sources_ok=2,
            ),
            source_reports=[
                SourceReport("a", "A", True, 4),
                SourceReport("b", "B", False, 0, "timeout"),
            ],
            started_at=now,
            finished_at=now,
            poster_path="/tmp/x.png",
            caption="caption",
            sent_targets=["aiocqhttp:GroupMessage:1"],
            errors=["AI fallback"],
            ai_used=False,
        )
        self.storage.record_run(result, "scheduled")
        latest = self.storage.latest_run()
        self.assertIsNotNone(latest)
        self.assertEqual(latest["status"], "ok")
        self.assertEqual(latest["total_selected"], 1)
        self.assertEqual(latest["sent_targets"], ["aiocqhttp:GroupMessage:1"])
        self.assertEqual(latest["source_reports"][1]["error"], "timeout")
        self.assertEqual(latest["items"][0]["category"], "综合")

    def test_source_health(self):
        self.storage.record_source_health(
            [
                SourceReport("a", "A", True, 1),
                SourceReport("b", "B", False, 0, "boom"),
            ]
        )
        health = {row["source_id"]: row for row in self.storage.source_health()}
        self.assertEqual(health["a"]["consecutive_failures"], 0)
        self.assertEqual(health["b"]["consecutive_failures"], 1)
        self.storage.record_source_health(
            [SourceReport("b", "B", False, 0, "boom again")]
        )
        health = {row["source_id"]: row for row in self.storage.source_health()}
        self.assertEqual(health["b"]["consecutive_failures"], 2)

    def test_lock(self):
        self.assertTrue(self.storage.try_acquire_lock("run", "owner1"))
        self.assertFalse(self.storage.try_acquire_lock("run", "owner2"))
        self.storage.release_lock("run", "owner2")
        self.assertFalse(self.storage.try_acquire_lock("run", "owner2"))
        self.storage.release_lock("run", "owner1")
        self.assertTrue(self.storage.try_acquire_lock("run", "owner2"))
        self.storage.release_lock("run", "owner2")

    def test_kv(self):
        self.assertIsNone(self.storage.kv_get("missing"))
        self.storage.kv_set("job", "abc")
        self.assertEqual(self.storage.kv_get("job"), "abc")
        self.storage.kv_delete("job")
        self.assertIsNone(self.storage.kv_get("job"))

    def test_stats(self):
        stats = self.storage.stats()
        self.assertEqual(stats["sent_keys"], 0)
        self.assertEqual(stats["runs"], 0)


if __name__ == "__main__":
    unittest.main()
