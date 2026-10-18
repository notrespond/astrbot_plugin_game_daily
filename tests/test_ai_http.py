from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aiohttp import web

from .helpers import make_config, make_item

from astrbot_plugin_game_daily.ai_service import AIService


class AIHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.captured = []
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self.ok_handler)
        app.router.add_post("/fail/chat/completions", self.fail_handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        sock = self.site._server.sockets[0]
        self.port = sock.getsockname()[1]
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def ok_handler(self, request: web.Request):
        self.captured.append(
            {
                "auth": request.headers.get("Authorization"),
                "body": await request.json(),
            }
        )
        content = json.dumps(
            {
                "items": [
                    {
                        "id": 1,
                        "title": "本地模拟中文标题",
                        "summary": "这是本地模拟接口返回的中文摘要。",
                        "category": "更新",
                        "importance": 85,
                    }
                ]
            },
            ensure_ascii=False,
        )
        return web.json_response(
            {"choices": [{"message": {"content": content}}]}
        )

    async def fail_handler(self, request: web.Request):
        return web.Response(status=500, text="mock failure")

    def _config(self, base_suffix="/v1", **overrides):
        raw = {
            "ai_api_key": "local-test-key",
            "ai_api_base": f"http://127.0.0.1:{self.port}{base_suffix}",
            "ai_model": "local-model",
            "use_proxy_env": False,
            "ai_timeout": 10,
        }
        raw.update(overrides)
        return make_config(Path(self.tmp.name), **raw)

    async def test_real_http_request_and_parse(self):
        ai = AIService(self._config())
        result = await ai.enhance([make_item(1, title="Untrusted title")])
        self.assertTrue(result.used)
        self.assertEqual(result.items[1].title, "本地模拟中文标题")
        self.assertEqual(len(self.captured), 1)
        captured = self.captured[0]
        self.assertEqual(captured["auth"], "Bearer local-test-key")
        self.assertEqual(captured["body"]["model"], "local-model")
        self.assertEqual(captured["body"]["messages"][0]["role"], "system")
        self.assertIn("不可信", captured["body"]["messages"][0]["content"])
        self.assertIn("<news_data>", captured["body"]["messages"][1]["content"])
        self.assertIn("Untrusted title", captured["body"]["messages"][1]["content"])

    async def test_http_error_falls_back(self):
        ai = AIService(self._config(base_suffix="/fail"))
        result = await ai.enhance([make_item(1)])
        self.assertFalse(result.used)
        self.assertIn("HTTP 500", result.errors[0])

    async def test_ping(self):
        ai = AIService(self._config())
        ok, message = await ai.ping()
        self.assertTrue(ok)
        self.assertIn("local-model", message)


if __name__ == "__main__":
    unittest.main()
