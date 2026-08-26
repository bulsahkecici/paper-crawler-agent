#!/usr/bin/env python3
"""Classify an existing PaperCrawler catalog without moving canonical sources.

Rules always run. If a loopback embedding model is available, section scores are
fused with local semantic similarity. Ambiguous cases may then be reviewed by a
loopback-only Qwen model. No cloud fallback is allowed.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

import classification_engine as classifier
import hybrid_classifier as hybrid
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
    source = record.get("local_pdf_path") or record.get("pdf_path") or record.get("path")
    if source:
        return Path(str(source)).stem
    doi = str(record.get("doi") or "").strip()
    if doi:
        return classifier._norm(doi).replace("/", "_").replace(".", "_")[:100]
    return harvest.sanitize_filename(str(record.get("title") or f"document_{index}"), max_length=100)


def classify_catalog(
    output_dir: str | Path | None = None,
    *,
    use_local_ai: bool = True,
) -> dict[str, Any]:
    if output_dir is not None:
        harvest.set_output_dir(output_dir)
    root = harvest.OUTPUT_DIR
    catalog = harvest.load_catalog()
    papers = catalog.get("papers") or catalog.get("downloaded") or []
    if not isinstance(papers, list):
        raise ValueError("catalog papers must be a list")

    emb_client = emb_model = llm_client = llm_model = None
    if use_local_ai:
        emb_client, emb_model, llm_client, llm_model = hybrid.detect_local_clients()
        if emb_model:
            print(f"Local embedding classifier: {emb_model} @ {emb_client.base_url}")
        else:
            print("Local embedding model not found; using deterministic rules only.")
        if llm_model:
            print(f"Local Qwen reviewer: {llm_model} @ {llm_client.base_url}")
        else:
            print("Local Qwen reviewer not found; ambiguous cases remain for review.")

    profile_vectors: dict[str, list[float]] = {}
    classifications_dir = _classification_dir(root)
    counters = {
        "status": Counter(), "type": Counter(), "source": Counter(), "tier": Counter(),
        "route": Counter(), "section": Counter(), "topic": Counter(),
    }
    low_confidence: list[dict[str, Any]] = []
    missing_section: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []

    for index, record in enumerate(papers, 1):
        if not isinstance(record, dict):
            continue
        result = hybrid.classify_hybrid(
            record,
            embedding_client=emb_client,
            embedding_model=emb_model,
            llm_client=llm_client,
            llm_model=llm_model,
            profile_vectors=profile_vectors,
        ) if use_local_ai else classifier.classify_record(record).as_dict()

        payload = {
            "schema_version": "2.0",
            "document_key": record.get("doi") or record.get("source_sha256") or _stem_for_record(record, index),
            "title": record.get("title"),
            "source_sha256": record.get("source_sha256"),
            "source_path": record.get("local_pdf_path") or record.get("pdf_path") or record.get("path"),
            **result,
        }
        stem = _stem_for_record(record, index)
        dest = classifications_dir / f"{stem}.classification.json"
        dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["classification_path"] = str(dest)
        results.append(payload)

        counters["status"][str(result.get("classification_status"))] += 1
        counters["type"][str(result.get("document_type"))] += 1
        counters["source"][str(result.get("source_class"))] += 1
        counters["tier"][str(result.get("authority_tier"))] += 1
        counters["route"][str(result.get("route_path"))] += 1
        for section in result.get("book_sections") or []:
            counters["section"][str(section.get("id"))] += 1
        for topic in result.get("topics") or []:
            counters["topic"][str(topic)] += 1
        confidence = float(result.get("classification_confidence") or 0.0)
        if confidence < 0.75:
            low_confidence.append({
                "title": record.get("title"), "confidence": confidence,
                "status": result.get("classification_status"),
                "document_type": result.get("document_type"), "route_path": result.get("route_path"),
            })
        if not result.get("primary_section"):
            missing_section.append({
                "title": record.get("title"), "document_type": result.get("document_type"),
                "source_class": result.get("source_class"),
            })

    audit = {
        "schema_version": "2.0",
        "documents": len(results),
        "local_ai": {
            "embedding_model": emb_model,
            "llm_model": llm_model,
            "loopback_only": True,
        },
        "status_counts": dict(counters["status"]),
        "document_type_counts": dict(counters["type"]),
        "source_class_counts": dict(counters["source"]),
        "authority_tier_counts": dict(counters["tier"]),
        "route_counts": dict(counters["route"]),
        "section_coverage": dict(sorted(counters["section"].items())),
        "topic_counts": dict(counters["topic"].most_common()),
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
    print("Statuses:", dict(counters["status"]))
    return audit


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Classify PaperCrawler catalog with rules and optional local AI.")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--rules-only", action="store_true", help="Disable local embedding and Qwen review.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    classify_catalog(args.output_dir, use_local_ai=not args.rules_only)


if __name__ == "__main__":
    main()
