from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from .helpers import make_config, make_item

from astrbot_plugin_game_daily.ai_service import AIError, AIService


class StubAI(AIService):
    def __init__(self, config, response: str | Exception | list[str]):
        super().__init__(config)
        self.response = response
        self.requests: list[list[dict]] = []

    async def _chat(self, messages, max_tokens=3000, temperature=0.2):
        self.requests.append(messages)
        if isinstance(self.response, Exception):
            raise self.response
        if isinstance(self.response, list):
            return self.response.pop(0)
        return self.response


def ai_config(tmp: Path, **overrides):
    raw = {
        "ai_api_key": "test-key",
        "ai_api_base": "https://ai.example/v1",
        "ai_model": "test-model",
        "ai_batch_size": 20,
    }
    raw.update(overrides)
    return make_config(tmp, **raw)


class AIServiceTests(unittest.TestCase):
    def test_unavailable_without_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))
            ai = AIService(cfg)
            result = self._run(ai.enhance([make_item(1)]))
            self.assertFalse(result.used)
            self.assertIn("未启用", " ".join(result.errors))

    def test_valid_response_is_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ai_config(Path(tmp))
            aa = make_item(1, title="Original A", summary="Summary A")
            bb = make_item(2, title="Original B", summary="Summary B")
            response = json.dumps(
                {
                    "items": [
                        {
                            "id": 1,
                            "title": "中文标题 A",
                            "summary": "这是经过整理的中文摘要 A。",
                            "category": "更新",
                            "importance": 88,
                            "duplicate_of": None,
                        },
                        {
                            "id": 2,
                            "title": "中文标题 B",
                            "summary": "这是经过整理的中文摘要 B。",
                            "category": "发售",
                            "importance": 77,
                            "duplicate_of": 1,
                        },
                        {
                            "id": 999,
                            "title": "不应存在",
                            "summary": "未知 id 应被忽略。",
                            "category": "综合",
                            "importance": 100,
                        },
                    ]
                },
                ensure_ascii=False,
            )
            ai = StubAI(cfg, response)
            result = self._run(ai.enhance([aa, bb]))
            self.assertTrue(result.used)
            self.assertEqual(len(result.items), 2)
            self.assertEqual(result.items[1].title, "中文标题 A")
            self.assertEqual(result.items[2].duplicate_of, 1)
            self.assertEqual(aa.original_id, 1)
            self.assertIn("<news_data>", ai.requests[0][1]["content"])
            self.assertIn("不可信", ai.requests[0][0]["content"])
            self.assertIn("Original A", ai.requests[0][1]["content"])

    def test_code_fence_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ai_config(Path(tmp))
            response = (
                "```json\n"
                '{"items":[{"id":1,"title":"标题","summary":"摘要内容足够长。",'
                '"category":"更新","importance":70}]}\n'
                "```"
            )
            ai = StubAI(cfg, response)
            result = self._run(ai.enhance([make_item(1)]))
            self.assertTrue(result.used)
            self.assertEqual(result.items[1].title, "标题")

    def test_invalid_json_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ai_config(Path(tmp))
            ai = StubAI(cfg, "not json at all")
            result = self._run(ai.enhance([make_item(1)]))
            self.assertFalse(result.used)
            self.assertTrue(result.errors)

    def test_network_error_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ai_config(Path(tmp))
            ai = StubAI(cfg, AIError("HTTP 500"))
            result = self._run(ai.enhance([make_item(1)]))
            self.assertFalse(result.used)
            self.assertIn("HTTP 500", result.errors[0])
            self.assertNotIn("test-key", " ".join(result.errors))

    def test_batching(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ai_config(Path(tmp), ai_batch_size=2)
            responses = []
            for start in (1, 3, 5):
                responses.append(
                    json.dumps(
                        {
                            "items": [
                                {
                                    "id": start,
                                    "title": f"标题 {start}",
                                    "summary": "摘要内容足够长。",
                                    "category": "更新",
                                    "importance": 60,
                                }
                            ]
                        },
                        ensure_ascii=False,
                    )
                )
            ai = StubAI(cfg, responses)
            items = [make_item(index) for index in range(1, 6)]
            result = self._run(ai.enhance(items))
            self.assertEqual(result.requests, 3)
            self.assertEqual(len(ai.requests), 3)
            self.assertTrue(result.used)

    def test_ping(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ai_config(Path(tmp))
            ai = StubAI(cfg, '{"ok":true}')
            ok, message = self._run(ai.ping())
            self.assertTrue(ok)
            self.assertIn("test-model", message)

    @staticmethod
    def _run(coro):
        import asyncio

        return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main()
