"""Pillow poster renderer for the Game Daily report.

The renderer is intentionally self-contained: no online image service and no
external template.  It draws a 3:4 white editorial poster with real news
thumbnails, a Steam deals strip and typographic hierarchy.

Fonts are discovered from the configured path first and then from common
Linux/Windows/macOS locations.  If no CJK-capable font exists, rendering fails
with a clear error instead of silently producing tofu/garbled Chinese.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import shutil
import subprocess
import threading
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .config import PluginConfig
from .models import NewsItem, ReportStats, SteamDeal

log = logging.getLogger(__name__)

_EMOJI_RE = re.compile(
    "[\U0001f000-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff"
    "\ufe0f\u200d\u20e3]"
)

COLOR_BG = (255, 255, 255)
COLOR_TEXT = (17, 19, 24)
COLOR_BODY = (85, 91, 102)
COLOR_MUTED = (129, 136, 149)
COLOR_SOURCE = (112, 126, 146)
COLOR_RULE = (220, 224, 229)
COLOR_ACCENT = (40, 185, 176)
COLOR_ACCENT_SOFT = (232, 248, 246)
COLOR_STEAM = (33, 136, 56)
COLOR_TAG_BG = (240, 242, 244)


def _px(value: float, scale: float) -> int:
    return int(round(value * scale))


class PosterError(RuntimeError):
    pass


_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

_FONT_CANDIDATE_PAIRS: tuple[tuple[str, str, int], ...] = (
    (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        2,
    ),
    (
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        2,
    ),
    (
        "/usr/share/fonts/opentype/noto/NotoSansSC-Regular.otf",
        "/usr/share/fonts/opentype/noto/NotoSansSC-Bold.otf",
        0,
    ),
    (
        "/usr/share/fonts/truetype/noto/NotoSansSC-Regular.otf",
        "/usr/share/fonts/truetype/noto/NotoSansSC-Bold.otf",
        0,
    ),
    (
        "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Regular.otf",
        "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Bold.otf",
        0,
    ),
    (
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        0,
    ),
    (
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        0,
    ),
    (
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
        0,
    ),
    ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc", 0),
    ("C:/Windows/Fonts/msyh.ttf", "C:/Windows/Fonts/msyhbd.ttf", 0),
    ("C:/Windows/Fonts/Deng.ttf", "C:/Windows/Fonts/Dengb.ttf", 0),
    ("C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/simhei.ttf", 0),
    ("C:/Windows/Fonts/simsun.ttc", "C:/Windows/Fonts/simsun.ttc", 0),
    ("/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/PingFang.ttc", 0),
    ("/Library/Fonts/Arial Unicode.ttf", "/Library/Fonts/Arial Unicode.ttf", 0),
)

_FONT_SCAN_DIRS = (
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    str(Path.home() / ".local/share/fonts"),
    str(Path.home() / ".fonts"),
    "C:/Windows/Fonts",
    "/System/Library/Fonts",
    "/Library/Fonts",
)
_CJK_HINTS = (
    "noto sans cjk",
    "noto sans sc",
    "source han sans",
    "sourcehansans",
    "microsoft yahei",
    "msyh",
    "pingfang",
    "simhei",
    "heiti",
    "wenquanyi",
    "wqy",
    "droid sans fallback",
    "simsun",
    "songti",
)


def _clean_display_text(text: str) -> str:
    value = _EMOJI_RE.sub("", str(text or ""))
    value = value.replace("\u200b", "").replace("\ufeff", "")
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _font_index_for(path: str) -> int:
    lowered = Path(path).name.lower()
    if "notosanscjk" in lowered or "notoserifcjk" in lowered:
        return 2
    return 0


def _try_font(path: str, size: int, index: int) -> ImageFont.FreeTypeFont | None:
    try:
        if Path(path).is_file():
            return ImageFont.truetype(path, size=size, index=index)
    except (OSError, ValueError):
        pass
    return None


def _fc_match(pattern: str) -> str | None:
    executable = shutil.which("fc-match")
    if not executable:
        return None
    try:
        result = subprocess.run(
            [executable, "-f", "%{file}", pattern],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    path = result.stdout.strip().splitlines()[0].strip() if result.stdout else ""
    return path if path and Path(path).is_file() else None


class FontBook:
    """Discovers a regular/bold CJK font pair and caches sized fonts."""

    def __init__(self, configured_path: str = "", logger=None) -> None:
        self.log = logger or log
        self._lock = threading.RLock()
        self._cache: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}
        self._regular_path, self._bold_path, self._index = self._discover(
            configured_path.strip()
        )

    @property
    def path(self) -> str:
        return self._regular_path

    def _discover(self, configured: str) -> tuple[str, str, int]:
        candidates: list[tuple[str, str, int]] = []
        env_path = os.environ.get("GAMEDAILY_FONT_PATH", "").strip()
        for value in (configured, env_path):
            if value and Path(value).is_file():
                bold = self._guess_bold(value)
                candidates.append((value, bold, _font_index_for(value)))

        for regular, bold, index in _FONT_CANDIDATE_PAIRS:
            if Path(regular).is_file():
                candidates.append(
                    (regular, bold if Path(bold).is_file() else regular, index)
                )

        for pattern in (
            "Noto Sans CJK SC:lang=zh",
            "Noto Sans SC:lang=zh",
            "Microsoft YaHei:lang=zh",
            "WenQuanYi Micro Hei:lang=zh",
            "sans-serif:lang=zh",
        ):
            match = _fc_match(pattern)
            if match:
                bold_match = _fc_match(pattern + ":weight=bold") or match
                candidates.append(
                    (match, bold_match, _font_index_for(match))
                )
                break

        if not candidates:
            for directory in _FONT_SCAN_DIRS:
                root = Path(directory)
                if not root.is_dir():
                    continue
                try:
                    files = [
                        file
                        for file in root.rglob("*")
                        if file.is_file()
                        and file.suffix.lower() in {".ttf", ".ttc", ".otf"}
                    ]
                except (OSError, PermissionError):
                    continue
                for file in files:
                    if any(hint in file.name.lower() for hint in _CJK_HINTS):
                        text = str(file)
                        candidates.append((text, text, _font_index_for(text)))
                if candidates:
                    break

        for regular, bold, index in candidates:
            font = _try_font(regular, 24, index)
            if font is None:
                continue
            # Render a CJK glyph to make sure the file really has Chinese
            # coverage.  A missing glyph is usually a blank/box mask.
            try:
                mask = font.getmask("游戏日报")
                if mask.getbbox() is None:
                    continue
            except Exception:
                continue
            return regular, bold, index

        raise PosterError(
            "未找到可用的中文字体。请在插件配置 font_path 指定 .ttf/.ttc/.otf，"
            "或在系统中安装 Noto Sans CJK / 思源黑体 / 微软雅黑。"
        )

    @staticmethod
    def _guess_bold(path: str) -> str:
        candidate = Path(path)
        name = candidate.name
        replacements = (
            ("Regular", "Bold"),
            ("regular", "bold"),
            ("-R.", "-B."),
            ("_R.", "_B."),
        )
        for old, new in replacements:
            if old in name:
                bold = candidate.with_name(name.replace(old, new))
                if bold.is_file():
                    return str(bold)
        return path

    def font(self, size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
        size = max(8, int(round(size)))
        key = (size, bool(bold))
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
            path = self._bold_path if bold else self._regular_path
            font = _try_font(path, size, self._index)
            if font is None:
                font = _try_font(self._regular_path, size, self._index)
            if font is None:
                raise PosterError(f"字体加载失败：{path}")
            self._cache[key] = font
            return font


def _text_width(font, text: str) -> float:
    try:
        return float(font.getlength(text))
    except Exception:
        return float(len(text) * font.size)


def _wrap_text(
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: float,
    max_lines: int,
) -> list[str]:
    """Pixel-aware word/CJK-aware wrapping with a bounded line count.

    Latin text is wrapped at spaces where possible (avoiding splits such as
    ``C / onfirmed``), while long CJK runs and oversized words fall back to
    character wrapping.  Overflow is trimmed with an ellipsis, never by
    shrinking the font.
    """

    value = _clean_display_text(text)
    if not value:
        return []
    if max_width <= 4 or max_lines <= 0:
        return []

    words = [part for part in re.split(r"(\s+)", value) if part]
    lines: list[str] = []
    current = ""
    pending_space = False
    for part in words:
        if part.isspace():
            pending_space = bool(current)
            continue
        separator = " " if pending_space and current else ""
        candidate = current + separator + part
        if _text_width(font, candidate) <= max_width:
            current = candidate
            pending_space = False
            continue
        if _text_width(font, part) <= max_width:
            if current:
                lines.append(current)
            current = part
            pending_space = False
            continue
        # The token itself is wider than a line.  Pack it into the remaining
        # space on the current line first (keeps "CS2 冬季更新..." together),
        # then continue on fresh lines.
        remaining = part
        while remaining:
            separator = " " if pending_space and current else ""
            available = max_width - _text_width(font, current + separator)
            if available <= 1 and current:
                lines.append(current)
                current = ""
                separator = ""
                available = max_width
            chunk = ""
            for char in remaining:
                if _text_width(font, chunk + char) <= available:
                    chunk += char
                else:
                    break
            if not chunk:
                chunk = remaining[0]
            current = current + separator + chunk
            remaining = remaining[len(chunk) :]
            pending_space = False
            if remaining:
                lines.append(current)
                current = ""
    if current:
        lines.append(current)

    if len(lines) > max_lines:
        line = lines[max_lines - 1]
        while line and _text_width(font, line + "…") > max_width:
            line = line[:-1]
        lines = lines[: max_lines - 1] + [line.rstrip() + "…"]
    return lines


def _text_bbox(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int, int, int]:
    return draw.textbbox((0, 0), text, font=font)


def _draw_tracked(
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    text: str,
    font,
    fill,
    tracking: float,
) -> float:
    cursor = x
    for char in text:
        draw.text((cursor, y), char, font=font, fill=fill)
        cursor += font.getlength(char) + tracking
    return cursor - x


def _cover_image(
    image: Image.Image,
    size: tuple[int, int],
    radius: int,
) -> Image.Image:
    width, height = size
    target = ImageOps.fit(image.convert("RGB"), size, method=Image.Resampling.LANCZOS)
    mask = Image.new("L", size, 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle(
        [0, 0, width - 1, height - 1],
        radius=max(0, radius),
        fill=255,
    )
    result = Image.new("RGBA", size, (0, 0, 0, 0))
    result.paste(target, (0, 0), mask)
    return result


def _draw_text_cover(
    image: Image.Image,
    rect: tuple[int, int, int, int],
    title: str,
    scale: float,
    fonts: "FontBook",
    radius: int,
    cover: bool = False,
) -> None:
    x, y, width, height = rect
    width = max(1, width)
    height = max(1, height)
    panel = Image.new("RGBA", (width, height), (240, 242, 244, 255))
    draw = ImageDraw.Draw(panel)
    bar_h = max(2, _px(3, scale))
    draw.rectangle(
        [0, 0, _px(26, scale), bar_h],
        fill=COLOR_ACCENT,
    )
    pad = _px(12, scale)
    font = fonts.font((12 if cover else 11) * scale, bold=True)
    lines = _wrap_text(
        _clean_display_text(title) or "游戏新闻",
        font,
        width - pad * 2,
        3,
    )
    line_h = _px(17, scale)
    text_h = len(lines) * line_h
    text_y = max(pad, (height - text_h) // 2)
    for line in lines:
        draw.text((pad, text_y), line, font=font, fill=COLOR_BODY)
        text_y += line_h
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, width - 1, height - 1],
        radius=max(0, radius),
        fill=255,
    )
    panel.putalpha(mask)
    image.paste(panel, (x, y), panel)


class PosterRenderer:
    def __init__(self, config: PluginConfig, logger=None) -> None:
        self.config = config
        self.log = logger or log
        self._font_book: FontBook | None = None
        self._lock = threading.RLock()
        self._image_cache: dict[str, Image.Image | None] = {}

    def _fonts(self) -> FontBook:
        with self._lock:
            if self._font_book is None:
                self._font_book = FontBook(self.config.font_path, self.log)
            return self._font_book

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def render(
        self,
        items: list[NewsItem],
        stats: ReportStats,
        now: datetime,
        empty: bool = False,
        filename_prefix: str = "gamedaily",
        ai_used: bool = False,
        deals: list[SteamDeal] | None = None,
    ) -> str:
        try:
            return self._render(
                items,
                stats,
                now,
                empty,
                filename_prefix,
                ai_used,
                deals or [],
            )
        except PosterError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PosterError(f"海报生成失败：{type(exc).__name__}: {exc}") from exc

    def cleanup(self, days: int | None = None) -> int:
        days = self.config.keep_posters_days if days is None else days
        cutoff = datetime.now().timestamp() - max(1, days) * 86400
        removed = 0
        try:
            for pattern in ("gamedaily-*.png", "preview-*.png"):
                for path in self.config.output_dir.glob(pattern):
                    try:
                        if path.stat().st_mtime < cutoff:
                            path.unlink(missing_ok=True)
                            removed += 1
                    except OSError:
                        continue
        except OSError:
            pass
        return removed

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def _render(
        self,
        items: list[NewsItem],
        stats: ReportStats,
        now: datetime,
        empty: bool,
        filename_prefix: str,
        ai_used: bool,
        deals: list[SteamDeal],
    ) -> str:
        width = self.config.poster_width
        height = self.config.poster_height
        if width < 600 or height < 800:
            raise PosterError("海报尺寸过小")
        s = width / 1200.0
        image = Image.new("RGB", (width, height), COLOR_BG)
        draw = ImageDraw.Draw(image)
        now_local = now.astimezone(self.config.timezone)
        fonts = self._fonts()

        images = self._preload_images(items if not empty else [], deals)
        self._draw_header(draw, width, s, now_local, fonts, ai_used)
        if empty or not items:
            self._draw_empty(draw, width, height, s, fonts)
        else:
            self._draw_news(draw, width, s, fonts, items[:6], images, now_local)
            self._draw_deals(draw, width, s, fonts, deals, images)
        self._draw_footer(draw, width, height, s, fonts)

        output_dir = self.config.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = now_local.strftime("%Y%m%d-%H%M%S")
        name = f"{filename_prefix}-{stamp}-{uuid.uuid4().hex[:6]}.png"
        path = output_dir / name
        tmp = output_dir / f".{name}.tmp"
        image.save(tmp, format="PNG", optimize=True)
        os.replace(tmp, path)
        self.cleanup()
        return str(path)

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------
    def _draw_header(
        self,
        draw: ImageDraw.ImageDraw,
        width: int,
        s: float,
        now: datetime,
        fonts: FontBook,
        ai_used: bool,
    ) -> None:
        left = _px(54, s)
        right = width - left
        draw.rectangle(
            [left, _px(50, s), left + _px(44, s), _px(54, s)],
            fill=COLOR_ACCENT,
        )
        english_font = fonts.font(12 * s, bold=True)
        _draw_tracked(
            draw,
            left,
            _px(64, s),
            "GAME DAILY",
            english_font,
            COLOR_MUTED,
            max(1.0, 2.4 * s),
        )

        title_font = fonts.font(64 * s, bold=True)
        draw.text(
            (left, _px(82, s)),
            "游戏日报",
            font=title_font,
            fill=COLOR_TEXT,
        )
        subtitle_font = fonts.font(17 * s)
        draw.text(
            (left, _px(166, s)),
            "每日游戏情报 · Steam 折扣精选 · 行业动态",
            font=subtitle_font,
            fill=COLOR_MUTED,
        )

        date_font = fonts.font(38 * s, bold=True)
        date_text = now.strftime("%Y.%m.%d")
        date_bbox = _text_bbox(draw, date_text, date_font)
        draw.text(
            (right - (date_bbox[2] - date_bbox[0]), _px(52, s)),
            date_text,
            font=date_font,
            fill=COLOR_TEXT,
        )
        weekday_font = fonts.font(15 * s)
        weekday_text = f"{_WEEKDAYS[now.weekday()]} · 第{now.isocalendar().week}周"
        weekday_bbox = _text_bbox(draw, weekday_text, weekday_font)
        draw.text(
            (right - (weekday_bbox[2] - weekday_bbox[0]), _px(104, s)),
            weekday_text,
            font=weekday_font,
            fill=COLOR_BODY,
        )
        update_font = fonts.font(12 * s, bold=True)
        update_text = "每日更新" if ai_used else "今日更新"
        update_bbox = _text_bbox(draw, update_text, update_font)
        draw.text(
            (right - (update_bbox[2] - update_bbox[0]), _px(128, s)),
            update_text,
            font=update_font,
            fill=COLOR_ACCENT,
        )
        draw.line(
            [(left, _px(216, s)), (right, _px(216, s))],
            fill=COLOR_RULE,
            width=1,
        )

    # ------------------------------------------------------------------
    # News
    # ------------------------------------------------------------------
    def _draw_news(
        self,
        draw: ImageDraw.ImageDraw,
        width: int,
        s: float,
        fonts: FontBook,
        items: list[NewsItem],
        images: dict[str, Image.Image | None],
        now: datetime,
    ) -> None:
        left = _px(54, s)
        right = width - left
        section_y = _px(248, s)
        draw.rectangle(
            [left, section_y + _px(3, s), left + _px(4, s), section_y + _px(26, s)],
            fill=COLOR_ACCENT,
        )
        heading_font = fonts.font(24 * s, bold=True)
        draw.text(
            (left + _px(16, s), section_y),
            "今日要闻",
            font=heading_font,
            fill=COLOR_TEXT,
        )
        heading_bbox = _text_bbox(draw, "今日要闻", heading_font)
        line_start = left + _px(16, s) + (heading_bbox[2] - heading_bbox[0]) + _px(18, s)
        draw.line(
            [(line_start, section_y + _px(14, s)), (right, section_y + _px(14, s))],
            fill=COLOR_RULE,
            width=1,
        )
        news_font = fonts.font(10 * s, bold=True)
        draw.text(
            (right - _text_bbox(draw, "NEWS", news_font)[2], section_y + _px(2, s)),
            "NEWS",
            font=news_font,
            fill=COLOR_MUTED,
        )

        top = _px(292, s)
        row_h = _px(140, s)
        for index, item in enumerate(items, start=1):
            y = top + (index - 1) * row_h
            self._draw_news_item(draw, left, right, y, row_h, s, fonts, item, index, images, now)
            if index < len(items):
                draw.line(
                    [(left, y + row_h - 1), (right, y + row_h - 1)],
                    fill=COLOR_RULE,
                    width=1,
                )

    def _draw_news_item(
        self,
        draw: ImageDraw.ImageDraw,
        left: int,
        right: int,
        y: int,
        row_h: int,
        s: float,
        fonts: FontBook,
        item: NewsItem,
        index: int,
        images: dict[str, Image.Image | None],
        now: datetime,
    ) -> None:
        # Number block
        number_size = _px(34, s)
        number_font = fonts.font(13 * s, bold=True)
        draw.rounded_rectangle(
            [left, y + _px(16, s), left + number_size, y + _px(16, s) + number_size],
            radius=_px(3, s),
            fill=COLOR_TEXT,
        )
        number = f"{index:02d}"
        nb = _text_bbox(draw, number, number_font)
        draw.text(
            (
                left + (number_size - (nb[2] - nb[0])) / 2,
                y + _px(16, s) + (number_size - (nb[3] - nb[1])) / 2 - nb[1],
            ),
            number,
            font=number_font,
            fill=COLOR_BG,
        )

        # Thumbnail
        thumb_x = left + _px(48, s)
        thumb_y = y + _px(8, s)
        thumb_w = _px(170, s)
        thumb_h = _px(106, s)
        image = images.get(item.image_url) if item.image_url else None
        if image is not None:
            cover = _cover_image(image, (thumb_w, thumb_h), _px(4, s))
            draw._image.paste(cover, (thumb_x, thumb_y), cover)
        else:
            _draw_text_cover(
                draw._image,
                (thumb_x, thumb_y, thumb_w, thumb_h),
                item.display_title or "游戏新闻",
                s,
                fonts,
                _px(4, s),
            )

        content_x = thumb_x + thumb_w + _px(22, s)
        # Category tag (right aligned, no large pill)
        category = item.category or "综合"
        category_font = fonts.font(10 * s, bold=True)
        cat_text = "独立游戏" if category == "独立" else category
        cat_bbox = _text_bbox(draw, cat_text, category_font)
        cat_w = cat_bbox[2] - cat_bbox[0] + _px(12, s)
        cat_h = _px(21, s)
        cat_x = right - cat_w
        cat_y = y + _px(9, s)
        cat_fill = COLOR_ACCENT_SOFT if category in {"发售", "更新", "评测"} else COLOR_TAG_BG
        draw.rounded_rectangle(
            [cat_x, cat_y, right, cat_y + cat_h],
            radius=_px(3, s),
            fill=cat_fill,
        )
        draw.text(
            (cat_x + _px(6, s), cat_y + _px(3, s)),
            cat_text,
            font=category_font,
            fill=COLOR_BODY,
        )

        text_w = max(_px(100, s), cat_x - content_x - _px(14, s))
        title_font = fonts.font(22 * s, bold=True)
        title_lh = _px(28, s)
        title_lines = _wrap_text(item.display_title, title_font, text_w, 2)
        title_y = y + _px(8, s)
        for line in title_lines:
            draw.text((content_x, title_y), line, font=title_font, fill=COLOR_TEXT)
            title_y += title_lh

        summary_font = fonts.font(14 * s)
        summary_lh = _px(19, s)
        summary_y = title_y + _px(2, s)
        summary_lines = _wrap_text(
            item.display_summary or "来源未提供摘要。",
            summary_font,
            text_w,
            2,
        )
        for line in summary_lines:
            draw.text((content_x, summary_y), line, font=summary_font, fill=COLOR_BODY)
            summary_y += summary_lh

        source_font = fonts.font(10.5 * s)
        source_name = _clean_display_text(item.display_source) or "未知来源"
        time_text = (
            "时间未标注"
            if item.published_at is None
            else item.published_at.astimezone(self.config.timezone).strftime("%m.%d %H:%M")
        )
        official = "官方 · " if item.is_official else ""
        source_text = f"{official}{source_name} · {time_text}"
        source_line = _wrap_text(source_text, source_font, text_w, 1)
        if source_line:
            source_y = min(
                y + row_h - _px(22, s),
                summary_y + _px(4, s),
            )
            draw.text(
                (content_x, source_y),
                source_line[0],
                font=source_font,
                fill=COLOR_SOURCE,
            )

    # ------------------------------------------------------------------
    # Steam deals
    # ------------------------------------------------------------------
    def _draw_deals(
        self,
        draw: ImageDraw.ImageDraw,
        width: int,
        s: float,
        fonts: FontBook,
        deals: list[SteamDeal],
        images: dict[str, Image.Image | None],
    ) -> None:
        left = _px(54, s)
        right = width - left
        section_y = _px(1150, s)
        draw.line([(left, section_y), (right, section_y)], fill=COLOR_RULE, width=1)

        icon_size = _px(24, s)
        icon_y = _px(1174, s)
        draw.rounded_rectangle(
            [left, icon_y, left + icon_size, icon_y + icon_size],
            radius=_px(4, s),
            fill=COLOR_TEXT,
        )
        icon_font = fonts.font(14 * s, bold=True)
        ib = _text_bbox(draw, "S", icon_font)
        draw.text(
            (
                left + (icon_size - (ib[2] - ib[0])) / 2,
                icon_y + (icon_size - (ib[3] - ib[1])) / 2 - ib[1],
            ),
            "S",
            font=icon_font,
            fill=COLOR_BG,
        )
        heading_font = fonts.font(24 * s, bold=True)
        draw.text(
            (left + _px(36, s), icon_y - _px(2, s)),
            "Steam 游戏折扣",
            font=heading_font,
            fill=COLOR_TEXT,
        )
        hint_font = fonts.font(11.5 * s)
        hint = "限时优惠 · 精选推荐  →"
        hb = _text_bbox(draw, hint, hint_font)
        draw.text(
            (right - (hb[2] - hb[0]), icon_y + _px(5, s)),
            hint,
            font=hint_font,
            fill=COLOR_MUTED,
        )

        cards_top = _px(1218, s)
        gap = _px(18, s)
        card_w = int((right - left - gap * 3) / 4)
        if not deals:
            empty_font = fonts.font(14 * s)
            text = "暂无有效折扣数据"
            tb = _text_bbox(draw, text, empty_font)
            draw.text(
                (
                    (width - (tb[2] - tb[0])) / 2,
                    cards_top + _px(42, s),
                ),
                text,
                font=empty_font,
                fill=COLOR_MUTED,
            )
            return

        for index, deal in enumerate(deals[:4]):
            x = left + index * (card_w + gap)
            self._draw_deal_card(
                draw, x, cards_top, card_w, s, fonts, deal, images
            )

    def _draw_deal_card(
        self,
        draw: ImageDraw.ImageDraw,
        x: int,
        top: int,
        card_w: int,
        s: float,
        fonts: FontBook,
        deal: SteamDeal,
        images: dict[str, Image.Image | None],
    ) -> None:
        cover_h = int(card_w * 9 / 16)
        image = images.get(deal.image_url) if deal.image_url else None
        if image is not None:
            cover = _cover_image(image, (card_w, cover_h), _px(4, s))
            draw._image.paste(cover, (x, top), cover)
        else:
            _draw_text_cover(
                draw._image,
                (x, top, card_w, cover_h),
                deal.name,
                s,
                fonts,
                _px(4, s),
                cover=True,
            )

        name_font = fonts.font(15 * s, bold=True)
        name_lh = _px(19, s)
        name_lines = _wrap_text(deal.name, name_font, card_w, 2)
        name_y = top + cover_h + _px(8, s)
        for line in name_lines:
            draw.text((x, name_y), line, font=name_font, fill=COLOR_TEXT)
            name_y += name_lh

        discount_font = fonts.font(11.5 * s, bold=True)
        tag_text = deal.discount_text
        tag_bbox = _text_bbox(draw, tag_text, discount_font)
        tag_w = tag_bbox[2] - tag_bbox[0] + _px(10, s)
        tag_h = _px(20, s)
        price_y = name_y + _px(3, s)
        draw.rounded_rectangle(
            [x, price_y, x + tag_w, price_y + tag_h],
            radius=_px(2, s),
            fill=COLOR_STEAM,
        )
        draw.text(
            (x + _px(5, s), price_y + _px(3, s)),
            tag_text,
            font=discount_font,
            fill=COLOR_BG,
        )

        old_font = fonts.font(11.5 * s)
        old_text = deal.original_price_text
        old_x = x + tag_w + _px(8, s)
        old_y = price_y + _px(3, s)
        old_bbox = _text_bbox(draw, old_text, old_font)
        draw.text((old_x, old_y), old_text, font=old_font, fill=COLOR_MUTED)
        draw.line(
            [
                (old_x, old_y + (old_bbox[3] - old_bbox[1]) // 2 + 1),
                (old_x + (old_bbox[2] - old_bbox[0]), old_y + (old_bbox[3] - old_bbox[1]) // 2 + 1),
            ],
            fill=COLOR_MUTED,
            width=1,
        )

        final_font = fonts.font(15 * s, bold=True)
        final_text = deal.final_price_text
        final_x = old_x + (old_bbox[2] - old_bbox[0]) + _px(8, s)
        draw.text(
            (final_x, price_y + _px(1, s)),
            final_text,
            font=final_font,
            fill=COLOR_TEXT,
        )

        genre_font = fonts.font(10.5 * s)
        genre_y = price_y + tag_h + _px(5, s)
        genre_lines = _wrap_text(deal.genres_text, genre_font, card_w, 2)
        for line in genre_lines:
            draw.text((x, genre_y), line, font=genre_font, fill=COLOR_MUTED)
            genre_y += _px(15, s)

    # ------------------------------------------------------------------
    # Empty and footer
    # ------------------------------------------------------------------
    def _draw_empty(
        self,
        draw: ImageDraw.ImageDraw,
        width: int,
        height: int,
        s: float,
        fonts: FontBook,
    ) -> None:
        center_x = width // 2
        center_y = _px(730, s)
        draw.line(
            [
                (center_x - _px(30, s), center_y),
                (center_x + _px(30, s), center_y),
            ],
            fill=COLOR_ACCENT,
            width=max(1, _px(2, s)),
        )
        title_font = fonts.font(34 * s, bold=True)
        title = "今日暂无更新"
        tb = _text_bbox(draw, title, title_font)
        draw.text(
            (center_x - (tb[2] - tb[0]) / 2, center_y + _px(34, s)),
            title,
            font=title_font,
            fill=COLOR_TEXT,
        )
        subtitle_font = fonts.font(15 * s)
        subtitle = "没有符合条件的游戏资讯，明天见。"
        sb = _text_bbox(draw, subtitle, subtitle_font)
        draw.text(
            (center_x - (sb[2] - sb[0]) / 2, center_y + _px(86, s)),
            subtitle,
            font=subtitle_font,
            fill=COLOR_MUTED,
        )

    def _draw_footer(
        self,
        draw: ImageDraw.ImageDraw,
        width: int,
        height: int,
        s: float,
        fonts: FontBook,
    ) -> None:
        left = _px(54, s)
        right = width - left
        line_y = height - _px(70, s)
        draw.line([(left, line_y), (right, line_y)], fill=COLOR_RULE, width=1)

        icon_y = line_y + _px(18, s)
        draw.rounded_rectangle(
            [left, icon_y, left + _px(22, s), icon_y + _px(13, s)],
            radius=_px(3, s),
            outline=COLOR_MUTED,
            width=max(1, _px(1, s)),
        )
        draw.line(
            [(left + _px(6, s), icon_y + _px(13, s)), (left + _px(4, s), icon_y + _px(17, s))],
            fill=COLOR_MUTED,
            width=max(1, _px(1, s)),
        )
        draw.line(
            [(left + _px(16, s), icon_y + _px(13, s)), (left + _px(18, s), icon_y + _px(17, s))],
            fill=COLOR_MUTED,
            width=max(1, _px(1, s)),
        )
        brand_font = fonts.font(11 * s, bold=True)
        draw.text(
            (left + _px(32, s), icon_y - _px(1, s)),
            "GAME DAILY",
            font=brand_font,
            fill=COLOR_BODY,
        )
        note_font = fonts.font(10.5 * s)
        note = "每日游戏资讯 · 与你一起发现更好的游戏"
        nb = _text_bbox(draw, note, note_font)
        draw.text(
            (right - (nb[2] - nb[0]), icon_y - _px(1, s)),
            note,
            font=note_font,
            fill=COLOR_MUTED,
        )

    # ------------------------------------------------------------------
    # Images
    # ------------------------------------------------------------------
    def _preload_images(
        self,
        items: list[NewsItem],
        deals: list[SteamDeal],
    ) -> dict[str, Image.Image | None]:
        urls = [item.image_url for item in items if item.image_url]
        urls += [deal.image_url for deal in deals if deal.image_url]
        urls = list(dict.fromkeys(urls))
        if not urls:
            return {}
        with ThreadPoolExecutor(max_workers=min(6, len(urls))) as pool:
            loaded = list(pool.map(self._load_image, urls))
        return dict(zip(urls, loaded))

    def _load_image(self, url: str) -> Image.Image | None:
        if not url:
            return None
        cached = self._image_cache.get(url)
        if cached is not None:
            return cached
        if not url.lower().startswith(("http://", "https://")):
            return None
        cache_dir = self.config.output_dir / ".image-cache"
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        key = hashlib.sha1(url.encode("utf-8", "ignore")).hexdigest()
        cache_path = cache_dir / f"{key}.img"
        image: Image.Image | None = None
        if cache_path.is_file():
            try:
                with Image.open(cache_path) as cached_image:
                    image = cached_image.convert("RGB").copy()
            except Exception:
                image = None
        if image is None:
            try:
                request = urllib.request.Request(
                    url,
                    headers={"User-Agent": self.config.user_agent},
                )
                with urllib.request.urlopen(request, timeout=8) as response:
                    data = response.read(6_000_000)
                with Image.open(io.BytesIO(data)) as downloaded:
                    image = downloaded.convert("RGB")
                image.load()
                try:
                    image.save(cache_path, format="PNG")
                except OSError:
                    pass
            except Exception as exc:  # noqa: BLE001
                self.log.debug("图片加载失败 %s: %s", url[:120], exc)
                image = None
        self._image_cache[url] = image
        return image
