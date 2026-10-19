from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image

from .helpers import make_config, make_item

from astrbot_plugin_game_daily.models import ReportStats, SteamDeal
from astrbot_plugin_game_daily.poster import PosterError, PosterRenderer


class PosterTests(unittest.TestCase):
    def _render(self, items, empty=False):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = make_config(Path(self.tmp.name))
        now = datetime(2026, 10, 18, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        try:
            renderer = PosterRenderer(cfg)
            return renderer.render(items, self._stats(items), now, empty=empty)
        except PosterError as exc:
            self.skipTest(f"当前环境没有可用中文字体：{exc}")

    @staticmethod
    def _stats(items):
        return ReportStats(
            total_collected=len(items) + 10,
            total_in_window=len(items) + 4,
            total_after_local_dedupe=len(items),
            total_selected=len(items),
            sources_total=10,
            sources_ok=9,
            sources_failed=1,
        )

    def test_six_cards_poster_size_and_file(self):
        items = [
            make_item(
                i,
                title=f"测试新闻标题 {i}：这是一个用于检查排版的中文标题",
                summary="这是一段较长的中文摘要，用于检查文字换行、卡片间距和底部来源行是否正常。"
                * 2,
                category=["更新", "发售", "免费/福利", "电竞", "行业", "主机"][i - 1],
                source_id=f"source-{i}",
                source=f"来源 {i}",
                published_at=datetime(2026, 10, 18, 0, i, tzinfo=timezone.utc),
            )
            for i in range(1, 7)
        ]
        path = self._render(items)
        self.assertTrue(Path(path).is_file())
        self.assertGreater(Path(path).stat().st_size, 20_000)
        with Image.open(path) as image:
            self.assertEqual(image.size, (1200, 1600))
            self.assertEqual(image.format, "PNG")

    def test_long_title_emoji_and_control_characters(self):
        item = make_item(
            1,
            title="超长标题" * 40 + " 🎮🎮\x00\x07",
            summary="摘要也超长" * 80 + " 🎮",
            source="来源" * 30,
        )
        path = self._render([item])
        self.assertTrue(Path(path).is_file())

    def test_four_cards(self):
        items = [make_item(i) for i in range(1, 5)]
        path = self._render(items)
        with Image.open(path) as image:
            self.assertEqual(image.size, (1200, 1600))

    def test_empty_poster(self):
        path = self._render([], empty=True)
        self.assertTrue(Path(path).is_file())
        with Image.open(path) as image:
            self.assertEqual(image.size, (1200, 1600))

    def test_steam_deals_section(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = make_config(Path(self.tmp.name))
        now = datetime(2026, 10, 19, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        deals = [
            SteamDeal(1, "测试游戏一", 50, 29800, 14900, genres=["动作", "开放世界"]),
            SteamDeal(2, "测试游戏二", 67, 24900, 8200, genres=["角色扮演", "剧情"]),
            SteamDeal(3, "测试游戏三", 75, 4800, 1200, genres=["独立", "探索"]),
            SteamDeal(4, "测试游戏四", 90, 38400, 3840, genres=["冒险", "魔法"]),
        ]
        items = [make_item(i) for i in range(1, 7)]
        try:
            path = PosterRenderer(cfg).render(
                items,
                self._stats(items),
                now,
                ai_used=True,
                deals=deals,
            )
        except PosterError as exc:
            self.skipTest(f"当前环境没有可用中文字体：{exc}")
        with Image.open(path) as image:
            self.assertEqual(image.size, (1200, 1600))

    def test_custom_size(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = make_config(
            Path(self.tmp.name), poster_width=900, poster_height=1200
        )
        now = datetime(2026, 10, 18, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        try:
            renderer = PosterRenderer(cfg)
            path = renderer.render(
                [make_item(1)], self._stats([make_item(1)]), now
            )
        except PosterError as exc:
            self.skipTest(f"当前环境没有可用中文字体：{exc}")
        with Image.open(path) as image:
            self.assertEqual(image.size, (900, 1200))


if __name__ == "__main__":
    unittest.main()
