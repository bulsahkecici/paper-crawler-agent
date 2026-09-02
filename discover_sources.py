#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
import free_discovery
import supplemental_discovery


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run free book-agnostic tunnel source discovery.")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--max-queries", type=int, default=60)
    parser.add_argument("--per-source", type=int, default=10)
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--no-dynamic-expansion", action="store_true")
    parser.add_argument("--skip-news-books", action="store_true", help="Skip RSS/Atom and book metadata discovery.")
    parser.add_argument("--max-book-queries", type=int, default=18)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = free_discovery.discover_all(
        output_dir=args.output_dir,
        max_queries=max(1, args.max_queries),
        per_source=max(1, args.per_source),
        acquire=not args.discover_only,
        expand_dynamic=not args.no_dynamic_expansion,
    )
    supplemental = None
    if not args.skip_news_books:
        supplemental = supplemental_discovery.run_supplemental_discovery(
            output_dir=args.output_dir,
            max_book_queries=max(1, args.max_book_queries),
            per_source=max(1, args.per_source),
            acquire_news=not args.discover_only,
        )
    print(json.dumps({"core_discovery": report, "news_and_books": supplemental}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
