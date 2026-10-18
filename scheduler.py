"""AstrBot-native scheduling lifecycle for Game Daily.

AstrBot 4.29 exposes ``context.cron_manager`` (APScheduler backed).  We register
exactly one basic cron job on plugin activation, remove stale copies from
previous loads, and delete our job on unload/reload.

Manual commands do not create jobs; they call the same report service directly.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Awaitable, Callable

from .config import PLUGIN_NAME, PluginConfig
from .models import ReportResult
from .storage import Storage

log = logging.getLogger(__name__)

JOB_NAME = "[GameDaily] 每日游戏资讯"
JOB_PREFIX = "[GameDaily]"


class DailyScheduler:
    def __init__(
        self,
        cron_manager,
        config: PluginConfig,
        storage: Storage,
        run_callback: Callable[[], Awaitable[ReportResult | None]],
        logger=None,
    ) -> None:
        self.cron_manager = cron_manager
        self.config = config
        self.storage = storage
        self.run_callback = run_callback
        self.log = logger or log
        self._job_id: str | None = None
        self._started = False
        self._closing = False
        self._start_lock = asyncio.Lock()
        self._run_lock = asyncio.Lock()
        self.registration_error = ""
        self.current_attempt = 0

    @property
    def job_id(self) -> str | None:
        return self._job_id

    @property
    def running(self) -> bool:
        return self._run_lock.locked()

    async def start(self) -> None:
        async with self._start_lock:
            if self._started:
                return
            self._closing = False
            if self.cron_manager is None:
                self.registration_error = "AstrBot 未提供 cron_manager，无法注册定时任务"
                self.log.error(self.registration_error)
                return
            owner = f"setup:{uuid.uuid4().hex}"
            acquired = await asyncio.to_thread(
                self.storage.try_acquire_lock, "scheduler_setup", owner, 120
            )
            if not acquired:
                self.registration_error = "另一个实例正在注册定时任务"
                self.log.warning(self.registration_error)
                return
            try:
                await self._remove_stale_jobs()
                if not self.config.enabled:
                    self.log.info("每日推送已关闭（enabled=false）")
                    await asyncio.to_thread(self.storage.kv_delete, "scheduler_job_id")
                    self._started = True
                    return
                job = await self.cron_manager.add_basic_job(
                    name=JOB_NAME,
                    cron_expression=self.config.effective_cron,
                    handler=self._handle_scheduled,
                    description=(
                        "游戏资讯日报：采集 → AI 摘要 → 海报 → 推送到配置目标"
                    ),
                    timezone=self.config.timezone_name,
                    payload={
                        "plugin": PLUGIN_NAME,
                        "kind": "game_daily",
                    },
                    enabled=True,
                    # The plugin registers its job on every load; letting the
                    # core resync a stale row after the plugin is removed can
                    # leave a handler-less task behind.
                    persistent=False,
                )
                self._job_id = getattr(job, "job_id", None)
                if self._job_id:
                    await asyncio.to_thread(
                        self.storage.kv_set, "scheduler_job_id", self._job_id
                    )
                self.registration_error = ""
                self._started = True
                next_run = self._next_run_text()
                self.log.info(
                    "定时推送已注册：%s（%s，%s）下次：%s",
                    JOB_NAME,
                    self.config.effective_cron,
                    self.config.timezone_name,
                    next_run or "未知",
                )
            except Exception as exc:  # noqa: BLE001
                self.registration_error = (
                    f"注册定时任务失败：{type(exc).__name__}: {exc}"
                )
                self.log.error(self.registration_error, exc_info=True)
            finally:
                try:
                    await asyncio.to_thread(
                        self.storage.release_lock, "scheduler_setup", owner
                    )
                except Exception:
                    self.log.warning("释放调度器注册锁失败", exc_info=True)

    async def stop(self) -> None:
        self._closing = True
        if self.cron_manager is None:
            return
        job_ids: set[str] = set()
        if self._job_id:
            job_ids.add(self._job_id)
        stored = await asyncio.to_thread(self.storage.kv_get, "scheduler_job_id")
        if stored:
            job_ids.add(stored)
        for job_id in job_ids:
            try:
                await self.cron_manager.delete_job(job_id)
            except Exception:
                self.log.warning("删除定时任务 %s 失败", job_id, exc_info=True)
        # Also remove any duplicate left behind by a failed reload.
        try:
            jobs = await self.cron_manager.list_jobs("basic")
            for job in jobs:
                if self._is_ours(job):
                    await self.cron_manager.delete_job(job.job_id)
        except Exception:
            self.log.warning("清理定时任务失败", exc_info=True)
        try:
            await asyncio.to_thread(self.storage.kv_delete, "scheduler_job_id")
        except Exception:
            pass
        self._job_id = None
        self._started = False
        self.log.info("定时推送已注销")

    async def _remove_stale_jobs(self) -> None:
        try:
            jobs = await self.cron_manager.list_jobs("basic")
        except Exception:
            self.log.warning("读取已有定时任务失败", exc_info=True)
            return
        for job in jobs:
            if not self._is_ours(job):
                continue
            try:
                await self.cron_manager.delete_job(job.job_id)
                self.log.info("清理重复的旧定时任务：%s", job.job_id)
            except Exception:
                self.log.warning("清理旧定时任务 %s 失败", job.job_id, exc_info=True)

    @staticmethod
    def _is_ours(job) -> bool:
        name = str(getattr(job, "name", "") or "")
        if name == JOB_NAME or name.startswith(JOB_PREFIX):
            return True
        payload = getattr(job, "payload", None)
        if isinstance(payload, str):
            try:
                import json

                payload = json.loads(payload)
            except Exception:
                payload = None
        return isinstance(payload, dict) and payload.get("plugin") == PLUGIN_NAME

    async def _handle_scheduled(self, **kwargs) -> ReportResult | None:
        _ = kwargs  # AstrBot passes the cron payload as keyword arguments.
        if self._closing:
            self.log.info("插件正在卸载，跳过本次定时推送")
            return None
        if self._run_lock.locked():
            self.log.warning("上一次日报任务仍在运行，跳过本次定时触发")
            return None
        async with self._run_lock:
            attempts = self.config.retry_times + 1
            last_error = ""
            for attempt in range(1, attempts + 1):
                self.current_attempt = attempt
                try:
                    self.log.info(
                        "定时日报开始（第 %s/%s 次）", attempt, attempts
                    )
                    result = await self.run_callback()
                    if result is None:
                        return None
                    if result.status in {"ok", "no_news"}:
                        return result
                    # A partial delivery must not be retried: that would
                    # duplicate messages in targets that already received it.
                    if result.items and result.sent_targets:
                        return result
                    if attempt < attempts:
                        self.log.warning(
                            "定时日报失败，准备重试：%s",
                            "; ".join(result.errors[:2]),
                        )
                    else:
                        return result
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    last_error = f"{type(exc).__name__}: {exc}"
                    self.log.error("定时日报异常：%s", last_error, exc_info=True)
                    if attempt >= attempts:
                        break
                if self._closing:
                    return None
                await asyncio.sleep(min(30.0, 8.0 * attempt))
            self.log.error("定时日报重试结束仍未成功：%s", last_error)
            return None

    def _next_run_text(self) -> str:
        if not self._job_id or self.cron_manager is None:
            return ""
        try:
            next_run = self.cron_manager.get_next_run_time(self._job_id)
        except Exception:
            return ""
        if next_run is None:
            return ""
        return next_run.astimezone(self.config.timezone).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    def status(self) -> dict:
        next_run = None
        if self._job_id and self.cron_manager is not None:
            try:
                value = self.cron_manager.get_next_run_time(self._job_id)
                if value is not None:
                    next_run = value.astimezone(self.config.timezone)
            except Exception:
                next_run = None
        return {
            "enabled": self.config.enabled,
            "registered": bool(self._job_id and not self.registration_error),
            "job_id": self._job_id,
            "next_run": next_run,
            "cron": self.config.effective_cron,
            "timezone": self.config.timezone_name,
            "running": self.running,
            "error": self.registration_error,
        }


def format_next_run(value: datetime | None) -> str:
    if value is None:
        return "未安排"
    return value.strftime("%Y-%m-%d %H:%M:%S")
