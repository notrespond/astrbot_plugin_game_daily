from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .helpers import make_config

from astrbot_plugin_game_daily.models import ReportResult, ReportStats
from astrbot_plugin_game_daily.scheduler import (
    DailyScheduler,
    JOB_NAME_PREPARE,
    JOB_NAME_SEND,
)
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

    def _scheduler(self, callback=None, prepare_callback=None, **overrides):
        cfg = make_config(Path(self.tmp.name), **overrides)
        return DailyScheduler(
            self.cron,
            cfg,
            self.storage,
            prepare_callback or self._ok_callback,
            callback or self._ok_callback,
        )

    async def _ok_callback(self):
        return make_result("ok")

    def _send_handler(self):
        for job_id, job in self.cron.jobs.items():
            if job.name == JOB_NAME_SEND:
                return self.cron.handlers[job_id]
        raise AssertionError("send job not found")

    def test_register_once_and_cleanup_stale(self):
        self.cron.jobs["old"] = FakeJob(
            "old", "[GameDaily] 每日游戏资讯", {"plugin": "astrbot_plugin_game_daily"}
        )
        self.cron.jobs["other"] = FakeJob("other", "另一个插件", {"plugin": "other"})
        scheduler = self._scheduler(push_time="08:00")
        asyncio.run(scheduler.start())
        self.assertIn("old", self.cron.deleted)
        self.assertNotIn("other", self.cron.deleted)
        self.assertEqual(len(self.cron.added), 2)
        names = {job["name"] for job in self.cron.added}
        self.assertEqual(names, {JOB_NAME_PREPARE, JOB_NAME_SEND})
        asyncio.run(scheduler.start())
        self.assertEqual(len(self.cron.added), 2)
        status = scheduler.status()
        self.assertTrue(status["registered"])
        self.assertIsNotNone(status["next_run"])
        self.assertIsNotNone(status["next_prepare"])
        self.assertEqual(status["prepare_cron"], "30 7 * * *")
        asyncio.run(scheduler.stop())
        self.assertEqual(scheduler.status()["job_id"], None)
        self.assertIn("other", self.cron.jobs)

    def test_disabled_does_not_register(self):
        scheduler = self._scheduler(enabled=False)
        asyncio.run(scheduler.start())
        self.assertEqual(len(self.cron.added), 0)
        self.assertFalse(scheduler.status()["enabled"])
        asyncio.run(scheduler.stop())

    def test_prepare_and_send_payloads_are_accepted(self):
        prepare_calls = []
        send_calls = []

        async def prepare_callback():
            prepare_calls.append(1)
            return make_result("ok")

        async def send_callback():
            send_calls.append(1)
            return make_result("ok")

        scheduler = self._scheduler(
            callback=send_callback, prepare_callback=prepare_callback
        )
        asyncio.run(scheduler.start())
        for job_id, job in self.cron.jobs.items():
            handler = self.cron.handlers[job_id]
            if job.name == JOB_NAME_PREPARE:
                asyncio.run(handler(plugin="astrbot_plugin_game_daily", kind="prepare"))
            else:
                asyncio.run(handler(plugin="astrbot_plugin_game_daily", kind="send"))
        self.assertEqual(prepare_calls, [1])
        self.assertEqual(send_calls, [1])
        asyncio.run(scheduler.stop())

    def test_retry_on_failed_send_result(self):
        calls = []

        async def callback():
            calls.append(1)
            return make_result("ok" if len(calls) >= 2 else "failed")

        scheduler = self._scheduler(callback=callback, retry_times=1)
        asyncio.run(scheduler.start())
        result = asyncio.run(self._send_handler()())
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
            handler = self._send_handler()
            first = asyncio.create_task(handler())
            await started.wait()
            second = await handler()
            self.assertIsNone(second)
            release.set()
            await first
            await scheduler.stop()

        asyncio.run(scenario())
        self.assertEqual(len(calls), 1)

    def test_stop_removes_both_jobs(self):
        scheduler = self._scheduler()
        asyncio.run(scheduler.start())
        self.assertEqual(len(self.cron.jobs), 2)
        asyncio.run(scheduler.stop())
        self.assertEqual(len(self.cron.jobs), 0)


if __name__ == "__main__":
    unittest.main()
