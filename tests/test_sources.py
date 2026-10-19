from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from .helpers import make_config, make_item

from astrbot_plugin_game_daily.news_sources import (
    SourceSpec,
    build_sources,
    clean_html_text,
    parse_datetime_value,
    parse_rss_feed,
)

RSS_SAMPLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>Test Feed</title>
    <link>https://example.com/</link>
    <item>
      <title>New Game &amp; Update</title>
      <link>https://example.com/news/1?utm_source=rss</link>
      <description><![CDATA[<p>Hello <b>world</b> &amp; more.</p>]]></description>
      <content:encoded><![CDATA[<p>Full content here.</p>]]></content:encoded>
      <enclosure url="https://example.com/image.jpg" type="image/jpeg" length="1" />
      <pubDate>Sun, 18 Oct 2026 08:00:00 +0800</pubDate>
      <category>Games</category>
    </item>
    <item>
      <title>No Date Item</title>
      <link>/news/2</link>
      <description>No timestamp.</description>
    </item>
    <item>
      <title></title>
      <link>https://example.com/news/3</link>
    </item>
  </channel>
</rss>
"""

ATOM_SAMPLE = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Feed</title>
  <link href="https://atom.example/" />
  <entry>
    <title>Atom Entry</title>
    <link rel="alternate" href="/posts/1" />
    <summary type="html">&lt;p&gt;Atom summary&lt;/p&gt;</summary>
    <published>2026-10-17T23:30:00Z</published>
    <id>tag:example,2026:1</id>
  </entry>
</feed>
"""


class SourceParserTests(unittest.TestCase):
    def setUp(self):
        self.tz = ZoneInfo("Asia/Shanghai")
        self.spec = SourceSpec(
            "test", "测试来源", "综合", 1.0, url="https://example.com/feed"
        )

    def test_rss_parsing(self):
        items = parse_rss_feed(RSS_SAMPLE, self.spec, self.tz)
        self.assertEqual(len(items), 2)
        first = items[0]
        self.assertEqual(first.title, "New Game & Update")
        self.assertEqual(first.link, "https://example.com/news/1")
        self.assertIn("Hello world", first.summary)
        self.assertIsNotNone(first.published_at)
        self.assertFalse(first.published_missing)
        self.assertEqual(first.image_url, "https://example.com/image.jpg")
        self.assertEqual(first.category, "综合")
        second = items[1]
        self.assertEqual(second.link, "https://example.com/news/2")
        self.assertIsNone(second.published_at)
        self.assertTrue(second.published_missing)

    def test_atom_parsing(self):
        items = parse_rss_feed(ATOM_SAMPLE, self.spec, self.tz)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Atom Entry")
        self.assertEqual(items[0].link, "https://atom.example/posts/1")
        self.assertIn("Atom summary", items[0].summary)
        self.assertIsNotNone(items[0].published_at)

    def test_invalid_xml_raises(self):
        with self.assertRaises(Exception):
            parse_rss_feed(b"<rss><channel><item>", self.spec, self.tz)

    def test_clean_html(self):
        source = "&lt;p&gt;Hello &amp;amp; world&lt;/p&gt;<script>alert(1)</script>"
        cleaned = clean_html_text(source)
        self.assertIn("Hello", cleaned)
        self.assertNotIn("alert", cleaned)
        self.assertNotIn("<", cleaned)

    def test_date_formats(self):
        parsed, missing = parse_datetime_value(
            "Sun, 18 Oct 2026 08:00:00 +0800", self.tz
        )
        self.assertFalse(missing)
        self.assertEqual(parsed.astimezone(self.tz).hour, 8)
        parsed, missing = parse_datetime_value("2026-10-18T08:00:00+08:00", self.tz)
        self.assertFalse(missing)
        self.assertEqual(parsed.astimezone(self.tz).hour, 8)
        parsed, missing = parse_datetime_value("not a date", self.tz)
        self.assertTrue(missing)
        self.assertIsNone(parsed)

    def test_duplicate_urls_in_feed_are_removed(self):
        data = RSS_SAMPLE.replace(b"/news/2", b"/news/1")
        items = parse_rss_feed(data, self.spec, self.tz)
        self.assertEqual(len(items), 1)


