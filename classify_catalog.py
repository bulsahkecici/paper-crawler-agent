#!/usr/bin/env python3
"""Classify an existing PaperCrawler catalog without moving canonical sources.

Outputs are sidecar classification JSON files plus a deterministic audit report.
Physical routing is represented as route_path metadata; moving/copying source
files is intentionally deferred to the handoff/export stage.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import classification_engine as classifier
import tunnel_harvest as harvest


def _classification_dir(output_dir: Path) -> Path:
    path = output_dir / "classifications"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _audit_dir(output_dir: Path) -> Path:
    path = output_dir / "audit"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _stem_for_record(record: dict[str, Any], index: int) -> str:
    pdf = record.get("local_pdf_path") or record.get("pdf_path") or record.get("path")
    if pdf:
        return Path(str(pdf)).stem
    doi = str(record.get("doi") or "").strip()
    if doi:
        return classifier._norm(doi).replace("/", "_").replace(".", "_")[:100]
    title = str(record.get("title") or f"document_{index}")
    return harvest.sanitize_filename(title, max_length=100)


def classify_catalog(output_dir: str | Path | None = None) -> dict[str, Any]:
    if output_dir is not None:
        harvest.set_output_dir(output_dir)
    root = harvest.OUTPUT_DIR
    catalog = harvest.load_catalog()
    papers = catalog.get("papers") or catalog.get("downloaded") or []
    if not isinstance(papers, list):
        raise ValueError("catalog papers must be a list")

    classifications_dir = _classification_dir(root)
    status_counts: Counter[str] = Counter()
    type_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    tier_counts: Counter[str] = Counter()
    route_counts: Counter[str] = Counter()
    section_counts: Counter[str] = Counter()
    topic_counts: Counter[str] = Counter()
    low_confidence: list[dict[str, Any]] = []
    missing_section: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []

    for index, record in enumerate(papers, 1):
        if not isinstance(record, dict):
            continue
        result = classifier.classify_record(record)
        payload = {
            "schema_version": "1.0",
            "document_key": record.get("doi") or record.get("source_sha256") or _stem_for_record(record, index),
            "title": record.get("title"),
            "source_sha256": record.get("source_sha256"),
            "source_path": record.get("local_pdf_path") or record.get("pdf_path") or record.get("path"),
            **result.as_dict(),
        }
        stem = _stem_for_record(record, index)
        dest = classifications_dir / f"{stem}.classification.json"
        dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["classification_path"] = str(dest)
        results.append(payload)

        status_counts[result.classification_status] += 1
        type_counts[result.document_type] += 1
        source_counts[result.source_class] += 1
        tier_counts[result.authority_tier] += 1
        route_counts[result.route_path] += 1
        for section in result.book_sections:
            section_counts[str(section["id"])] += 1
        for topic in result.topics:
            topic_counts[topic] += 1
        if result.classification_confidence < 0.75:
            low_confidence.append(
                {
                    "title": record.get("title"),
                    "confidence": result.classification_confidence,
                    "status": result.classification_status,
                    "document_type": result.document_type,
                    "route_path": result.route_path,
                }
            )
        if not result.primary_section:
            missing_section.append(
                {
                    "title": record.get("title"),
                    "document_type": result.document_type,
                    "source_class": result.source_class,
                }
            )

    audit = {
        "schema_version": "1.0",
        "documents": len(results),
        "status_counts": dict(status_counts),
        "document_type_counts": dict(type_counts),
        "source_class_counts": dict(source_counts),
        "authority_tier_counts": dict(tier_counts),
        "route_counts": dict(route_counts),
        "section_coverage": dict(sorted(section_counts.items())),
        "topic_counts": dict(topic_counts.most_common()),
        "low_confidence_count": len(low_confidence),
        "missing_section_count": len(missing_section),
        "low_confidence": low_confidence,
        "missing_section": missing_section,
    }
    audit_path = _audit_dir(root) / "classification_audit.json"
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")

    index_path = root / "classification_index.jsonl"
    with index_path.open("w", encoding="utf-8") as handle:
        for row in results:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"Classified: {len(results)}")
    print(f"Classification sidecars: {classifications_dir}")
    print(f"Audit: {audit_path}")
    print(f"Index: {index_path}")
    print("Statuses:", dict(status_counts))
    print("Missing section:", len(missing_section), " Low confidence:", len(low_confidence))
    return audit


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Classify PaperCrawler catalog and write audit sidecars.")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="PaperCrawler staging directory. Defaults to TUNNEL_PAPERS_DIR / ./tunel_makaleleri.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    classify_catalog(args.output_dir)


if __name__ == "__main__":
    main()
