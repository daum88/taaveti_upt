#!/usr/bin/env python3
"""Hydrate cached OHLCV and news evidence for active instruments."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from adapters.sqlite.connection import close_db, init_db  # noqa: E402
from application.initialization import warmup_cache  # noqa: E402
from settings import load_settings  # noqa: E402


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S"
    )
    settings = load_settings()
    try:
        init_db()
        result = warmup_cache(settings)
    finally:
        close_db()
    print(f"Warmup complete: {result.ohlcv_bars} OHLCV bars, {result.news_articles} news articles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
