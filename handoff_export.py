#!/usr/bin/env python3
"""Create a self-contained TunnelBookAI source handoff package.

The exporter never mutates canonical PaperCrawler sources. It validates the
classification gate, source existence and SHA256, then hardlinks (or copies)
accepted sources into deterministic source-type folders with metadata and
classification sidecars. Discovery provenance and optional raw web snapshots are
preserved for TunnelBookAI auditability.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

import tunnel_harvest as harvest
import corpus_policy
import decision_router
import relevance_engine as relevance

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT / "config"


def _policy() -> dict[str, Any]:
    with (CONFIG_DIR / "classification_policy.yaml").open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    return payload.get("handoff") or {}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_document_id(row: dict[str, Any], source_path: Path | None = None) -> str:
    sha = str(row.get("source_sha256") or "").strip().lower()
    if not sha and source_path is not None:
        sha = _sha256(source_path)
    if not sha:
        identity = "|".join(str(row.get(key) or "") for key in ("doi", "source_url", "landing_url", "title"))
        sha = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return "PC_" + sha[:16].upper()


def _safe_route(route_path: str) -> Path:
    route = Path(str(route_path or "90_STAGING/NEEDS_CLASSIFICATION"))
    if route.is_absolute() or ".." in route.parts:
        raise ValueError(f"Unsafe route path: {route_path}")
    return route


def _link_or_copy(src: Path, dest: Path) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    try:
        os.link(src, dest)
        return "hardlink"
    except OSError:
        shutil.copy2(src, dest)
        return "copy"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        item = json.loads(line)
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


QUEUE_COLUMNS = [
    "document_id", "title", "source_path", "source_sha256", "discovery_source",
    "source_url", "landing_url", "pdf_url", "document_type", "source_class",
    "authority_tier", "relevance_score", "relevance_status", "primary_section",
    "book_sections", "classification_status", "acquisition_status", "decision",
    "reason", "recommended_action",
]


def _write_csv(path: Path, rows: list[dict[str, Any]], sort_key: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=QUEUE_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in sorted(rows, key=sort_key):
            item = dict(row)
            item["book_sections"] = ";".join(
                str(section.get("id") if isinstance(section, dict) else section)
                for section in (row.get("book_sections") or [])
            )
            writer.writerow(item)


def export_handoff(
    output_dir: str | Path | None = None,
    *,
    destination: str | Path | None = None,
) -> dict[str, Any]:
    if output_dir is not None:
        harvest.set_output_dir(output_dir)
    source_root = harvest.OUTPUT_DIR
    policy = _policy()
    require_sha = bool(policy.get("require_sha256", True))
    package_name = str(policy.get("package_name") or "TunnelBookAI_Source_Pack")

    package_root = Path(destination).resolve() if destination else (source_root / "exports" / package_name)
    originals_root = package_root / "01_originals"
    registry_root = package_root / "00_registry"
    audit_root = package_root / "99_audit"
    registry_root.mkdir(parents=True, exist_ok=True)
    audit_root.mkdir(parents=True, exist_ok=True)

    rows = _read_jsonl(source_root / "classification_index.jsonl")
    manifest: list[dict[str, Any]] = []
    queues: dict[str, list[dict[str, Any]]] = {
        decision_router.RETRY_ACQUISITION: [], decision_router.RECLASSIFY: [],
        decision_router.AUTO_REJECT: [], decision_router.MANUAL_REVIEW: [],
    }
    status_counts: Counter[str] = Counter()
    route_counts: Counter[str] = Counter()
    copy_modes: Counter[str] = Counter()
    seen_ids: set[str] = set()

    for row in rows:
        status = str(row.get("classification_status") or "")
        row = dict(row)
        row["evidence_level"] = str(row.get("evidence_level") or corpus_policy.evidence_level(row))
        row["source_tier"] = str(row.get("source_tier") or corpus_policy.source_tier(row))
        row["normalized_document_type"] = str(row.get("normalized_document_type") or corpus_policy.normalized_document_type(row))
        recheck = relevance.evaluate(row, text_override=str(row.get("abstract") or ""))
        row = {**row, **recheck}
        source_value = row.get("source_path")
        source_path = Path(str(source_value)).expanduser() if source_value else None
        source_exists = bool(source_path and source_path.is_file())
        actual_sha = _sha256(source_path) if source_exists and source_path is not None else ""
        expected_sha = str(row.get("source_sha256") or "").strip().lower()
        sha_valid = not (expected_sha and actual_sha and expected_sha != actual_sha)
        if require_sha and actual_sha and not expected_sha:
            row["source_sha256"] = actual_sha
        routed = decision_router.route(row, source_exists=source_exists, sha_valid=sha_valid)
        if routed["decision"] != decision_router.AUTO_HANDOFF:
            queues[routed["decision"]].append(decision_router.queue_entry(row, routed))
            continue

        document_id = _stable_document_id(row, source_path)
        if document_id in seen_ids:
            duplicate = decision_router._decision(decision_router.AUTO_REJECT, "duplicate_document_id", "retain one canonical source")
            queues[decision_router.AUTO_REJECT].append(decision_router.queue_entry(row, duplicate))
            continue
        seen_ids.add(document_id)

        # AUTO_HANDOFF normally has a physical source.  The only exception is an
        # explicitly flagged official metadata record, which carries no checksum.
        metadata_only_exception = not source_exists

        route = _safe_route(str(row.get("route_path") or ""))
        doc_dir = originals_root / route / document_id
        doc_dir.mkdir(parents=True, exist_ok=True)
        source_dest = None
        copy_mode = "metadata_only_official_exception"
        if source_path is not None and source_exists:
            suffix = source_path.suffix.lower() or ".bin"
            source_dest = doc_dir / ("source" + suffix)
            copy_mode = _link_or_copy(source_path, source_dest)
            copy_modes[copy_mode] += 1

        extra_assets: list[dict[str, Any]] = []
        raw_html_value = row.get("raw_html_path")
        if raw_html_value:
            raw_html = Path(str(raw_html_value)).expanduser()
            if raw_html.exists() and raw_html.is_file():
                expected_raw_sha = str(row.get("raw_html_sha256") or "").strip().lower()
                actual_raw_sha = _sha256(raw_html)
                if not expected_raw_sha or expected_raw_sha == actual_raw_sha:
                    raw_dest = doc_dir / "source_raw.html"
                    raw_mode = _link_or_copy(raw_html, raw_dest)
                    copy_modes[raw_mode] += 1
                    extra_assets.append({
                        "kind": "raw_html_snapshot",
                        "path": str(raw_dest.relative_to(package_root)),
                        "sha256": actual_raw_sha,
                        "copy_mode": raw_mode,
                    })

        classification_payload = dict(row)
        classification_payload.update({
            "document_id": document_id,
            "paper_crawler_status": "READY_FOR_HANDOFF",
            "tunnelbookai_status": "NOT_INGESTED",
            "handoff_source_path": str(source_dest.relative_to(package_root)) if source_dest else None,
            "source_sha256": actual_sha or None,
            "extra_assets": extra_assets,
            "final_evidence_status": "NOT_EVALUATED",
        })
        (doc_dir / "classification.json").write_text(
            json.dumps(classification_payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        metadata = {
            "schema_version": "1.1",
            "document_id": document_id,
            "canonical_id": row.get("canonical_id") or "CAN_" + document_id.removeprefix("PC_"),
            "title": row.get("title"),
            "authors": row.get("authors") or [],
            "year": row.get("year"),
            "publisher": row.get("publisher"),
            "doi": row.get("doi"),
            "document_type": row.get("document_type"),
            "normalized_document_type": row.get("normalized_document_type"),
            "source_tier": row.get("source_tier"),
            "evidence_level": row.get("evidence_level"),
            "source_class": row.get("source_class"),
            "authority_tier": row.get("authority_tier"),
            "evidence_priority": row.get("evidence_priority"),
            "primary_section": row.get("primary_section"),
            "book_sections": row.get("book_sections") or [],
            "topics": row.get("topics") or [],
            "classification_confidence": row.get("classification_confidence"),
            "classification_status": status,
            "route_path": str(route),
            "source_sha256": actual_sha or None,
            "source_filename": source_path.name if source_path else None,
            "source_url": row.get("source_url"),
            "landing_url": row.get("landing_url"),
            "pdf_url": row.get("pdf_url"),
            "discovery_source": row.get("discovery_source"),
            "discovery_query": row.get("discovery_query"),
            "acquisition_status": row.get("acquisition_status"),
            "metadata_only": bool(row.get("metadata_only", False)),
            "extra_assets": extra_assets,
            "paper_crawler_status": "READY_FOR_HANDOFF",
            "provisional_authority_tier": row.get("authority_tier"),
            "final_evidence_status": "NOT_EVALUATED",
            "tunnelbookai_status": "NOT_INGESTED",
            "metadata_only_official_exception": metadata_only_exception,
        }
        if "piarc" in str(row.get("source_url") or row.get("landing_url") or "").casefold():
            metadata["parent_document"] = row.get("parent_document") or "PIARC Road Tunnels Manual"
            metadata["section_url"] = row.get("source_url") or row.get("landing_url")
            metadata["section_title"] = row.get("title")
        (doc_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

        manifest_row = {
            **metadata,
            "source_path": str(source_dest.relative_to(package_root)) if source_dest else None,
            "classification_path": str((doc_dir / "classification.json").relative_to(package_root)),
            "metadata_path": str((doc_dir / "metadata.json").relative_to(package_root)),
            "copy_mode": copy_mode,
        }
        manifest.append(manifest_row)
        status_counts[status] += 1
        route_counts[str(route)] += 1

    manifest_path = registry_root / "manifest.jsonl"
    with manifest_path.open("w", encoding="utf-8") as handle:
        for row in manifest:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    handoff_manifest = []
    for row in manifest:
        handoff_manifest.append({
            "document_id": row.get("document_id"),
            "canonical_id": row.get("canonical_id") or "CAN_" + str(row.get("source_sha256") or "")[:20].upper(),
            "title": row.get("title"), "authors": row.get("authors") or [], "year": row.get("year"),
            "doi": row.get("doi"), "source_url": row.get("source_url"),
            "resolved_url": row.get("pdf_url") or row.get("landing_url") or row.get("source_url"),
            "local_path": row.get("source_path"), "sha256": row.get("source_sha256"),
            "source_name": row.get("discovery_source"), "source_tier": row.get("source_tier"),
            "document_type": row.get("normalized_document_type"), "primary_section": row.get("primary_section"),
            "secondary_sections": [s.get("id") for s in row.get("book_sections") or [] if s.get("id") != row.get("primary_section")],
            "classification_status": row.get("classification_status"),
            "classification_confidence": row.get("classification_confidence"),
            "evidence_level": row.get("evidence_level"), "acquisition_status": row.get("acquisition_status"),
            "handoff_status": "READY_FOR_HANDOFF",
            "paper_crawler_status": "READY_FOR_HANDOFF",
            "provisional_authority_tier": row.get("provisional_authority_tier") or row.get("authority_tier"),
            "final_evidence_status": "NOT_EVALUATED", "tunnelbookai_status": "NOT_INGESTED",
            "metadata_only_official_exception": bool(row.get("metadata_only_official_exception")),
            "provenance": {key: row.get(key) for key in ("source_url", "landing_url", "pdf_url", "discovery_source", "discovery_query", "doi", "publisher") if row.get(key)},
        })
    _write_jsonl(registry_root / "handoff_manifest.jsonl", handoff_manifest)

    checksums_path = registry_root / "checksums.sha256"
    with checksums_path.open("w", encoding="utf-8") as handle:
        for row in manifest:
            if row.get("source_sha256") and row.get("source_path"):
                handle.write(f"{row['source_sha256']}  {row['source_path']}\n")
            for asset in row.get("extra_assets") or []:
                if asset.get("sha256") and asset.get("path"):
                    handle.write(f"{asset['sha256']}  {asset['path']}\n")

    handoff_report = {
        "schema_version": "1.1",
        "package": package_name,
        "source_root": str(source_root),
        "package_root": str(package_root),
        "input_classifications": len(rows),
        "ready_for_handoff": len(manifest),
        "rejected": len(queues[decision_router.AUTO_REJECT]),
        "decision_counts": {
            decision_router.AUTO_HANDOFF: len(manifest),
            **{decision: len(items) for decision, items in queues.items()},
        },
        "status_counts": dict(status_counts),
        "route_counts": dict(route_counts),
        "copy_modes": dict(copy_modes),
        "rejections": queues[decision_router.AUTO_REJECT],
        "gate_meaning": "READY_FOR_HANDOFF means safe and sufficiently classified for TunnelBookAI processing; it is not final evidence approval.",
    }
    (audit_root / "handoff_audit.json").write_text(
        json.dumps(handoff_report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    review_queue = queues[decision_router.MANUAL_REVIEW]
    retry_queue = queues[decision_router.RETRY_ACQUISITION]
    reclassify_queue = queues[decision_router.RECLASSIFY]
    rejected = queues[decision_router.AUTO_REJECT]
    _write_jsonl(audit_root / "review_queue.jsonl", review_queue)
    _write_jsonl(audit_root / "retry_acquisition.jsonl", retry_queue)
    _write_jsonl(audit_root / "reclassify_queue.jsonl", reclassify_queue)
    _write_jsonl(audit_root / "rejected_manifest.jsonl", rejected)
    decision_summary = handoff_report["decision_counts"]
    (audit_root / "decision_summary.json").write_text(
        json.dumps(decision_summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    csv_root = source_root / "audit"
    _write_csv(csv_root / "review_queue.csv", review_queue, lambda row: (
        str(row.get("reason") or ""), str(row.get("primary_section") or ""),
        str(row.get("relevance_status") or ""), str(row.get("title") or "").casefold(),
    ))
    _write_csv(csv_root / "retry_acquisition.csv", retry_queue, lambda row: (
        str(row.get("discovery_source") or ""), str(row.get("acquisition_status") or ""),
        str(row.get("title") or "").casefold(),
    ))
    _write_csv(csv_root / "reclassify_queue.csv", reclassify_queue, lambda row: (
        str(row.get("reason") or ""), str(row.get("primary_section") or ""),
        str(row.get("title") or "").casefold(),
    ))
    _write_csv(csv_root / "rejected_manifest.csv", rejected, lambda row: (
        str(row.get("reason") or ""), str(row.get("title") or "").casefold(),
    ))
    (registry_root / "handoff_contract.json").write_text(
        json.dumps({
            "schema_version": "1.1",
            "producer": "paper-crawler-agent",
            "consumer": "TunnelBookAI",
            "manifest": "00_registry/manifest.jsonl",
            "handoff_manifest": "00_registry/handoff_manifest.jsonl",
            "checksums": "00_registry/checksums.sha256",
            "source_tree": "01_originals",
            "audit": "99_audit/handoff_audit.json",
            "review_queue": "99_audit/review_queue.jsonl",
            "retry_acquisition": "99_audit/retry_acquisition.jsonl",
            "reclassify_queue": "99_audit/reclassify_queue.jsonl",
            "rejected_manifest": "99_audit/rejected_manifest.jsonl",
            "provenance_fields": ["source_url", "landing_url", "pdf_url", "discovery_source", "discovery_query", "doi", "publisher"],
            "consumer_rule": "TunnelBookAI must revalidate SHA256 and perform full-text conversion, quality audit, final section classification and evidence gating before corpus ingest.",
        }, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"READY_FOR_HANDOFF: {len(manifest)}")
    print(f"Decision routes: {handoff_report['decision_counts']}")
    print(f"Package: {package_root}")
    return handoff_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export classified PaperCrawler sources for TunnelBookAI.")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--destination", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    export_handoff(args.output_dir, destination=args.destination)


if __name__ == "__main__":
    main()
