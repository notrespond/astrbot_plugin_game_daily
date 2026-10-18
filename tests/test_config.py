from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from .helpers import make_config

from astrbot_plugin_game_daily.config import (
    DEFAULT_PUSH_TIME,
    DEFAULT_TIMEZONE,
    PluginConfig,
    parse_rss_sources,
)


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))
            self.assertTrue(cfg.enabled)
            self.assertEqual(cfg.push_time, DEFAULT_PUSH_TIME)
            self.assertEqual(cfg.timezone_name, DEFAULT_TIMEZONE)
            self.assertEqual(cfg.effective_cron, "30 8 * * *")
            self.assertEqual(cfg.max_news, 6)
            self.assertEqual(cfg.poster_width, 1200)
            self.assertEqual(cfg.poster_height, 675)
            self.assertTrue(cfg.include_missing_time)
            self.assertFalse(cfg.ai_available)
            self.assertEqual(cfg.ai_key_source, "")

    def test_invalid_values_are_clamped_or_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(
                Path(tmp),
                push_time="99:99",
                timezone="Not/AZone",
                max_news=999,
                max_per_source=0,
                request_timeout=1,
                poster_width=100,
                poster_height=100,
            )
            self.assertEqual(cfg.push_time, "08:30")
            self.assertEqual(cfg.effective_cron, "30 8 * * *")
            self.assertEqual(cfg.timezone_name, DEFAULT_TIMEZONE)
            self.assertEqual(cfg.max_news, 6)
            self.assertEqual(cfg.max_per_source, 1)
            self.assertEqual(cfg.request_timeout, 5)
            self.assertGreaterEqual(cfg.poster_width, 800)
            self.assertGreaterEqual(cfg.poster_height, 450)

    def test_cron_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp), cron_expression="0 9 * * 1-5")
            self.assertEqual(cfg.effective_cron, "0 9 * * 1-5")

    def test_ai_key_from_config_and_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp), ai_api_key="secret-from-config")
            self.assertEqual(cfg.ai_api_key, "secret-from-config")
            self.assertTrue(cfg.ai_available)
            self.assertEqual(cfg.ai_key_source, "config")
            self.assertNotIn("secret-from-config", json.dumps(cfg.public_dict()))

            cfg2 = PluginConfig.from_mapping(
                {"ai_api_key": "", "ai_api_key_env": "MY_GAME_KEY"},
                Path(tmp),
                env={"MY_GAME_KEY": "secret-from-env"},
            )
            self.assertEqual(cfg2.ai_api_key, "secret-from-env")
            self.assertEqual(cfg2.ai_key_source, "env:MY_GAME_KEY")
            self.assertTrue(cfg2.ai_available)

    def test_parse_rss_sources_formats(self):
        raw = [
            {"name": "A", "url": "https://a.example/feed", "category": "PC"},
            "B|https://b.example/feed|主机|1.5",
            "!https://disabled.example/feed",
            {"name": "bad", "url": "ftp://bad.example/feed"},
            {"name": "dupe", "url": "https://a.example/feed"},
        ]
        parsed = parse_rss_sources(raw)
        self.assertEqual(len(parsed), 3)
        self.assertEqual(parsed[0].name, "A")
        self.assertEqual(parsed[1].category, "主机")
        self.assertEqual(parsed[1].weight, 1.5)
        self.assertFalse(parsed[2].enabled)

    def test_output_dir_defaults_under_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))
            self.assertEqual(cfg.output_dir, Path(tmp).resolve() / "posters")
            self.assertTrue(cfg.output_dir.is_dir())

    def test_public_dict_has_no_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp), ai_api_key="top-secret")
            public = cfg.public_dict()
            self.assertTrue(public["ai_key_configured"])
            self.assertNotIn("top-secret", str(public))


if __name__ == "__main__":
    unittest.main()
