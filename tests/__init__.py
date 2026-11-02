"""Game Daily tests."""

import sys
import types


class _StubLogger:
    def _noop(self, *args, **kwargs):
        return None

    debug = info = warning = error = exception = _noop


try:
    import astrbot.api  # noqa: F401
except ModuleNotFoundError:
    _astrbot = types.ModuleType("astrbot")
    _astrbot.__path__ = []
    _api = types.ModuleType("astrbot.api")
    _api.logger = _StubLogger()
    _astrbot.api = _api
    sys.modules.setdefault("astrbot", _astrbot)
    sys.modules.setdefault("astrbot.api", _api)
