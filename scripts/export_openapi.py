#!/usr/bin/env python3
"""Write the current FastAPI contract as a deterministic OpenAPI artifact."""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from adapters.web.app import create_app  # noqa: E402

DEFAULT_OUTPUT = PROJECT_ROOT / "docs" / "openapi.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n")
    print(args.output)


if __name__ == "__main__":
    main()
