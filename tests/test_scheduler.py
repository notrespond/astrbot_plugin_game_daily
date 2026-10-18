from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .helpers import make_config

from astrbot_plugin_game_daily.models import ReportResult, ReportStats
from astrbot_plugin_game_daily.scheduler import DailyScheduler, JOB_NAME
from astrbot_plugin_game_daily.storage import Storage


@dataclass
class FakeJob:
    job_id: str
    name: str
    payload: dict
    enabled: bool = True
    persistent: bool = True


class FakeCronManager:
    def __init__(self):
        self.jobs: dict[str, FakeJob] = {}
        self.handlers: dict[str, object] = {}
        self.added: list[dict] = []
        self.deleted: list[str] = []
        self._counter = 0

    async def add_basic_job(self, **kwargs):
        self._counter += 1
        job_id = f"job-{self._counter}"
        job = FakeJob(
            job_id=job_id,
            name=kwargs["name"],
            payload=dict(kwargs.get("payload") or {}),
        )
        self.jobs[job_id] = job
        self.handlers[job_id] = kwargs["handler"]
        self.added.append(kwargs)
        return job

    async def list_jobs(self, job_type=None):
        return list(self.jobs.values())

    async def delete_job(self, job_id):
        self.deleted.append(job_id)
        self.jobs.pop(job_id, None)
        self.handlers.pop(job_id, None)

    def get_next_run_time(self, job_id):
        if job_id not in self.jobs:
            return None
        return datetime(2026, 10, 19, 0, 30, tzinfo=timezone.utc)


def make_result(status="ok") -> ReportResult:
    now = datetime.now(timezone.utc)
    return ReportResult(
        status=status,
        items=[],
        stats=ReportStats(),
        source_reports=[],
        started_at=now,
        finished_at=now,
    )


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.tmp.name) / "s.db")
        self.cron = FakeCronManager()

    def tearDown(self):
        self.storage.close()
        self.tmp.cleanup()

    def _scheduler(self, callback=None, **overrides):
        cfg = make_config(Path(self.tmp.name), **overrides)
        return DailyScheduler(
            self.cron, cfg, self.storage, callback or self._ok_callback
        )

    async def _ok_callback(self):
        return make_result("ok")

    def test_register_once_and_cleanup_stale(self):
        self.cron.jobs["old"] = FakeJob(
            "old", JOB_NAME, {"plugin": "astrbot_plugin_game_daily"}
        )
        self.cron.jobs["other"] = FakeJob("other", "另一个插件", {"plugin": "other"})
        scheduler = self._scheduler()
        asyncio.run(scheduler.start())
        self.assertIn("old", self.cron.deleted)
        self.assertNotIn("other", self.cron.deleted)
        self.assertEqual(len(self.cron.added), 1)
        asyncio.run(scheduler.start())
        self.assertEqual(len(self.cron.added), 1)
        status = scheduler.status()
        self.assertTrue(status["registered"])
        self.assertIsNotNone(status["next_run"])
        asyncio.run(scheduler.stop())
        self.assertEqual(scheduler.status()["job_id"], None)
        self.assertIn("other", self.cron.jobs)

    def test_disabled_does_not_register(self):
        scheduler = self._scheduler(enabled=False)
        asyncio.run(scheduler.start())
        self.assertEqual(len(self.cron.added), 0)
        self.assertFalse(scheduler.status()["enabled"])
        asyncio.run(scheduler.stop())

    def test_cron_payload_is_accepted(self):
        calls = []

        async def callback():
            calls.append(1)
            return make_result("ok")

        scheduler = self._scheduler(callback=callback)
        asyncio.run(scheduler.start())
        job_id = next(iter(self.cron.handlers))
        handler = self.cron.handlers[job_id]
        result = asyncio.run(handler(plugin="astrbot_plugin_game_daily", kind="game_daily"))
        self.assertEqual(result.status, "ok")
        self.assertEqual(calls, [1])
        asyncio.run(scheduler.stop())

    def test_retry_on_failed_result(self):
        calls = []

        async def callback():
            calls.append(1)
            return make_result("ok" if len(calls) >= 2 else "failed")

        scheduler = self._scheduler(callback=callback, retry_times=1)
        asyncio.run(scheduler.start())
        job_id = next(iter(self.cron.handlers))
        result = asyncio.run(self.cron.handlers[job_id]())
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(calls), 2)
        asyncio.run(scheduler.stop())

    def test_concurrent_trigger_is_skipped(self):
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def callback():
            calls.append(1)
            started.set()
            await release.wait()
            return make_result("ok")

        scheduler = self._scheduler(callback=callback)

        async def scenario():
            await scheduler.start()
            job_id = next(iter(self.cron.handlers))
            first = asyncio.create_task(self.cron.handlers[job_id]())
            await started.wait()
            second = await self.cron.handlers[job_id]()
            self.assertIsNone(second)
            release.set()
            await first
            await scheduler.stop()

        asyncio.run(scenario())
        self.assertEqual(len(calls), 1)

    def test_stop_removes_job(self):
        scheduler = self._scheduler()
        asyncio.run(scheduler.start())
        self.assertEqual(len(self.cron.jobs), 1)
        asyncio.run(scheduler.stop())
        self.assertEqual(len(self.cron.jobs), 0)


if __name__ == "__main__":
    unittest.main()
