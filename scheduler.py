"""AstrBot-native scheduling lifecycle for Game Daily.

Two native cron jobs are registered:
* 30 minutes before ``push_time``: generate the news report, AI summary and
  poster, then persist it as a prepared report.
* at ``push_time``: send the already prepared poster to the configured targets.

This keeps the actual push at the configured minute instead of spending the
first minute(s) fetching RSS, calling the AI and rendering.  Both jobs are
removed on unload/reload; stale copies from previous versions are cleaned up.

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

JOB_PREFIX = "[GameDaily]"
JOB_NAME_PREPARE = "[GameDaily] 预生成游戏日报"
JOB_NAME_SEND = "[GameDaily] 发送游戏日报"
PREPARE_LEAD_MINUTES = 30

KV_PREPARE_JOB = "scheduler_prepare_job_id"
KV_SEND_JOB = "scheduler_send_job_id"


class DailyScheduler:
    def __init__(
        self,
        cron_manager,
        config: PluginConfig,
        storage: Storage,
        prepare_callback: Callable[[], Awaitable[ReportResult | None]],
        send_callback: Callable[[], Awaitable[ReportResult | None]],
        logger=None,
    ) -> None:
        self.cron_manager = cron_manager
        self.config = config
        self.storage = storage
        self.prepare_callback = prepare_callback
        self.send_callback = send_callback
        self.log = logger or log
        self._prepare_job_id: str | None = None
        self._send_job_id: str | None = None
        self._started = False
        self._closing = False
        self._start_lock = asyncio.Lock()
        self._prepare_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self.registration_error = ""
        self.current_attempt = 0

    @property
    def job_id(self) -> str | None:
        return self._send_job_id

    @property
    def running(self) -> bool:
        return self._prepare_lock.locked() or self._send_lock.locked()

    @property
    def prepare_cron(self) -> str:
        try:
            hour_text, minute_text = self.config.push_time.split(":", 1)
            total = (int(hour_text) * 60 + int(minute_text) - PREPARE_LEAD_MINUTES) % 1440
        except Exception:
            total = (8 * 60 + 30 - PREPARE_LEAD_MINUTES) % 1440
        return f"{total % 60} {total // 60} * * *"

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
                    await self._clear_job_ids()
                    self._started = True
                    return

                prepare_job = await self.cron_manager.add_basic_job(
                    name=JOB_NAME_PREPARE,
                    cron_expression=self.prepare_cron,
                    handler=self._handle_prepare,
                    description="提前生成游戏日报海报，等待到点发送",
                    timezone=self.config.timezone_name,
                    payload={"plugin": PLUGIN_NAME, "kind": "prepare"},
                    enabled=True,
                    persistent=False,
                )
                send_job = await self.cron_manager.add_basic_job(
                    name=JOB_NAME_SEND,
                    cron_expression=self.config.effective_cron,
                    handler=self._handle_send,
                    description="将预生成的游戏日报发送到配置目标",
                    timezone=self.config.timezone_name,
                    payload={"plugin": PLUGIN_NAME, "kind": "send"},
                    enabled=True,
                    persistent=False,
                )
                self._prepare_job_id = getattr(prepare_job, "job_id", None)
                self._send_job_id = getattr(send_job, "job_id", None)
                await asyncio.to_thread(
                    self.storage.kv_set, KV_PREPARE_JOB, self._prepare_job_id or ""
                )
                await asyncio.to_thread(
                    self.storage.kv_set, KV_SEND_JOB, self._send_job_id or ""
                )
                self.registration_error = ""
                self._started = True
                self.log.info(
                    "定时任务已注册：预生成 %s（%s），发送 %s（%s），时区 %s；下次预生成 %s，下次发送 %s",
                    JOB_NAME_PREPARE,
                    self.prepare_cron,
                    JOB_NAME_SEND,
                    self.config.effective_cron,
                    self.config.timezone_name,
                    self._next_run_text(self._prepare_job_id) or "未知",
                    self._next_run_text(self._send_job_id) or "未知",
                )
            except Exception as exc:  # noqa: BLE001
                self.registration_error = (
                    f"注册定时任务失败：{type(exc).__name__}: {exc}"
                )
                self.log.error(self.registration_error, exc_info=True)
                # Clean up a half-registered pair.
                try:
                    await self._remove_stale_jobs()
                except Exception:
                    pass
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
        for value in (self._prepare_job_id, self._send_job_id):
            if value:
                job_ids.add(value)
        for key in (KV_PREPARE_JOB, KV_SEND_JOB):
            stored = await asyncio.to_thread(self.storage.kv_get, key)
            if stored:
                job_ids.add(stored)
        for job_id in job_ids:
            try:
                await self.cron_manager.delete_job(job_id)
            except Exception:
                self.log.warning("删除定时任务 %s 失败", job_id, exc_info=True)
        try:
            jobs = await self.cron_manager.list_jobs("basic")
            for job in jobs:
                if self._is_ours(job):
                    await self.cron_manager.delete_job(job.job_id)
        except Exception:
            self.log.warning("清理定时任务失败", exc_info=True)
        await self._clear_job_ids()
        self._prepare_job_id = None
        self._send_job_id = None
        self._started = False
        self.log.info("定时推送已注销")

    async def _clear_job_ids(self) -> None:
        for key in (KV_PREPARE_JOB, KV_SEND_JOB, "scheduler_job_id"):
            try:
                await asyncio.to_thread(self.storage.kv_delete, key)
            except Exception:
                pass

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
        if name == JOB_NAME_PREPARE or name == JOB_NAME_SEND or name.startswith(JOB_PREFIX):
            return True
        payload = getattr(job, "payload", None)
        if isinstance(payload, str):
            try:
                import json

                payload = json.loads(payload)
            except Exception:
                payload = None
        return isinstance(payload, dict) and payload.get("plugin") == PLUGIN_NAME

    async def _handle_prepare(self, **kwargs) -> ReportResult | None:
        _ = kwargs
        return await self._run_with_retries(
            label="预生成日报",
            callback=self.prepare_callback,
            lock=self._prepare_lock,
        )

    async def _handle_send(self, **kwargs) -> ReportResult | None:
        _ = kwargs
        return await self._run_with_retries(
            label="发送日报",
            callback=self.send_callback,
            lock=self._send_lock,
        )

    async def _run_with_retries(
        self,
        *,
        label: str,
        callback: Callable[[], Awaitable[ReportResult | None]],
        lock: asyncio.Lock,
    ) -> ReportResult | None:
        if self._closing:
            self.log.info("插件正在卸载，跳过本次%s", label)
            return None
        if lock.locked():
            self.log.warning("%s仍在运行，跳过本次触发", label)
            return None
        async with lock:
            attempts = self.config.retry_times + 1
            last_error = ""
            for attempt in range(1, attempts + 1):
                self.current_attempt = attempt
                try:
                    self.log.info("%s开始（第 %s/%s 次）", label, attempt, attempts)
                    result = await callback()
                    if result is None:
                        return None
                    if result.status in {"ok", "no_news"}:
                        return result
                    if result.items and result.sent_targets:
                        return result
                    if attempt < attempts:
                        self.log.warning(
                            "%s失败，准备重试：%s",
                            label,
                            "; ".join(result.errors[:2]),
                        )
                    else:
                        return result
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    last_error = f"{type(exc).__name__}: {exc}"
                    self.log.error("%s异常：%s", label, last_error, exc_info=True)
                    if attempt >= attempts:
                        break
                if self._closing:
                    return None
                await asyncio.sleep(min(30.0, 8.0 * attempt))
            self.log.error("%s重试结束仍未成功：%s", label, last_error)
            return None

    def _next_run_text(self, job_id: str | None) -> str:
        if not job_id or self.cron_manager is None:
            return ""
        try:
            next_run = self.cron_manager.get_next_run_time(job_id)
        except Exception:
            return ""
        if next_run is None:
            return ""
        return next_run.astimezone(self.config.timezone).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    def _next_run(self, job_id: str | None):
        if not job_id or self.cron_manager is None:
            return None
        try:
            value = self.cron_manager.get_next_run_time(job_id)
            return value.astimezone(self.config.timezone) if value else None
        except Exception:
            return None

    def status(self) -> dict:
        return {
            "enabled": self.config.enabled,
            "registered": bool(
                self._prepare_job_id
                and self._send_job_id
                and not self.registration_error
            ),
            "job_id": self._send_job_id,
            "prepare_job_id": self._prepare_job_id,
            "next_run": self._next_run(self._send_job_id),
            "next_prepare": self._next_run(self._prepare_job_id),
            "cron": self.config.effective_cron,
            "prepare_cron": self.prepare_cron,
            "timezone": self.config.timezone_name,
            "running": self.running,
            "error": self.registration_error,
        }


def format_next_run(value: datetime | None) -> str:
    if value is None:
        return "未安排"
    return value.strftime("%Y-%m-%d %H:%M:%S")
