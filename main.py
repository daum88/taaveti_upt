#!/usr/bin/env python3
"""Launch the optional Rich terminal dashboard."""

from __future__ import annotations

import argparse
import logging

from settings import load_settings
from ui.terminal import run


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for logger_name in ("yfinance", "urllib3", "google"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-agents", action="store_true", help="Disable LLM agent trading")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    configure_logging(args.verbose)
    run(load_settings(), enable_agents=not args.no_agents)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
