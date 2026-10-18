"""Pillow poster renderer for the Game Daily report.

The renderer is intentionally self-contained: no online image service, no
external template, no remote thumbnails.  It draws a 16:9 editorial poster with
a dark gradient, cards, dividers and typographic hierarchy.

Fonts are discovered from the configured path first and then from common
Linux/Windows/macOS locations.  If no CJK-capable font exists, rendering fails
with a clear error instead of silently producing tofu/garbled Chinese.
"""

from __future__ import annotations

import logging
import math
import os
import re
import shutil
import subprocess
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .config import PluginConfig
from .models import NewsItem, ReportStats

log = logging.getLogger(__name__)

_EMOJI_RE = re.compile(
    "[\U0001f000-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff"
    "\ufe0f\u200d\u20e3]"
)


class PosterError(RuntimeError):
    pass


@dataclass(frozen=True)
class PosterTheme:
    bg_top: tuple[int, int, int]
    bg_bottom: tuple[int, int, int]
    accent: tuple[int, int, int]
    accent2: tuple[int, int, int]
    text: tuple[int, int, int]
    muted: tuple[int, int, int]
    card: tuple[int, int, int]
    border: tuple[int, int, int]


THEMES: dict[str, PosterTheme] = {
    "dark-neon": PosterTheme(
        bg_top=(11, 16, 31),
        bg_bottom=(4, 6, 14),
        accent=(57, 224, 255),
        accent2=(166, 118, 255),
        text=(244, 248, 255),
        muted=(148, 162, 190),
        card=(18, 25, 43),
        border=(255, 255, 255),
    ),
    "dark-blue": PosterTheme(
        bg_top=(8, 19, 40),
        bg_bottom=(3, 8, 20),
        accent=(79, 151, 255),
        accent2=(49, 214, 255),
        text=(241, 247, 255),
        muted=(145, 164, 194),
        card=(15, 29, 54),
        border=(255, 255, 255),
    ),
    "dark-crimson": PosterTheme(
        bg_top=(28, 12, 22),
        bg_bottom=(9, 4, 10),
        accent=(255, 92, 116),
        accent2=(255, 174, 92),
        text=(255, 246, 246),
        muted=(187, 156, 166),
        card=(40, 18, 30),
        border=(255, 255, 255),
    ),
}

CATEGORY_COLORS: dict[str, tuple[int, int, int]] = {
    "发售": (76, 226, 154),
    "更新": (57, 210, 255),
    "免费/福利": (255, 200, 77),
    "折扣": (255, 148, 73),
    "电竞": (255, 105, 180),
    "行业": (116, 152, 255),
    "主机": (164, 126, 255),
    "PC": (90, 224, 230),
    "独立": (130, 232, 190),
    "综合": (180, 192, 214),
}

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


def _lerp_color(
    start: tuple[int, int, int], end: tuple[int, int, int], ratio: float
) -> tuple[int, int, int]:
    return tuple(int(round(a + (b - a) * ratio)) for a, b in zip(start, end))


