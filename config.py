"""Configuration parsing, validation and defaults for Game Daily.

The AstrBot WebUI schema lives in ``_conf_schema.json``.  This module turns the
raw ``AstrBotConfig`` mapping (or a plain dict in tests) into a validated,
typed object.  It never logs or returns the API key.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

PLUGIN_NAME = "astrbot_plugin_game_daily"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_PUSH_TIME = "08:30"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; OTAKUsGameDaily/1.0; +https://astrbot.app) "
    "AstrBot-GameDaily/1.0"
)
ALLOWED_THEMES = ("dark-neon", "dark-blue", "dark-crimson")
DEFAULT_CATEGORIES = [
    "发售",
    "更新",
    "免费/福利",
    "折扣",
    "电竞",
    "行业",
    "主机",
    "PC",
    "独立",
    "综合",
]


@dataclass(slots=True)
class RSSSourceConfig:
    id: str
    name: str
    url: str
    category: str = "综合"
    weight: float = 1.0
    enabled: bool = True
    custom: bool = True

    def public_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "category": self.category,
            "weight": self.weight,
            "enabled": self.enabled,
        }


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y", "on", "启用", "开启"}:
            return True
        if lowered in {"false", "0", "no", "n", "off", "禁用", "关闭"}:
            return False
    return default


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _as_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[\n,，]+", value)
        return [part.strip() for part in parts if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _iter_template_entries(raw: Any):
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(value, str):
                yield {"name": str(key), "url": value}
            elif isinstance(value, dict):
                entry = dict(value)
                entry.setdefault("name", str(key))
                yield entry
        return
    if not isinstance(raw, (list, tuple)):
        return
    for entry in raw:
        if isinstance(entry, dict):
            yield dict(entry)
        elif isinstance(entry, str) and entry.strip():
            yield entry.strip()


def _parse_one_rss_entry(entry: Any) -> dict | None:
    if isinstance(entry, str):
        text = entry.strip()
        if not text:
            return None
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                return parsed
        enabled = True
        if text.startswith("!"):
            enabled = False
            text = text[1:].strip()
        parts = [part.strip() for part in text.split("|")]
        if len(parts) == 1:
            return {"url": parts[0], "enabled": enabled}
        if parts[0].lower() in {"http", "https"}:
            return {"url": parts[0], "enabled": enabled}
        result = {
            "name": parts[0],
            "url": parts[1] if len(parts) > 1 else "",
            "enabled": enabled,
        }
        if len(parts) > 2 and parts[2]:
            result["category"] = parts[2]
        if len(parts) > 3:
            result["weight"] = parts[3]
        return result
    if isinstance(entry, dict):
        return entry
    return None


def parse_rss_sources(
    raw: Any, logger: logging.Logger | None = None
) -> list[RSSSourceConfig]:
    log = logger or logging.getLogger(__name__)
    result: list[RSSSourceConfig] = []
    seen_urls: set[str] = set()

    for entry in _iter_template_entries(raw):
        parsed = _parse_one_rss_entry(entry)
        if not parsed:
            continue
        url = _as_str(parsed.get("url"))
        if not url:
            continue
        if not url.lower().startswith(("http://", "https://")):
            log.warning("忽略无效 RSS 地址（缺少 http/https）：%s", url[:120])
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        host = urlsplit(url).netloc or "RSS"
        name = _as_str(parsed.get("name")) or host
        category = _as_str(parsed.get("category")) or "综合"
        weight = _as_float(parsed.get("weight"), 1.0, 0.1, 3.0)
        enabled = _as_bool(parsed.get("enabled"), True)
        source_id = _as_str(parsed.get("id")) or (
            "custom-" + hashlib.sha1(url.encode("utf-8", "ignore")).hexdigest()[:10]
        )
        result.append(
            RSSSourceConfig(
                id=source_id,
                name=name,
                url=url,
                category=category,
                weight=weight,
                enabled=enabled,
            )
        )
    return result


def _parse_targets(raw: Any) -> list[str]:
    values = _as_str_list(raw)
    result: list[str] = []
    for value in values:
        if "\n" in value or "," in value:
            result.extend(_as_str_list(value))
        else:
            result.append(value)
    cleaned: list[str] = []
    for value in result:
        value = value.strip()
        if value and value not in cleaned:
            cleaned.append(value)
    return cleaned


def _normalize_api_base(value: str) -> str:
    base = value.strip().rstrip("/")
    if not base:
        return "https://api.openai.com/v1"
    if base.endswith("/chat/completions"):
        return base[: -len("/chat/completions")]
    return base


def _resolve_timezone(value: str, log: logging.Logger) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except Exception:
        log.warning("无效时区 %s，已回退到 %s", value, DEFAULT_TIMEZONE)
        return ZoneInfo(DEFAULT_TIMEZONE)


def _parse_push_time(value: str, log: logging.Logger) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d{1,2})\s*[:：]\s*(\d{1,2})\s*", value or "")
    if not match:
        log.warning("无效推送时间 %r，已回退到 %s", value, DEFAULT_PUSH_TIME)
        return 8, 30
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        log.warning("推送时间超出范围 %r，已回退到 %s", value, DEFAULT_PUSH_TIME)
        return 8, 30
    return hour, minute


@dataclass
class PluginConfig:
    data_dir: Path
    enabled: bool = True
    push_time: str = DEFAULT_PUSH_TIME
    cron_expression_raw: str = ""
    timezone_name: str = DEFAULT_TIMEZONE
    push_targets: list[str] = field(default_factory=list)
    no_news_policy: str = "text"
    notify_on_failure: bool = True
    commands_admin_only: bool = False
    empty_report_text: str = "今日暂无符合条件的游戏资讯，明天见。"
    news_window_hours: int = 24
    max_news: int = 6
    max_per_source: int = 2
    max_source_items: int = 8
    max_candidates: int = 60
    ai_max_candidates: int = 30
    categories_preferred: list[str] = field(default_factory=list)
    excluded_keywords: list[str] = field(default_factory=list)
    include_missing_time: bool = True
    request_timeout: int = 20
    request_interval: float = 0.5
    max_concurrency: int = 4
    retry_times: int = 2
    max_response_bytes: int = 4 * 1024 * 1024
    user_agent: str = DEFAULT_USER_AGENT
    use_proxy_env: bool = True
    enable_steam_news_api: bool = True
    steam_appids: list[str] = field(default_factory=list)
    disabled_sources: list[str] = field(default_factory=list)
    rss_sources: list[RSSSourceConfig] = field(default_factory=list)
    ai_enabled: bool = True
    ai_api_base: str = "https://api.openai.com/v1"
    ai_model: str = "gpt-4o-mini"
    ai_api_key: str = field(default="", repr=False)
    ai_api_key_env: str = "GAMEDAILY_AI_API_KEY"
    ai_timeout: int = 60
    ai_max_input_chars: int = 12000
    ai_batch_size: int = 20
    ai_temperature: float = 0.2
    ai_json_mode: bool = False
    poster_width: int = 1200
    poster_height: int = 675
    font_path: str = ""
    poster_theme: str = "dark-neon"
    output_dir: Path = field(default_factory=Path)
    keep_posters_days: int = 7
    send_caption: bool = True
    send_text_fallback: bool = True
    dedupe_retention_days: int = 30
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo(DEFAULT_TIMEZONE))
    ai_key_source: str = ""
    warnings: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @classmethod
    def from_mapping(
        cls,
        raw: dict | Any,
        data_dir: str | Path,
        logger: logging.Logger | None = None,
        env: dict[str, str] | None = None,
    ) -> "PluginConfig":
        log = logger or logging.getLogger(__name__)
        mapping: dict = dict(raw or {}) if hasattr(raw, "keys") else {}
        data_path = Path(data_dir).expanduser().resolve()
        data_path.mkdir(parents=True, exist_ok=True)
        warnings: list[str] = []

        def get(key: str, default: Any = None) -> Any:
            return mapping.get(key, default)

        enabled = _as_bool(get("enabled"), True)
        push_time = _as_str(get("push_time"), DEFAULT_PUSH_TIME)
        push_hour, push_minute = _parse_push_time(push_time, log)
        push_time = f"{push_hour:02d}:{push_minute:02d}"
        tz_name = _as_str(get("timezone"), DEFAULT_TIMEZONE) or DEFAULT_TIMEZONE
        tz = _resolve_timezone(tz_name, log)
        tz_name = tz.key

        max_news = _as_int(get("max_news"), 6, 4, 6)
        max_per_source = _as_int(get("max_per_source"), 2, 1, 6)
        max_source_items = _as_int(get("max_source_items"), 8, 1, 30)
        max_candidates = _as_int(get("max_candidates"), 60, 10, 200)
        ai_max_candidates = _as_int(
            get("ai_max_candidates"), 30, 5, max_candidates
        )

        categories = _as_str_list(get("categories_preferred")) or [
            "发售",
            "更新",
            "免费/福利",
            "电竞",
            "行业",
        ]
        excluded = _as_str_list(get("excluded_keywords"))

        steam_appids_raw = get("steam_appids")
        if steam_appids_raw is None:
            steam_appids_raw = ["730", "570", "578080", "1172470", "1091500", "1245620"]
        appids: list[str] = []
        for value in _as_str_list(steam_appids_raw):
            if value.isdigit() and value not in appids:
                appids.append(value)
            else:
                log.warning("忽略无效 Steam appid：%s", value[:20])

        rss_sources = parse_rss_sources(get("rss_sources"), log)
        disabled = [item.lower() for item in _as_str_list(get("disabled_sources"))]

        ai_base = _normalize_api_base(
            _as_str(get("ai_api_base"), "https://api.openai.com/v1")
            or "https://api.openai.com/v1"
        )
        ai_model = _as_str(get("ai_model"), "gpt-4o-mini") or "gpt-4o-mini"
        ai_key = _as_str(get("ai_api_key"))
        env_map = env if env is not None else os.environ
        ai_key_env = (
            _as_str(get("ai_api_key_env"), "GAMEDAILY_AI_API_KEY")
            or "GAMEDAILY_AI_API_KEY"
        )
        ai_key_source = ""
        if ai_key:
            ai_key_source = "config"
        else:
            for env_name in (ai_key_env, "GAMEDAILY_AI_API_KEY"):
                candidate = (env_map.get(env_name) or "").strip()
                if candidate:
                    ai_key = candidate
                    ai_key_source = f"env:{env_name}"
                    break
        if not ai_key:
            warnings.append("未配置 AI API Key，将使用基于规则的筛选与摘要。")

        theme = _as_str(get("poster_theme"), "dark-neon") or "dark-neon"
        if theme not in ALLOWED_THEMES:
            log.warning("未知海报主题 %r，已回退到 dark-neon", theme)
            theme = "dark-neon"

        output_raw = _as_str(get("output_dir"))
        if output_raw:
            out_path = Path(output_raw).expanduser()
            if not out_path.is_absolute():
                out_path = data_path / out_path
        else:
            out_path = data_path / "posters"
        out_path = out_path.resolve()

        no_news_policy = _as_str(get("no_news_policy"), "text").lower()
        if no_news_policy not in {"text", "skip"}:
            no_news_policy = "text"

        font_path = _as_str(get("font_path"))
        if font_path:
            candidate = Path(font_path).expanduser()
            if not candidate.is_absolute():
                local_font = data_path / candidate
                if local_font.is_file():
                    font_path = str(local_font.resolve())

        cfg = cls(
            data_dir=data_path,
            enabled=enabled,
            push_time=push_time,
            cron_expression_raw=_as_str(get("cron_expression")),
            timezone_name=tz_name,
            push_targets=_parse_targets(get("push_targets")),
            no_news_policy=no_news_policy,
            notify_on_failure=_as_bool(get("notify_on_failure"), True),
            commands_admin_only=_as_bool(get("commands_admin_only"), False),
            empty_report_text=_as_str(
                get("empty_report_text"),
                "今日暂无符合条件的游戏资讯，明天见。",
            )
            or "今日暂无符合条件的游戏资讯，明天见。",
            news_window_hours=_as_int(get("news_window_hours"), 24, 1, 168),
            max_news=max_news,
            max_per_source=max_per_source,
            max_source_items=max_source_items,
            max_candidates=max_candidates,
            ai_max_candidates=ai_max_candidates,
            categories_preferred=categories[:10],
            excluded_keywords=excluded[:50],
            include_missing_time=_as_bool(get("include_missing_time"), True),
            request_timeout=_as_int(get("request_timeout"), 20, 5, 120),
            request_interval=_as_float(get("request_interval"), 0.5, 0.0, 10.0),
            max_concurrency=_as_int(get("max_concurrency"), 4, 1, 8),
            retry_times=_as_int(get("retry_times"), 2, 0, 3),
            max_response_bytes=_as_int(
                get("max_response_bytes"), 4 * 1024 * 1024, 256 * 1024, 16 * 1024 * 1024
            ),
            user_agent=_as_str(get("user_agent"), DEFAULT_USER_AGENT)
            or DEFAULT_USER_AGENT,
            use_proxy_env=_as_bool(get("use_proxy_env"), True),
            enable_steam_news_api=_as_bool(get("enable_steam_news_api"), True),
            steam_appids=appids,
            disabled_sources=disabled,
            rss_sources=rss_sources,
            ai_enabled=_as_bool(get("ai_enabled"), True),
            ai_api_base=ai_base,
            ai_model=ai_model,
            ai_api_key=ai_key,
            ai_api_key_env=ai_key_env,
            ai_timeout=_as_int(get("ai_timeout"), 60, 10, 180),
            ai_max_input_chars=_as_int(get("ai_max_input_chars"), 12000, 2000, 60000),
            ai_batch_size=_as_int(get("ai_batch_size"), 20, 1, 50),
            ai_temperature=_as_float(get("ai_temperature"), 0.2, 0.0, 1.0),
            ai_json_mode=_as_bool(get("ai_json_mode"), False),
            poster_width=_as_int(get("poster_width"), 1200, 800, 2400),
            poster_height=_as_int(get("poster_height"), 675, 450, 1600),
            font_path=font_path,
            poster_theme=theme,
            output_dir=out_path,
            keep_posters_days=_as_int(get("keep_posters_days"), 7, 1, 365),
            send_caption=_as_bool(get("send_caption"), True),
            send_text_fallback=_as_bool(get("send_text_fallback"), True),
            dedupe_retention_days=_as_int(
                get("dedupe_retention_days"), 30, 1, 3650
            ),
            tz=tz,
            ai_key_source=ai_key_source,
            warnings=warnings,
        )
        if cfg.poster_width != 1200 or cfg.poster_height != 675:
            ratio = cfg.poster_width / max(1, cfg.poster_height)
            if abs(ratio - 16 / 9) > 0.03:
                cfg.warnings.append(
                    f"海报尺寸 {cfg.poster_width}x{cfg.poster_height} 不是 16:9，"
                    "建议使用 1200x675 或 1600x900。"
                )
        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        return cfg

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------
    @property
    def ai_available(self) -> bool:
        return bool(
            self.ai_enabled
            and self.ai_api_key
            and self.ai_api_base
            and self.ai_model
        )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "gamedaily.db"

    @property
    def effective_cron(self) -> str:
        raw = self.cron_expression_raw.strip()
        if raw:
            parts = raw.split()
            if len(parts) == 5:
                return raw
            self.warnings.append(
                f"Cron 表达式 {raw!r} 不是 5 段，已改为按 push_time 生成。"
            )
        hour, minute = _parse_push_time(
            self.push_time, logging.getLogger(__name__)
        )
        return f"{minute} {hour} * * *"

    @property
    def timezone(self) -> ZoneInfo:
        return self.tz

    @property
    def is_source_disabled(self) -> set[str]:
        return set(self.disabled_sources)

    def public_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "push_time": self.push_time,
            "cron": self.effective_cron,
            "timezone": self.timezone_name,
            "target_count": len(self.push_targets),
            "window_hours": self.news_window_hours,
            "max_news": self.max_news,
            "max_per_source": self.max_per_source,
            "ai_enabled": self.ai_enabled,
            "ai_available": self.ai_available,
            "ai_model": self.ai_model,
            "ai_key_configured": bool(self.ai_api_key),
            "ai_key_source": self.ai_key_source,
            "poster": f"{self.poster_width}x{self.poster_height}",
            "theme": self.poster_theme,
            "output_dir": str(self.output_dir),
            "rss_extra": len(self.rss_sources),
            "disabled_sources": list(self.disabled_sources),
            "warnings": list(self.warnings),
        }

    def redact(self, text: str) -> str:
        """Remove the configured API key from arbitrary log/error text."""

        if not self.ai_api_key:
            return text
        return text.replace(self.ai_api_key, "***")