class BuildSourcesTests(unittest.TestCase):
    def test_disable_and_custom_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(
                Path(tmp),
                disabled_sources=["steam_api", "dotesports"],
                rss_sources=[
                    {
                        "name": "我的源",
                        "url": "https://custom.example/feed",
                        "category": "独立",
                        "weight": 1.7,
                        "enabled": True,
                    },
                    {
                        "name": "PC Gamer 自定义",
                        "url": "https://www.pcgamer.com/rss/",
                        "category": "PC",
                        "weight": 2.0,
                        "enabled": True,
                    },
                ],
            )
            sources = build_sources(cfg)
            ids = {source.id for source in sources}
            self.assertNotIn("steam_api", ids)
            self.assertNotIn("dotesports", ids)
            self.assertIn("pcgamer", ids)
            pcgamer = next(source for source in sources if source.id == "pcgamer")
            self.assertEqual(pcgamer.name, "PC Gamer 自定义")
            self.assertEqual(pcgamer.weight, 2.0)
            custom = [source for source in sources if source.id.startswith("custom-")]
            self.assertEqual(len(custom), 1)
            self.assertEqual(custom[0].category, "独立")

    def test_steam_api_disabled_without_appids(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp), steam_appids=[])
            ids = {source.id for source in build_sources(cfg)}
            self.assertNotIn("steam_api", ids)

    def test_custom_source_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(
                Path(tmp),
                rss_sources=[
                    {
                        "name": "关闭的源",
                        "url": "https://off.example/feed",
                        "enabled": False,
                    }
                ],
            )
            ids = {source.id for source in build_sources(cfg)}
            self.assertFalse(any(source.startswith("custom-") for source in ids))

    def test_disabled_custom_entry_can_disable_builtin(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(
                Path(tmp),
                rss_sources=[
                    {
                        "name": "PC Gamer",
                        "url": "https://www.pcgamer.com/rss/",
                        "enabled": False,
                    }
                ],
            )
            ids = {source.id for source in build_sources(cfg)}
            self.assertNotIn("pcgamer", ids)


class SteamApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_official_feed_items_are_kept(self):
        import json
        from datetime import datetime, timezone

        from astrbot_plugin_game_daily.news_sources import (
            NewsFetcher,
            SourceSpec,
        )

        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp), steam_appids=["730"])
            fetcher = NewsFetcher(cfg)
            now = datetime(2026, 10, 18, 8, 30, tzinfo=timezone.utc)
            payload = json.dumps(
                {
                    "appnews": {
                        "newsitems": [
                            {
                                "gid": "1",
                                "title": "Official Update",
                                "url": "https://store.steampowered.com/news/app/730/view/1",
                                "contents": "<p>Official content</p>",
                                "date": int(now.timestamp()),
                                "feed_type": 1,
                                "feedname": "steam_community_announcements",
                            },
                            {
                                "gid": "2",
                                "title": "External Post",
                                "url": "https://store.steampowered.com/news/externalpost/x/2",
                                "contents": "<p>External content</p>",
                                "date": int(now.timestamp()),
                                "feed_type": 0,
                                "feedname": "PC Gamer",
                            },
                        ]
                    }
                }
            ).encode("utf-8")

            async def fake_request(session, url, params=None):
                return payload

            fetcher._request_bytes = fake_request  # type: ignore[method-assign]
            spec = SourceSpec(
                "steam_api",
                "Steam 官方公告",
                "更新",
                1.6,
                "steam_api",
                appids=("730",),
            )
            result = await fetcher._fetch_steam_api(None, spec, now)
            self.assertTrue(result.ok)
            self.assertEqual(len(result.items), 1)
            self.assertEqual(result.items[0].title, "Official Update")
            self.assertIn("Steam 公告", result.items[0].source)


class RuleModelTests(unittest.TestCase):
    def test_category_rules(self):
        from astrbot_plugin_game_daily.news_service import categorize

        self.assertEqual(categorize("Steam 免费领取", "", "综合"), "免费/福利")
        self.assertEqual(categorize("Summer Sale discount", "", "综合"), "折扣")
        self.assertEqual(categorize("T1 wins championship", "", "综合"), "电竞")
        self.assertEqual(categorize("版本更新补丁", "", "综合"), "更新")
        self.assertEqual(categorize("工作室被收购", "", "综合"), "行业")
        self.assertEqual(categorize("Nintendo Switch news", "", "综合"), "主机")
        self.assertEqual(categorize("indie showcase", "", "综合"), "独立")
        self.assertEqual(categorize("某游戏消息", "", "PC"), "PC")
        self.assertEqual(categorize("某游戏消息", "", "综合"), "综合")

    def test_rule_score_prefers_fresh_official(self):
        from datetime import timedelta

        from astrbot_plugin_game_daily.news_service import _rule_score

        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))
            now = make_item(1).collected_at
            fresh = make_item(
                1,
                title="官方宣布发售日期",
                summary="官方宣布了重要消息。" * 8,
                category="发售",
                published_at=now - timedelta(hours=1),
                source_weight=1.5,
            )
            old = make_item(
                2,
                title="普通消息",
                summary="普通消息。",
                category="综合",
                published_at=now - timedelta(hours=23),
                source_weight=1.0,
            )
            self.assertGreater(
                _rule_score(fresh, now, cfg), _rule_score(old, now, cfg)
            )


if __name__ == "__main__":
    unittest.main()
