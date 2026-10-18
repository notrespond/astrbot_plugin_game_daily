"""Manual network smoke test (not part of unittest discovery).

Usage:
    python astrbot_plugin_game_daily/tests/manual_smoke.py
    python astrbot_plugin_game_daily/tests/manual_smoke.py --limit 3 --no-poster

It fetches the enabled public sources, prints per-source results, and renders a
real poster from real collected items.  It never sends messages.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from astrbot_plugin_game_daily.ai_service import AIService
from astrbot_plugin_game_daily.config import PluginConfig
from astrbot_plugin_game_daily.news_service import DailyReportService
from astrbot_plugin_game_daily.news_sources import NewsFetcher, build_sources
from astrbot_plugin_game_daily.poster import PosterRenderer
from astrbot_plugin_game_daily.storage import Storage


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="", help="poster output directory")
    parser.add_argument("--limit", type=int, default=0, help="limit sources (0=all)")
    parser.add_argument("--no-poster", action="store_true")
    args = parser.parse_args()

    work = (
        Path(args.output).expanduser().resolve()
        if args.output
        else Path(tempfile.mkdtemp(prefix="gamedaily-smoke-"))
    )
    work.mkdir(parents=True, exist_ok=True)
    cfg = PluginConfig.from_mapping(
        {
            "ai_enabled": False,
            "retry_times": 1,
            "request_interval": 0.2,
            "request_timeout": 20,
            "max_concurrency": 4,
            "output_dir": str(work / "posters"),
            "max_news": 6,
        },
        work,
    )
    sources = build_sources(cfg)
    if args.limit > 0:
        sources = sources[: args.limit]
    print(f"data_dir={work}")
    print(f"sources={len(sources)}")
    fetcher = NewsFetcher(cfg)
    results = await fetcher.fetch(sources)
    ok = 0
    total = 0
    for result in results:
        status = "OK " if result.ok else "ERR"
        total += len(result.items)
        ok += 1 if result.ok else 0
        print(f"{status} {result.source_id:24} {len(result.items):4} {result.error[:120]}")
    print(f"sources_ok={ok}/{len(results)} items={total}")

    storage = Storage(cfg.db_path)
    service = DailyReportService(
        cfg, storage, fetcher, AIService(cfg), PosterRenderer(cfg)
    )
    report = await service.generate(
        use_sent_filter=False,
        make_poster=not args.no_poster,
        poster_when_empty=True,
    )
    print(f"status={report.status} {report.stats.summary_text()}")
    print(f"poster={report.poster_path}")
    for item in report.items:
        print(f" - [{item.category}] {item.display_title} | {item.display_source}")
    if report.errors:
        print("errors:")
        for error in report.errors[:10]:
            print(f" * {error[:240]}")
    storage.close()
    return 0 if report.status in {"ok", "no_news"} else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
