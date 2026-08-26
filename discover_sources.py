#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
import free_discovery

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run free chapter-aware source discovery.")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--max-queries", type=int, default=60)
    parser.add_argument("--per-source", type=int, default=10)
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--no-dynamic-expansion", action="store_true")
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
    print(json.dumps(report, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
