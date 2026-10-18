"""AstrBot entry point for the Game Daily plugin.

Commands:
    /gamedaily            立即采集并推送日报（当前会话）
    /gamedaily_preview    生成海报但不发送
    /gamedaily_status     查看定时任务、上次结果与新闻数量
    /gamedaily_test       测试新闻源、AI API 与海报生成
    /gamedaily_target     显示当前会话的 UMO（用于配置推送目标）
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

from .ai_service import AIService
from .config import PLUGIN_NAME, PluginConfig
from .models import ReportResult, ReportStats
from .news_service import DailyReportService, RunBusyError, text_fallback
from .news_sources import NewsFetcher, build_sources
from .poster import PosterError, PosterRenderer
from .scheduler import DailyScheduler, format_next_run
from .storage import Storage

PLUGIN_VERSION = "1.0.0"


def _resolve_data_dir() -> Path:
    override = os.environ.get("GAMEDAILY_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        return Path(get_astrbot_plugin_data_path()).resolve() / PLUGIN_NAME
    except Exception:
        return Path(__file__).resolve().parent / "data"


def _fmt_time(value: str | None, tz) -> str:
    if not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return value


class GameDailyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self._raw_config = config or {}
        self.data_dir = _resolve_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cfg = PluginConfig.from_mapping(
            self._raw_config, self.data_dir, self.logger
        )
        try:
            self.storage = Storage(self.cfg.db_path, self.logger)
        except Exception:
            self.logger.exception("无法打开插件数据库，回退到临时目录")
            fallback = Path(tempfile.gettempdir()) / "astrbot_game_daily"
            self.storage = Storage(fallback / "gamedaily.db", self.logger)
            self.cfg.warnings.append(
                f"数据库无法写入 {self.cfg.db_path}，已回退到 {fallback}"
            )
        self.fetcher = NewsFetcher(self.cfg, self.logger)
        self.ai = AIService(self.cfg, self.logger)
        self.ai.set_provider_chat(self._astrbot_provider_chat)
        self._refresh_provider_availability()
        self.poster = PosterRenderer(self.cfg, self.logger)
        self.service = DailyReportService(
            self.cfg,
            self.storage,
            self.fetcher,
            self.ai,
            self.poster,
            self.logger,
        )
        self.scheduler = DailyScheduler(
            getattr(context, "cron_manager", None),
            self.cfg,
            self.storage,
            self._scheduled_pipeline,
            self.logger,
        )
        self._init_lock = asyncio.Lock()
        self._initialized = False
        for warning in self.cfg.warnings:
            self.logger.warning(warning)
        self.logger.info(
            "Game Daily 配置：推送=%s %s %s，目标=%s，AI=%s，新闻源=%s，海报=%sx%s",
            self.cfg.enabled,
            self.cfg.push_time,
            self.cfg.timezone_name,
            len(self.cfg.push_targets),
            (
                "on"
                if self.cfg.ai_available
                else (
                    "auto"
                    if self.cfg.ai_enabled
                    and self.cfg.ai_use_astrbot_provider
                    and not self.cfg.ai_api_key
                    else "fallback"
                )
            ),
            len(build_sources(self.cfg)),
            self.cfg.poster_width,
            self.cfg.poster_height,
        )

    def _refresh_provider_availability(self) -> None:
        if not self.cfg.ai_use_astrbot_provider:
            self.cfg.ai_provider_available = False
            return
        try:
            self.cfg.ai_provider_available = bool(self.context.get_all_providers())
        except Exception:
            self.cfg.ai_provider_available = False
            self.logger.warning(
                "检测 AstrBot 聊天模型失败，将使用规则模式", exc_info=True
            )

    async def _astrbot_provider_chat(self, messages: list[dict]) -> str:
        """Use AstrBot's configured chat model when no external Key exists."""

        provider = None
        if self.cfg.ai_provider_id:
            provider = self.context.get_provider_by_id(self.cfg.ai_provider_id)
            if provider is None:
                self.logger.warning(
                    "配置的 AstrBot 模型 %s 不存在，改用当前默认模型",
                    self.cfg.ai_provider_id,
                )
        if provider is None:
            provider = await self.context.get_using_provider_async()
        if provider is None:
            raise RuntimeError("AstrBot 没有可用的聊天模型")
        system_prompt = "\n\n".join(
            str(message.get("content", ""))
            for message in messages
            if message.get("role") == "system"
        )
        prompt = "\n\n".join(
            str(message.get("content", ""))
            for message in messages
            if message.get("role") != "system"
        )
        response = await self.context.llm_generate(
            chat_provider_id=provider.meta().id,
            prompt=prompt,
            system_prompt=system_prompt,
        )
        return response.completion_text or ""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        async with self._init_lock:
            if self._initialized:
                return
            self._initialized = True
        self._refresh_provider_availability()
        try:
            await self.scheduler.start()
        except Exception:
            self.logger.error("启动定时任务失败", exc_info=True)
        try:
            await self.service.purge_old_records()
        except Exception:
            self.logger.warning("启动清理失败", exc_info=True)

    @filter.on_astrbot_loaded()
    async def _on_astrbot_loaded(self) -> None:
        self._refresh_provider_availability()
        self.logger.info(
            "AstrBot 聊天模型可用：%s（AI 模式：%s）",
            self.cfg.ai_provider_available,
            "on" if self.cfg.ai_available else "fallback",
        )
        # setTimeout-like retry for environments where initialize() ran before
        # the core cron manager was ready.
        if not self.scheduler.status().get("registered"):
            try:
                await self.scheduler.start()
            except Exception:
                self.logger.error("AstrBot 加载完成后注册定时任务失败", exc_info=True)

    async def terminate(self) -> None:
        self.logger.info("Game Daily 正在卸载…")
        try:
            await self.scheduler.stop()
        except Exception:
            self.logger.warning("停止定时任务失败", exc_info=True)
        for _ in range(20):
            if not self.scheduler.running:
                break
            await asyncio.sleep(0.25)
        if self.scheduler.running:
            self.logger.warning(
                "日报任务仍在运行，保留数据库连接以避免中断当前任务"
            )
            return
        try:
            self.storage.close()
        except Exception:
            self.logger.warning("关闭数据库失败", exc_info=True)

    # ------------------------------------------------------------------
    # Shared delivery helpers
    # ------------------------------------------------------------------
    def _targets(self) -> list[str]:
        return list(self.cfg.push_targets)

    def _build_chain(
        self, result: ReportResult, include_caption: bool = True
    ) -> list:
        chain: list = []
        poster = Path(result.poster_path) if result.poster_path else None
        if poster is not None and poster.is_file():
            chain.append(Comp.Image.fromFileSystem(str(poster)))
            if include_caption and self.cfg.send_caption and result.caption:
                chain.append(Comp.Plain(result.caption))
            return chain
        if self.cfg.send_text_fallback:
            chain.append(Comp.Plain(text_fallback(result, self.cfg)))
        elif result.caption:
            chain.append(Comp.Plain(result.caption))
        return chain

    async def _send_text(self, targets: Iterable[str], text: str) -> list[str]:
        sent: list[str] = []
        for target in targets:
            try:
                ok = await asyncio.wait_for(
                    self.context.send_message(
                        target, MessageChain(chain=[Comp.Plain(text)])
                    ),
                    timeout=90,
                )
                if ok:
                    sent.append(target)
                else:
                    self.logger.warning("推送目标不可用：%s", target)
            except Exception as exc:  # noqa: BLE001
                self.logger.error(
                    "推送文本到 %s 失败：%s",
                    target,
                    self.cfg.redact(str(exc)),
                )
            await asyncio.sleep(0.3)
        return sent

    async def _send_targets(
        self, result: ReportResult
    ) -> tuple[list[str], list[dict]]:
        targets = self._targets()
        sent: list[str] = []
        failed: list[dict] = []
        if not targets:
            result.errors.append("未配置 push_targets，定时推送未发送。")
            self.logger.warning("未配置 push_targets，跳过定时推送")
            return sent, failed
        for target in targets:
            try:
                ok = await asyncio.wait_for(
                    self.context.send_message(
                        target, MessageChain(chain=self._build_chain(result))
                    ),
                    timeout=90,
                )
                if ok:
                    sent.append(target)
                    self.logger.info("日报已推送到 %s", target)
                else:
                    failed.append({"target": target, "error": "平台未找到该会话"})
                    self.logger.warning("推送目标不可用：%s", target)
            except Exception as exc:  # noqa: BLE001
                message = self.cfg.redact(f"{type(exc).__name__}: {exc}")[:300]
                failed.append({"target": target, "error": message})
                self.logger.error("推送到 %s 失败：%s", target, message)
            await asyncio.sleep(0.4)
        return sent, failed

    # ------------------------------------------------------------------
    # Scheduled pipeline (called by AstrBot cron manager)
    # ------------------------------------------------------------------
    async def _scheduled_pipeline(self) -> ReportResult:
        started = datetime.now(self.cfg.timezone)
        try:
            async with self.service.exclusive("scheduled"):
                result = await self.service.generate(
                    mode="scheduled",
                    use_sent_filter=True,
                    make_poster=True,
                    poster_when_empty=False,
                )
                if result.fatal_source_error:
                    text = (
                        "游戏日报生成失败：新闻源均不可用或配置错误。\n"
                        + "\n".join(result.errors[:3])
                    )
                    final_attempt = self.scheduler.current_attempt >= (
                        self.cfg.retry_times + 1
                    )
                    if self.cfg.notify_on_failure and final_attempt:
                        result.sent_targets = await self._send_text(
                            self._targets(), self.cfg.redact(text)
                        )
                    await self.service.finalize_run(result, "scheduled")
                    return result

                if not result.items:
                    if self.cfg.no_news_policy == "text":
                        result.sent_targets = await self._send_text(
                            self._targets(),
                            result.caption or self.cfg.empty_report_text,
                        )
                    else:
                        self.logger.info("今日无合格新闻，按配置跳过推送")
                    await self.service.finalize_run(result, "scheduled")
                    return result

                sent, failed = await self._send_targets(result)
                result.sent_targets = sent
                result.failed_targets = failed
                if sent:
                    await self.service.mark_sent(result)
                await self.service.finalize_run(result, "scheduled")
                await self.service.purge_old_records()
                return result
        except RunBusyError as exc:
            self.logger.warning("定时日报跳过：%s", exc)
            synthetic = ReportResult(
                status="failed",
                items=[],
                stats=ReportStats(),
                source_reports=[],
                started_at=started,
                finished_at=datetime.now(self.cfg.timezone),
                errors=[str(exc)],
            )
            return synthetic
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            message = self.cfg.redact(f"{type(exc).__name__}: {exc}")
            self.logger.error("定时日报异常：%s", message, exc_info=True)
            synthetic = ReportResult(
                status="failed",
                items=[],
                stats=ReportStats(),
                source_reports=[],
                started_at=started,
                finished_at=datetime.now(self.cfg.timezone),
                errors=[message],
            )
            try:
                await self.service.finalize_run(synthetic, "scheduled")
            except Exception:
                pass
            return synthetic

    # ------------------------------------------------------------------
    # Command helpers
    # ------------------------------------------------------------------
    def _denied(self, event: AstrMessageEvent) -> str | None:
        if self.cfg.commands_admin_only and not event.is_admin():
            return "该命令仅限管理员使用。"
        return None

    def _force_requested(self, event: AstrMessageEvent) -> bool:
        text = (event.message_str or "").lower()
        return any(
            token in text for token in ("force", "all", "刷新", "全部", "强制")
        )

    def _preview_suffix(self, extra: str = "") -> str:
        text = "预览模式：未发送到推送目标，也未写入已发送记录。"
        return f"{extra}\n{text}" if extra else text

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    @filter.command("gamedaily", alias={"游戏日报"})
    async def cmd_gamedaily(self, event: AstrMessageEvent):
        """立即采集并推送游戏资讯日报（当前会话）"""
        denied = self._denied(event)
        if denied:
            yield event.plain_result(denied)
            return
        yield event.plain_result("正在采集游戏资讯并生成海报，请稍候…")
        try:
            async with self.service.exclusive("manual"):
                result = await self.service.generate(
                    mode="manual",
                    use_sent_filter=not self._force_requested(event),
                    make_poster=True,
                    poster_when_empty=False,
                )
                if result.fatal_source_error:
                    await self.service.finalize_run(result, "manual")
                    yield event.plain_result(
                        "日报生成失败：\n" + "\n".join(result.errors[:4])
                    )
                    return
                if not result.items:
                    await self.service.finalize_run(result, "manual")
                    yield event.plain_result(
                        result.caption or self.cfg.empty_report_text
                    )
                    return
                chain = self._build_chain(result)
                if not chain:
                    chain = [Comp.Plain(text_fallback(result, self.cfg))]
                yield event.chain_result(chain)
                result.sent_targets = [event.unified_msg_origin]
                await self.service.mark_sent(result)
                await self.service.finalize_run(result, "manual")
                await self.service.purge_old_records()
        except RunBusyError as exc:
            yield event.plain_result(str(exc))
        except asyncio.CancelledError:
            raise
        except PosterError as exc:
            yield event.plain_result(f"海报生成失败：{exc}")
        except Exception as exc:  # noqa: BLE001
            message = self.cfg.redact(f"{type(exc).__name__}: {exc}")
            self.logger.error("手动日报失败：%s", message, exc_info=True)
            yield event.plain_result(f"日报生成失败：{message}")

    @filter.command("gamedaily_preview", alias={"游戏日报预览"})
    async def cmd_gamedaily_preview(self, event: AstrMessageEvent):
        """生成日报海报但不定时推送、不写入已发送记录"""
        denied = self._denied(event)
        if denied:
            yield event.plain_result(denied)
            return
        yield event.plain_result("正在生成预览海报…")
        try:
            async with self.service.exclusive("preview"):
                result = await self.service.generate(
                    mode="preview",
                    use_sent_filter=False,
                    make_poster=True,
                    poster_when_empty=True,
                )
                chain = self._build_chain(result, include_caption=False)
                extra = result.stats.summary_text()
                if result.errors:
                    extra += "\n提示：" + "；".join(result.errors[:2])
                chain.append(Comp.Plain(self._preview_suffix(extra)))
                yield event.chain_result(chain)
        except RunBusyError as exc:
            yield event.plain_result(str(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            message = self.cfg.redact(f"{type(exc).__name__}: {exc}")
            self.logger.error("预览失败：%s", message, exc_info=True)
            yield event.plain_result(f"预览生成失败：{message}")

    @filter.command("gamedaily_status", alias={"游戏日报状态"})
    async def cmd_gamedaily_status(self, event: AstrMessageEvent):
        """查看日报定时任务状态、上次结果与新闻数量"""
        try:
            scheduler_status = self.scheduler.status()
            latest = await asyncio.to_thread(self.storage.latest_run)
            health = await asyncio.to_thread(self.storage.source_health)
            db_stats = await asyncio.to_thread(self.storage.stats)
            lines = ["游戏日报状态", ""]
            if self.cfg.enabled:
                lines.append(
                    f"定时推送：开启 · 每天 {self.cfg.push_time} "
                    f"({self.cfg.timezone_name}) · Cron {scheduler_status['cron']}"
                )
                lines.append(
                    f"下次执行：{format_next_run(scheduler_status['next_run'])}"
                )
            else:
                lines.append("定时推送：已关闭（enabled=false）")
            if scheduler_status.get("error"):
                lines.append(f"调度错误：{scheduler_status['error']}")
            lines.append(
                f"推送目标：{len(self.cfg.push_targets)} 个"
                + (
                    ""
                    if self.cfg.push_targets
                    else "（使用 /gamedaily_target 获取当前会话 UMO 后填入配置）"
                )
            )
            lines.append(
                f"AI：{self.cfg.ai_model if self.cfg.ai_available else '规则模式'}"
                f"（Key {'已配置' if self.cfg.ai_api_key else '未配置'}）"
            )
            lines.append(f"已发送去重键：{db_stats['sent_keys']} · 运行记录：{db_stats['runs']}")
            if latest:
                lines.append("")
                lines.append("上次执行：")
                lines.append(
                    f"- 时间：{_fmt_time(latest.get('started_at'), self.cfg.timezone)}"
                    f" · 模式：{latest.get('mode')} · 状态：{latest.get('status')}"
                )
                lines.append(
                    f"- 采集 {latest.get('total_collected', 0)} 条 → "
                    f"去重后 {latest.get('total_after_dedupe', 0)} 条 → "
                    f"展示 {latest.get('total_selected', 0)} 条"
                )
                sent_targets = latest.get("sent_targets") or []
                failed_targets = latest.get("failed_targets") or []
                lines.append(
                    f"- 已发送 {len(sent_targets)} 个目标"
                    + (
                        f"，失败 {len(failed_targets)} 个"
                        if failed_targets
                        else ""
                    )
                )
                if latest.get("poster_path"):
                    lines.append(f"- 海报：{Path(latest['poster_path']).name}")
                errors = latest.get("errors") or []
                if errors:
                    lines.append(
                        "- 提示：" + "；".join(str(item) for item in errors[:3])
                    )
                items = latest.get("items") or []
                if items:
                    lines.append("- 最近条目：")
                    for item in items[:3]:
                        lines.append(
                            f"  · [{item.get('category', '综合')}] "
                            f"{item.get('title', '')}"
                        )
            else:
                lines.append("")
                lines.append("上次执行：暂无记录")
            failed_sources = [row for row in health if row.get("consecutive_failures")]
            if failed_sources:
                lines.append("")
                lines.append("异常新闻源：")
                for row in failed_sources[:5]:
                    lines.append(
                        f"- {row.get('name')}：连续失败 "
                        f"{row.get('consecutive_failures')} 次（{row.get('last_error', '')[:60]}）"
                    )
            warnings = self.cfg.warnings
            if warnings:
                lines.append("")
                lines.append("配置提示：" + "；".join(warnings[:3]))
            yield event.plain_result("\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            message = self.cfg.redact(f"{type(exc).__name__}: {exc}")
            self.logger.error("读取状态失败：%s", message, exc_info=True)
            yield event.plain_result(f"读取状态失败：{message}")

    @filter.command("gamedaily_test", alias={"游戏日报测试"})
    async def cmd_gamedaily_test(self, event: AstrMessageEvent):
        """测试新闻源、AI API 与本地海报生成"""
        denied = self._denied(event)
        if denied:
            yield event.plain_result(denied)
            return
        yield event.plain_result(
            "开始测试：将请求全部已启用新闻源（每源 1 条）、AI API 与本地海报。"
        )
        try:
            async with self.service.exclusive("test"):
                result = await self.service.self_test()
            chain = [Comp.Plain(result.summary())]
            if result.poster_path and Path(result.poster_path).is_file():
                chain.append(Comp.Image.fromFileSystem(result.poster_path))
            yield event.chain_result(chain)
        except RunBusyError as exc:
            yield event.plain_result(str(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            message = self.cfg.redact(f"{type(exc).__name__}: {exc}")
            self.logger.error("测试失败：%s", message, exc_info=True)
            yield event.plain_result(f"测试失败：{message}")

    @filter.command("gamedaily_target", alias={"游戏日报目标"})
    async def cmd_gamedaily_target(self, event: AstrMessageEvent):
        """显示当前会话的 UMO，便于填入 push_targets"""
        yield event.plain_result(
            "当前会话 UMO：\n"
            f"{event.unified_msg_origin}\n\n"
            "将此字符串加入插件配置的 push_targets 列表即可接收每日推送。"
        )


__all__ = ["GameDailyPlugin"]
