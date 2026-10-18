from __future__ import annotations

import unittest
from datetime import timezone

from .helpers import make_item

from astrbot_plugin_game_daily.models import (
    content_fingerprint,
    ensure_aware,
    normalize_title,
    normalize_url,
)


class ModelTests(unittest.TestCase):
    def test_normalize_url_strips_tracking(self):
        self.assertEqual(
            normalize_url(
                "https://Example.COM:443/news/?utm_source=x&id=42#comments"
            ),
            "https://example.com/news?id=42",
        )
        self.assertEqual(
            normalize_url("https://example.com/a/?fbclid=1&gclid=2"),
            "https://example.com/a",
        )

    def test_normalize_url_keeps_functional_params(self):
        self.assertEqual(
            normalize_url("https://store.steampowered.com/news/app/730?l=schinese"),
            "https://store.steampowered.com/news/app/730?l=schinese",
        )

    def test_normalize_title_removes_punctuation_and_case(self):
        self.assertEqual(normalize_title("Hello, World! 你好"), "helloworld你好")
        self.assertEqual(normalize_title("ＡＢＣ　１２３"), "abc123")

    def test_fingerprint_is_stable(self):
        first = content_fingerprint("Title", "Summary", "https://example.com/a")
        second = content_fingerprint("Title", "Summary", "https://example.com/a/")
        self.assertEqual(first, second)

    def test_missing_time_stays_missing(self):
        item = make_item(1, published_at=None)
        self.assertIsNone(item.published_at)
        self.assertTrue(item.published_missing)

    def test_ensure_aware_treats_naive_as_utc(self):
        naive = make_item(1).collected_at.replace(tzinfo=None)
        self.assertEqual(ensure_aware(naive).tzinfo, timezone.utc)

    def test_dedupe_keys_include_title_url_content(self):
        item = make_item(1)
        kinds = {key.split(":", 1)[0] for key in item.dedupe_keys()}
        self.assertEqual(kinds, {"title", "url", "content"})

    def test_display_fields_and_merged_sources(self):
        first = make_item(1, title="A")
        second = make_item(2, title="B", source="另一家")
        first.merge_sources_from(second)
        self.assertIn("另一家", first.display_source)
        self.assertEqual(first.display_source.count("测试来源"), 1)


if __name__ == "__main__":
    unittest.main()
