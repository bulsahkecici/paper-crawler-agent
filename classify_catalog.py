#!/usr/bin/env python3
"""Classify harvested papers plus free-discovery sources without moving originals.

Rules always run. If a loopback embedding model is available, section scores are
fused with local semantic similarity. Ambiguous cases may then be reviewed by a
loopback-only Qwen model. No cloud fallback is allowed.

Inputs:
- catalog.json (classic academic harvest)
- discovery_catalog.jsonl (institutional/OAI/sitemap/web discovery)
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


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _record_key(record: dict[str, Any], index: int) -> str:
    doi = harvest.normalize_doi(record.get("doi"))
    if doi:
        return "doi:" + doi.lower()
    sha = str(record.get("source_sha256") or "").strip().lower()
    if sha:
        return "sha:" + sha
    discovery_key = str(record.get("discovery_key") or "").strip()
    if discovery_key:
        return discovery_key
    for key in ("source_path", "local_pdf_path", "pdf_path", "path", "source_url", "landing_url"):
        value = str(record.get(key) or "").strip()
        if value:
            return key + ":" + value
    return f"index:{index}:{record.get('title') or ''}"


def _merge_records(classic: list[Any], discovered: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    sequence = [*classic, *discovered]
    for index, record in enumerate(sequence, 1):
        if not isinstance(record, dict):
            continue
        key = _record_key(record, index)
        previous = merged.get(key)
        if previous is None:
            merged[key] = dict(record)
            continue
        current_rank = (
            bool(record.get("source_path") or record.get("local_pdf_path") or record.get("path")),
            len(str(record.get("abstract") or record.get("text_excerpt") or "")),
            bool(record.get("discovery_source")),
        )
        previous_rank = (
            bool(previous.get("source_path") or previous.get("local_pdf_path") or previous.get("path")),
            len(str(previous.get("abstract") or previous.get("text_excerpt") or "")),
            bool(previous.get("discovery_source")),
        )
        if current_rank > previous_rank:
            merged[key] = {**previous, **record}
        else:
            for field in ("discovery_source", "discovery_query", "source_url", "raw_html_path", "acquisition_status"):
                if not previous.get(field) and record.get(field):
                    previous[field] = record[field]
    return list(merged.values())


def _stem_for_record(record: dict[str, Any], index: int) -> str:
    source = record.get("source_path") or record.get("local_pdf_path") or record.get("pdf_path") or record.get("path")
    if source:
        return Path(str(source)).stem
    doi = str(record.get("doi") or "").strip()
    if doi:
        return classifier._norm(doi).replace("/", "_").replace(".", "_")[:100]
    discovery_key = str(record.get("discovery_key") or "").strip()
    if discovery_key:
        return harvest.sanitize_filename(discovery_key, max_length=100)
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
    classic = catalog.get("papers") or catalog.get("downloaded") or []
    if not isinstance(classic, list):
        raise ValueError("catalog papers must be a list")
    discovered = _read_jsonl(root / "discovery_catalog.jsonl")
    papers = _merge_records(classic, discovered)

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
        "route": Counter(), "section": Counter(), "usable_section": Counter(), "topic": Counter(), "input": Counter(),
    }
    low_confidence: list[dict[str, Any]] = []
    missing_section: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []

    for index, record in enumerate(papers, 1):
        if not isinstance(record, dict):
            continue
        if not record.get("abstract") and record.get("text_excerpt"):
            record = {**record, "abstract": str(record.get("text_excerpt") or "")[:6000]}
        result = hybrid.classify_hybrid(
            record,
            embedding_client=emb_client,
            embedding_model=emb_model,
            llm_client=llm_client,
            llm_model=llm_model,
            profile_vectors=profile_vectors,
        ) if use_local_ai else classifier.classify_record(record).as_dict()

        source_path = record.get("source_path") or record.get("local_pdf_path") or record.get("pdf_path") or record.get("path")
        source_exists = bool(source_path and Path(str(source_path)).expanduser().exists())
        acquisition_ok = str(record.get("acquisition_status") or "").upper() not in {"FAILED", "METADATA_ONLY", "NOT_REQUESTED", "NO_URL"}
        handoff_candidate = source_exists and (acquisition_ok or not record.get("discovery_source"))
        payload = {
            "schema_version": "2.2",
            "document_key": record.get("doi") or record.get("source_sha256") or record.get("discovery_key") or _stem_for_record(record, index),
            "title": record.get("title"),
            "authors": record.get("authors") or [],
            "year": record.get("year"),
            "publisher": record.get("publisher") or record.get("venue"),
            "doi": harvest.normalize_doi(record.get("doi")),
            "source_sha256": record.get("source_sha256"),
            "source_path": source_path,
            "source_url": record.get("source_url"),
            "landing_url": record.get("landing_url"),
            "pdf_url": record.get("pdf_url"),
            "discovery_source": record.get("discovery_source") or record.get("source"),
            "discovery_query": record.get("discovery_query") or record.get("query"),
            "acquisition_status": record.get("acquisition_status"),
            "raw_html_path": record.get("raw_html_path"),
            "raw_html_sha256": record.get("raw_html_sha256"),
            "metadata_only": bool(record.get("metadata_only", False)),
            "handoff_candidate": handoff_candidate,
            **result,
        }
        stem = _stem_for_record(record, index)
        dest = classifications_dir / f"{stem}.classification.json"
        dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["classification_path"] = str(dest)
        results.append(payload)

        counters["input"]["discovery"] += int(bool(record.get("discovery_source")))
        counters["input"]["classic"] += int(not bool(record.get("discovery_source")))
        counters["status"][str(result.get("classification_status"))] += 1
        counters["type"][str(result.get("document_type"))] += 1
        counters["source"][str(result.get("source_class"))] += 1
        counters["tier"][str(result.get("authority_tier"))] += 1
        counters["route"][str(result.get("route_path"))] += 1
        for section in result.get("book_sections") or []:
            sid = str(section.get("id"))
            counters["section"][sid] += 1
            if handoff_candidate:
                counters["usable_section"][sid] += 1
        for topic in result.get("topics") or []:
            counters["topic"][str(topic)] += 1
        confidence = float(result.get("classification_confidence") or 0.0)
        if confidence < 0.75:
            low_confidence.append({
                "title": record.get("title"), "confidence": confidence,
                "status": result.get("classification_status"),
                "document_type": result.get("document_type"), "route_path": result.get("route_path"),
                "discovery_source": record.get("discovery_source"),
            })
        if not result.get("primary_section"):
            missing_section.append({
                "title": record.get("title"), "document_type": result.get("document_type"),
                "source_class": result.get("source_class"), "discovery_source": record.get("discovery_source"),
            })

    audit = {
        "schema_version": "2.2",
        "documents": len(results),
        "input_counts": dict(counters["input"]),
        "classic_catalog_rows": len(classic),
        "discovery_catalog_rows": len(discovered),
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
        "handoff_candidate_section_coverage": dict(sorted(counters["usable_section"].items())),
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

    print(f"Classified: {len(results)} (classic={len(classic)}, discovery={len(discovered)})")
    print(f"Classification sidecars: {classifications_dir}")
    print(f"Audit: {audit_path}")
    print(f"Index: {index_path}")
    print("Statuses:", dict(counters["status"]))
    return audit


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Classify PaperCrawler harvest + free-discovery catalogs.")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--rules-only", action="store_true", help="Disable local embedding and Qwen review.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    classify_catalog(args.output_dir, use_local_ai=not args.rules_only)


if __name__ == "__main__":
    main()
