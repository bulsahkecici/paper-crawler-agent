#!/usr/bin/env python3
"""One-command PaperCrawler → TunnelBookAI handoff preparation."""

from __future__ import annotations

import argparse
import json

import classify_catalog
import free_discovery
import gap_discovery
import handoff_export
import light_pdf_extract
import source_dedup_audit


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discover, enrich, classify, gap-fill, audit and export TunnelBookAI source handoff.")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--destination", default=None)
    parser.add_argument("--max-queries", type=int, default=60)
    parser.add_argument("--per-source", type=int, default=10)
    parser.add_argument("--light-pdf-pages", type=int, default=3)
    parser.add_argument("--rules-only", action="store_true", help="Disable local embedding/Qwen classification.")
    parser.add_argument("--skip-gap-pass", action="store_true", help="Skip coverage-driven second discovery pass.")
    parser.add_argument("--no-dynamic-expansion", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    discovery_report = free_discovery.discover_all(
        output_dir=args.output_dir,
        max_queries=max(1, args.max_queries),
        per_source=max(1, args.per_source),
        acquire=True,
        expand_dynamic=not args.no_dynamic_expansion,
    )
    light_extract_report = light_pdf_extract.enrich_catalog(
        args.output_dir,
        max_pages=max(1, args.light_pdf_pages),
    )
    first_classification = classify_catalog.classify_catalog(
        args.output_dir,
        use_local_ai=not args.rules_only,
    )
    gap_report = None
    final_classification = first_classification
    if not args.skip_gap_pass:
        gap_report = gap_discovery.run_gap_discovery(args.output_dir)
        if int(gap_report.get("catalog_additions") or 0) > 0:
            light_pdf_extract.enrich_catalog(args.output_dir, max_pages=max(1, args.light_pdf_pages))
            final_classification = classify_catalog.classify_catalog(
                args.output_dir,
                use_local_ai=not args.rules_only,
            )
    source_audit = source_dedup_audit.audit(args.output_dir)
    handoff = handoff_export.export_handoff(args.output_dir, destination=args.destination)
    report = {
        "discovery": discovery_report,
        "light_pdf_extract": light_extract_report,
        "initial_classification": {
            "documents": first_classification.get("documents"),
            "status_counts": first_classification.get("status_counts"),
        },
        "gap_pass": gap_report,
        "final_classification": {
            "documents": final_classification.get("documents"),
            "status_counts": final_classification.get("status_counts"),
            "section_coverage": final_classification.get("section_coverage"),
        },
        "source_dedup_version_audit": {
            "documents": source_audit.get("documents"),
            "exact_duplicate_groups": source_audit.get("exact_duplicate_groups"),
            "same_doi_groups": source_audit.get("same_doi_groups"),
            "fuzzy_review_pairs": source_audit.get("fuzzy_review_pairs"),
        },
        "handoff": handoff,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