class PosterRenderer:
    def __init__(self, config: PluginConfig, logger=None) -> None:
        self.config = config
        self.log = logger or log
        self._font_book: FontBook | None = None
        self._lock = threading.RLock()

    @property
    def theme(self) -> PosterTheme:
        return THEMES.get(self.config.poster_theme, THEMES["dark-neon"])

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
    ) -> str:
        try:
            return self._render(
                items, stats, now, empty, filename_prefix, ai_used
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
    ) -> str:
        width = self.config.poster_width
        height = self.config.poster_height
        if width < 400 or height < 240:
            raise PosterError("海报尺寸过小")
        s = min(width / 1200.0, height / 675.0)
        if s <= 0:
            raise PosterError("海报尺寸无效")

        fonts = self._fonts()
        theme = self.theme
        now_local = now.astimezone(self.config.timezone)
        image = self._background(width, height, s, theme)
        draw = ImageDraw.Draw(image)

        self._draw_header(draw, width, s, now_local, theme, fonts, ai_used)
        content_top = int(round(136 * s))
        content_bottom = height - int(round(66 * s))
        margin = int(round(44 * s))

        if empty or not items:
            self._draw_empty(
                image, draw, width, height, s, theme, fonts, content_top, content_bottom
            )
        else:
            self._draw_cards(
                image,
                draw,
                items[:6],
                width,
                height,
                margin,
                content_top,
                content_bottom,
                s,
                theme,
                fonts,
            )

        self._draw_footer(
            draw, width, height, s, theme, fonts, stats, now_local
        )

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

    def _background(
        self, width: int, height: int, s: float, theme: PosterTheme
    ) -> Image.Image:
        image = Image.new("RGB", (width, height), theme.bg_bottom)
        draw = ImageDraw.Draw(image)
        for y in range(height):
            ratio = y / max(1, height - 1)
            draw.line(
                [(0, y), (width, y)],
                fill=_lerp_color(theme.bg_top, theme.bg_bottom, ratio),
            )

        overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        glow_draw = ImageDraw.Draw(overlay)
        glow_draw.ellipse(
            [
                int(-width * 0.18),
                int(-height * 0.30),
                int(width * 0.52),
                int(height * 0.55),
            ],
            fill=(*theme.accent, 46),
        )
        glow_draw.ellipse(
            [
                int(width * 0.55),
                int(height * 0.55),
                int(width * 1.18),
                int(height * 1.30),
            ],
            fill=(*theme.accent2, 38),
        )
        overlay = overlay.filter(ImageFilter.GaussianBlur(max(24, int(80 * s))))
        image = Image.alpha_composite(image.convert("RGBA"), overlay)

        grid = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        grid_draw = ImageDraw.Draw(grid)
        step = max(24, int(64 * s))
        for x in range(-height, width, step):
            grid_draw.line(
                [(x, 0), (x + height, height)],
                fill=(255, 255, 255, 6),
                width=1,
            )
        for y in range(0, height, step):
            grid_draw.line(
                [(0, y), (width, y)], fill=(255, 255, 255, 5), width=1
            )
        grid_draw.ellipse(
            [
                int(width * 0.72),
                int(-height * 0.16),
                int(width * 1.06),
                int(height * 0.34),
            ],
            outline=(*theme.accent, 28),
            width=max(1, int(2 * s)),
        )
        grid_draw.ellipse(
            [
                int(width * 0.77),
                int(-height * 0.10),
                int(width * 1.01),
                int(height * 0.26),
            ],
            outline=(*theme.accent2, 20),
            width=max(1, int(1 * s)),
        )
        image = Image.alpha_composite(image, grid)
        return image

    def _draw_header(
        self,
        draw: ImageDraw.ImageDraw,
        width: int,
        s: float,
        now: datetime,
        theme: PosterTheme,
        fonts: FontBook,
        ai_used: bool,
    ) -> None:
        left = int(round(44 * s))
        top = int(round(26 * s))
        accent_bar = [left, top + int(4 * s), left + int(5 * s), top + int(78 * s)]
        draw.rounded_rectangle(accent_bar, radius=int(3 * s), fill=theme.accent)

        title_font = fonts.font(40 * s, bold=True)
        subtitle_font = fonts.font(17 * s)
        tiny_font = fonts.font(12 * s)
        date_font = fonts.font(22 * s, bold=True)
        date_small = fonts.font(12 * s)

        title_x = left + int(18 * s)
        _draw_tracked(
            draw,
            title_x,
            top,
            "GAME DAILY",
            title_font,
            theme.text,
            max(1.0, 2.4 * s),
        )
        draw.text(
            (title_x + int(1 * s), top + int(52 * s)),
            "每日游戏情报",
            font=subtitle_font,
            fill=theme.muted,
        )
        dot_x = title_x + int(112 * s)
        dot_y = top + int(60 * s)
        draw.ellipse(
            [dot_x, dot_y, dot_x + int(4 * s), dot_y + int(4 * s)],
            fill=theme.accent,
        )
        draw.text(
            (dot_x + int(12 * s), top + int(52 * s)),
            "PC · 主机 · 独立 · 电竞",
            font=tiny_font,
            fill=theme.muted,
        )

        date_text = now.strftime("%Y.%m.%d")
        date_bbox = _text_bbox(draw, date_text, date_font)
        date_w = date_bbox[2] - date_bbox[0]
        draw.text(
            (width - left - date_w, top + int(2 * s)),
            date_text,
            font=date_font,
            fill=theme.text,
        )
        weekday = _WEEKDAYS[now.weekday()]
        week = now.isocalendar().week
        sub_text = f"{weekday} · 第 {week} 周"
        sub_bbox = _text_bbox(draw, sub_text, date_small)
        sub_w = sub_bbox[2] - sub_bbox[0]
        draw.text(
            (width - left - sub_w, top + int(32 * s)),
            sub_text,
            font=date_small,
            fill=theme.muted,
        )
        tag = "AI 精选" if ai_used else "本地精选"
        tag_font = fonts.font(11 * s, bold=True)
        tag_bbox = _text_bbox(draw, tag, tag_font)
        tag_w = tag_bbox[2] - tag_bbox[0] + int(16 * s)
        tag_h = int(20 * s)
        tag_x2 = width - left
        tag_x1 = tag_x2 - tag_w
        tag_y = top + int(52 * s)
        draw.rounded_rectangle(
            [tag_x1, tag_y, tag_x2, tag_y + tag_h],
            radius=int(10 * s),
            fill=(*theme.accent, 20),
            outline=(*theme.accent, 80),
            width=1,
        )
        draw.text(
            (tag_x1 + int(8 * s), tag_y + int(3 * s)),
            tag,
            font=tag_font,
            fill=theme.accent,
        )

        divider_y = int(round(118 * s))
        draw.line(
            [(left, divider_y), (width - left, divider_y)],
            fill=(*theme.border, 26),
            width=1,
        )
        accent_end = left + int(round(118 * s))
        draw.line(
            [(left, divider_y), (accent_end, divider_y)],
            fill=(*theme.accent, 190),
            width=max(1, int(round(2 * s))),
        )

    def _card_rects(
        self,
        count: int,
        width: int,
        margin: int,
        content_top: int,
        content_bottom: int,
        s: float,
    ) -> list[tuple[int, int, int, int]]:
        content_h = max(80, content_bottom - content_top)
        gap = max(8, int(round(12 * s)))
        card_w = (width - 2 * margin - gap) // 2
        rects: list[tuple[int, int, int, int]] = []
        if count <= 0:
            return rects
        if count <= 2:
            row_h = min(content_h, int(round(300 * s)))
            top = content_top + (content_h - row_h) // 2
            if count == 1:
                return [(margin, top, width - margin, top + row_h)]
            return [
                (margin, top, margin + card_w, top + row_h),
                (margin + card_w + gap, top, width - margin, top + row_h),
            ]
        rows = math.ceil(count / 2)
        row_h = (content_h - (rows - 1) * gap) // rows
        for row in range(rows):
            top = content_top + row * (row_h + gap)
            remaining = count - row * 2
            if remaining == 1:
                rects.append((margin, top, width - margin, top + row_h))
            else:
                rects.append((margin, top, margin + card_w, top + row_h))
                rects.append(
                    (
                        margin + card_w + gap,
                        top,
                        margin + card_w + gap + card_w,
                        top + row_h,
                    )
                )
        return rects[:count]

    def _draw_cards(
        self,
        image: Image.Image,
        draw: ImageDraw.ImageDraw,
        items: list[NewsItem],
        width: int,
        height: int,
        margin: int,
        content_top: int,
        content_bottom: int,
        s: float,
        theme: PosterTheme,
        fonts: FontBook,
    ) -> None:
        rects = self._card_rects(
            len(items), width, margin, content_top, content_bottom, s
        )
        for index, (item, rect) in enumerate(zip(items, rects), start=1):
            self._draw_card(
                image,
                draw,
                item,
                index,
                rect,
                s,
                theme,
                fonts,
            )

    def _draw_card(
        self,
        image: Image.Image,
        draw: ImageDraw.ImageDraw,
        item: NewsItem,
        index: int,
        rect: tuple[int, int, int, int],
        s: float,
        theme: PosterTheme,
        fonts: FontBook,
    ) -> None:
        x1, y1, x2, y2 = rect
        card_w = max(1, x2 - x1)
        card_h = max(1, y2 - y1)
        radius = int(round(16 * s))
        pad = int(round(14 * s))

        overlay = Image.new("RGBA", (card_w, card_h), (0, 0, 0, 0))
        card_draw = ImageDraw.Draw(overlay)
        card_draw.rounded_rectangle(
            [0, 0, card_w - 1, card_h - 1],
            radius=radius,
            fill=(*theme.card, 218),
            outline=(*theme.border, 30),
            width=1,
        )
        accent_w = max(2, int(round(3 * s)))
        card_draw.rounded_rectangle(
            [0, int(round(14 * s)), accent_w, card_h - int(round(14 * s))],
            radius=accent_w,
            fill=(*theme.accent, 205),
        )
        image.alpha_composite(overlay, dest=(x1, y1))
        draw = ImageDraw.Draw(image)

        # Index badge and category chip share the top row.
        badge_size = int(round(26 * s))
        badge_x = x1 + pad
        badge_y = y1 + pad
        category = item.category if item.category in CATEGORY_COLORS else "综合"
        category_color = CATEGORY_COLORS[category]
        index_font = fonts.font(11 * s, bold=True)
        card_draw = ImageDraw.Draw(image)
        card_draw.rounded_rectangle(
            [badge_x, badge_y, badge_x + badge_size, badge_y + badge_size],
            radius=int(round(8 * s)),
            fill=(*category_color, 26),
            outline=(*category_color, 82),
            width=1,
        )
        index_text = f"{index:02d}"
        idx_bbox = _text_bbox(draw, index_text, index_font)
        draw.text(
            (
                badge_x + (badge_size - (idx_bbox[2] - idx_bbox[0])) / 2,
                badge_y + (badge_size - (idx_bbox[3] - idx_bbox[1])) / 2 - idx_bbox[1],
            ),
            index_text,
            font=index_font,
            fill=category_color,
        )

        chip_font = fonts.font(10.5 * s, bold=True)
        chip_text = category
        chip_bbox = _text_bbox(draw, chip_text, chip_font)
        chip_w = chip_bbox[2] - chip_bbox[0] + int(round(16 * s))
        chip_h = int(round(21 * s))
        chip_x2 = x2 - pad
        chip_x1 = chip_x2 - chip_w
        chip_y = y1 + pad + max(0, (badge_size - chip_h) // 2)
        draw.rounded_rectangle(
            [chip_x1, chip_y, chip_x2, chip_y + chip_h],
            radius=int(round(10 * s)),
            fill=(*category_color, 20),
            outline=(*category_color, 75),
            width=1,
        )
        draw.text(
            (
                chip_x1 + int(round(8 * s)),
                chip_y + (chip_h - (chip_bbox[3] - chip_bbox[1])) / 2 - chip_bbox[1],
            ),
            chip_text,
            font=chip_font,
            fill=category_color,
        )

        top_row_h = max(badge_size, chip_h)
        title_y = y1 + pad + top_row_h + int(round(7 * s))
        source_h = int(round(15 * s))
        source_y = y2 - pad - source_h
        text_x = x1 + pad
        text_w = card_w - pad * 2

        base_card_h = card_h / max(0.01, s)
        base_title = max(17.0, min(29.0, (base_card_h - 58) * 0.185))
        title_font = fonts.font(base_title * s, bold=True)
        title_lh = int(round(title_font.size * 1.28))
        title_lines = _wrap_text(item.display_title, title_font, text_w, 2)
        title_block = len(title_lines) * title_lh

        summary_available = source_y - int(round(6 * s)) - (title_y + title_block)
        summary_font = fonts.font(max(11.5, base_title * 0.64) * s)
        summary_lh = int(round(summary_font.size * 1.38))
        max_summary_lines = max(0, min(2, summary_available // max(1, summary_lh)))
        summary_text = item.display_summary or "来源未提供摘要。"
        summary_lines = _wrap_text(
            summary_text, summary_font, text_w, max_summary_lines
        )

        current_y = title_y
        for line in title_lines:
            draw.text(
                (text_x, current_y),
                line,
                font=title_font,
                fill=theme.text,
            )
            current_y += title_lh
        if summary_lines:
            current_y += int(round(4 * s))
            for line in summary_lines:
                draw.text(
                    (text_x, current_y),
                    line,
                    font=summary_font,
                    fill=theme.muted,
                )
                current_y += summary_lh

        # Source / time line pinned to the bottom of the card.
        source_font = fonts.font(10.5 * s)
        dot_r = max(1, int(round(2 * s)))
        dot_y = source_y + source_h // 2
        draw.ellipse(
            [text_x, dot_y, text_x + dot_r * 2, dot_y + dot_r * 2],
            fill=category_color,
        )
        source_name = _clean_display_text(item.display_source) or "未知来源"
        time_text = (
            "时间未标注"
            if item.published_at is None
            else item.published_at.astimezone(self.config.timezone).strftime(
                "%m-%d %H:%M"
            )
        )
        official_prefix = "官方 · " if item.is_official else ""
        source_text = f"{official_prefix}来源 · {source_name} · {time_text}"
        max_source_w = text_w - dot_r * 2 - int(round(8 * s))
        source_line = _wrap_text(source_text, source_font, max_source_w, 1)
        if source_line:
            draw.text(
                (
                    text_x + dot_r * 2 + int(round(7 * s)),
                    source_y + (source_h - source_font.size) // 2 - 1,
                ),
                source_line[0],
                font=source_font,
                fill=theme.muted,
            )

    def _draw_empty(
        self,
        image: Image.Image,
        draw: ImageDraw.ImageDraw,
        width: int,
        height: int,
        s: float,
        theme: PosterTheme,
        fonts: FontBook,
        content_top: int,
        content_bottom: int,
    ) -> None:
        center_x = width // 2
        center_y = (content_top + content_bottom) // 2
        panel_w = int(round(620 * s))
        panel_h = int(round(250 * s))
        overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        panel = ImageDraw.Draw(overlay)
        panel.rounded_rectangle(
            [
                center_x - panel_w // 2,
                center_y - panel_h // 2,
                center_x + panel_w // 2,
                center_y + panel_h // 2,
            ],
            radius=int(round(22 * s)),
            fill=(*theme.card, 190),
            outline=(*theme.border, 25),
            width=1,
        )
        image.alpha_composite(overlay)
        draw = ImageDraw.Draw(image)

        icon_y = center_y - int(round(68 * s))
        radius = int(round(27 * s))
        draw.ellipse(
            [
                center_x - radius,
                icon_y - radius,
                center_x + radius,
                icon_y + radius,
            ],
            outline=(*theme.accent, 120),
            width=max(1, int(round(2 * s))),
        )
        draw.line(
            [
                (center_x - int(round(10 * s)), icon_y),
                (center_x + int(round(10 * s)), icon_y),
            ],
            fill=theme.accent,
            width=max(1, int(round(2 * s))),
        )

        title_font = fonts.font(34 * s, bold=True)
        title = "今日暂无更新"
        title_bbox = _text_bbox(draw, title, title_font)
        draw.text(
            (
                center_x - (title_bbox[2] - title_bbox[0]) / 2,
                center_y - int(round(18 * s)),
            ),
            title,
            font=title_font,
            fill=theme.text,
        )
        subtitle_font = fonts.font(15 * s)
        subtitle = "没有符合条件的游戏资讯，明天见。"
        sub_bbox = _text_bbox(draw, subtitle, subtitle_font)
        draw.text(
            (
                center_x - (sub_bbox[2] - sub_bbox[0]) / 2,
                center_y + int(round(34 * s)),
            ),
            subtitle,
            font=subtitle_font,
            fill=theme.muted,
        )

    def _draw_footer(
        self,
        draw: ImageDraw.ImageDraw,
        width: int,
        height: int,
        s: float,
        theme: PosterTheme,
        fonts: FontBook,
        stats: ReportStats,
        now: datetime,
    ) -> None:
        left = int(round(44 * s))
        right = width - left
        line_y = height - int(round(50 * s))
        draw.line(
            [(left, line_y), (right, line_y)],
            fill=(*theme.border, 22),
            width=1,
        )
        font = fonts.font(10.5 * s)
        small = fonts.font(9.5 * s)
        stats_text = (
            f"采集 {stats.total_collected} 条  ·  去重后 "
            f"{stats.total_after_local_dedupe} 条  ·  展示 "
            f"{stats.total_selected} 条"
        )
        draw.text(
            (left, line_y + int(round(8 * s))),
            stats_text,
            font=font,
            fill=theme.muted,
        )
        update_text = (
            f"数据更新 {now.strftime('%m-%d %H:%M')}  ·  "
            f"{self.config.timezone_name}"
        )
        bbox = _text_bbox(draw, update_text, font)
        update_w = bbox[2] - bbox[0]
        draw.text(
            (right - update_w, line_y + int(round(8 * s))),
            update_text,
            font=font,
            fill=theme.muted,
        )
        source_text = "公开 RSS / 官方新闻接口 · 内容以原文为准"
        source_bbox = _text_bbox(draw, source_text, small)
        source_w = source_bbox[2] - source_bbox[0]
        if width - source_w > left * 2 + 220 * s:
            draw.text(
                (right - source_w, line_y - int(round(14 * s))),
                source_text,
                font=small,
                fill=theme.muted,
            )
