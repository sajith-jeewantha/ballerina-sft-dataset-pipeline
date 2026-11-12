"""CLI for API-based collectors.

    python -m ballerinascraper.collect github [--limit N]
    python -m ballerinascraper.collect central [--limit N]

(The HTML crawl is `scrapy crawl web`, run from scraper/ballerinascraper.)
"""

from __future__ import annotations

import argparse
import json
import sys

from balpipe_common.config import ConfigError, load_settings
from balpipe_common.logging import setup_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ballerinascraper.collect", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", choices=["github", "central"])
    parser.add_argument("--limit", type=int, default=None, help="stop after N new records (smoke tests)")
    parser.add_argument("--config", default=None, help="path to pipeline.yaml")
    parser.add_argument("--log-format", choices=["json", "text"], default=None)
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    setup_logging(settings.logging.level, args.log_format or settings.logging.format)

    if args.source == "github":
        from ballerinascraper.collectors.github import GithubCollector

        counts = GithubCollector(settings, limit=args.limit).run()
    else:
        from ballerinascraper.collectors.central import CentralCollector

        counts = CentralCollector(settings, limit=args.limit).run()
    print(json.dumps({"source": args.source, **counts}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
