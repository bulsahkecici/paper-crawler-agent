#!/usr/bin/env python3
"""Create a self-contained TunnelBookAI source handoff package.

The exporter never mutates canonical PaperCrawler sources. It validates the
classification gate, source existence and SHA256, then hardlinks (or copies)
accepted sources into deterministic source-type folders with metadata and
classification sidecars.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

import tunnel_harvest as harvest

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


def _stable_document_id(row: dict[str, Any], source_path: Path) -> str:
    sha = str(row.get("source_sha256") or "").strip().lower()
    if not sha:
        sha = _sha256(source_path)
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


def export_handoff(
    output_dir: str | Path | None = None,
    *,
    destination: str | Path | None = None,
) -> dict[str, Any]:
    if output_dir is not None:
        harvest.set_output_dir(output_dir)
    source_root = harvest.OUTPUT_DIR
    policy = _policy()
    allowed = set(policy.get("allowed_classification_statuses") or [])
    require_section = bool(policy.get("require_primary_section", True))
    require_source = bool(policy.get("require_existing_source", True))
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
    rejected: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    route_counts: Counter[str] = Counter()
    copy_modes: Counter[str] = Counter()
    seen_ids: set[str] = set()

    for row in rows:
        status = str(row.get("classification_status") or "")
        reason = None
        if status not in allowed:
            reason = "classification_status_not_allowed"
        elif require_section and not row.get("primary_section"):
            reason = "primary_section_missing"

        source_value = row.get("source_path")
        source_path = Path(str(source_value)).expanduser() if source_value else None
        if reason is None and require_source and (source_path is None or not source_path.exists()):
            reason = "source_missing"
        if reason:
            rejected.append({"title": row.get("title"), "status": status, "reason": reason})
            continue
        assert source_path is not None

        actual_sha = _sha256(source_path)
        expected_sha = str(row.get("source_sha256") or "").strip().lower()
        if expected_sha and expected_sha != actual_sha:
            rejected.append({"title": row.get("title"), "status": status, "reason": "sha256_mismatch"})
            continue
        if require_sha and not expected_sha:
            row["source_sha256"] = actual_sha

        document_id = _stable_document_id(row, source_path)
        if document_id in seen_ids:
            rejected.append({"title": row.get("title"), "status": status, "reason": "duplicate_document_id"})
            continue
        seen_ids.add(document_id)

        route = _safe_route(str(row.get("route_path") or ""))
        doc_dir = originals_root / route / document_id
        suffix = source_path.suffix.lower() or ".bin"
        source_dest = doc_dir / ("source" + suffix)
        copy_mode = _link_or_copy(source_path, source_dest)
        copy_modes[copy_mode] += 1

        classification_payload = dict(row)
        classification_payload.update({
            "document_id": document_id,
            "paper_crawler_status": "READY_FOR_HANDOFF",
            "tunnelbookai_status": "NOT_INGESTED",
            "handoff_source_path": str(source_dest.relative_to(package_root)),
            "source_sha256": actual_sha,
        })
        (doc_dir / "classification.json").write_text(
            json.dumps(classification_payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        metadata = {
            "schema_version": "1.0",
            "document_id": document_id,
            "title": row.get("title"),
            "document_type": row.get("document_type"),
            "source_class": row.get("source_class"),
            "authority_tier": row.get("authority_tier"),
            "evidence_priority": row.get("evidence_priority"),
            "primary_section": row.get("primary_section"),
            "book_sections": row.get("book_sections") or [],
            "topics": row.get("topics") or [],
            "classification_confidence": row.get("classification_confidence"),
            "classification_status": status,
            "route_path": str(route),
            "source_sha256": actual_sha,
            "source_filename": source_path.name,
            "paper_crawler_status": "READY_FOR_HANDOFF",
            "tunnelbookai_status": "NOT_INGESTED",
        }
        (doc_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

        manifest_row = {
            **metadata,
            "source_path": str(source_dest.relative_to(package_root)),
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

    checksums_path = registry_root / "checksums.sha256"
    with checksums_path.open("w", encoding="utf-8") as handle:
        for row in manifest:
            handle.write(f"{row['source_sha256']}  {row['source_path']}\n")

    handoff_report = {
        "schema_version": "1.0",
        "package": package_name,
        "source_root": str(source_root),
        "package_root": str(package_root),
        "input_classifications": len(rows),
        "ready_for_handoff": len(manifest),
        "rejected": len(rejected),
        "status_counts": dict(status_counts),
        "route_counts": dict(route_counts),
        "copy_modes": dict(copy_modes),
        "rejections": rejected,
        "gate_meaning": "READY_FOR_HANDOFF means safe and sufficiently classified for TunnelBookAI processing; it is not final evidence approval.",
    }
    (audit_root / "handoff_audit.json").write_text(
        json.dumps(handoff_report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (registry_root / "handoff_contract.json").write_text(
        json.dumps({
            "schema_version": "1.0",
            "producer": "paper-crawler-agent",
            "consumer": "TunnelBookAI",
            "manifest": "00_registry/manifest.jsonl",
            "checksums": "00_registry/checksums.sha256",
            "source_tree": "01_originals",
            "audit": "99_audit/handoff_audit.json",
            "consumer_rule": "TunnelBookAI must revalidate SHA256 and perform full-text conversion, quality audit, final section classification and evidence gating before corpus ingest.",
        }, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"READY_FOR_HANDOFF: {len(manifest)}")
    print(f"Rejected by handoff gate: {len(rejected)}")
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
