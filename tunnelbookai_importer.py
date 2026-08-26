#!/usr/bin/env python3
"""Validate a PaperCrawler handoff package and build a TunnelBookAI ingest plan.

This importer is intentionally corpus-safe: it never writes into a canonical corpus by
default. It validates the handoff contract/checksums and produces an ingest_plan.jsonl
that TunnelBookAI can feed to its Docling/conversion pipeline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        value = json.loads(line)
        if isinstance(value, dict):
            rows.append(value)
    return rows


def validate_package(package_root: str | Path, output_dir: str | Path | None = None) -> dict[str, Any]:
    root = Path(package_root).expanduser().resolve()
    registry = root / "00_registry"
    manifest_path = registry / "manifest.jsonl"
    checksums_path = registry / "checksums.sha256"
    contract_path = registry / "handoff_contract.json"
    if not manifest_path.exists() or not checksums_path.exists() or not contract_path.exists():
        raise FileNotFoundError("Invalid handoff package: manifest/checksums/contract required")

    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    manifest = _read_jsonl(manifest_path)
    checksum_map: dict[str, str] = {}
    for line in checksums_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        sha, rel = line.split(None, 1)
        checksum_map[rel.strip().lstrip("* ")] = sha.lower()

    errors: list[dict[str, str]] = []
    ingest_rows: list[dict[str, Any]] = []
    seen_sha: dict[str, str] = {}
    for row in manifest:
        rel = str(row.get("source_path") or "")
        if not rel:
            errors.append({"document_id": str(row.get("document_id") or ""), "error": "missing_source_path"})
            continue
        source = (root / rel).resolve()
        try:
            source.relative_to(root)
        except ValueError:
            errors.append({"document_id": str(row.get("document_id") or ""), "error": "path_escape"})
            continue
        if not source.exists():
            errors.append({"document_id": str(row.get("document_id") or ""), "error": "source_missing"})
            continue
        actual = _sha256(source)
        expected = str(row.get("source_sha256") or checksum_map.get(rel) or "").lower()
        if not expected or actual != expected:
            errors.append({"document_id": str(row.get("document_id") or ""), "error": "sha256_mismatch"})
            continue
        duplicate_of = seen_sha.get(actual)
        if duplicate_of:
            status = "DUPLICATE_IN_HANDOFF"
        else:
            status = "READY_FOR_DOCLING"
            seen_sha[actual] = str(row.get("document_id") or rel)
        ingest_rows.append({
            "document_id": row.get("document_id"),
            "source_path": str(source),
            "source_sha256": actual,
            "document_type": row.get("document_type"),
            "source_class": row.get("source_class"),
            "authority_tier": row.get("authority_tier"),
            "primary_section": row.get("primary_section"),
            "book_sections": row.get("book_sections") or [],
            "topics": row.get("topics") or [],
            "paper_crawler_status": row.get("paper_crawler_status"),
            "tunnelbookai_status": status,
            "duplicate_of": duplicate_of,
            "next_stage": "DOCLING_FULLTEXT_AND_QUALITY_AUDIT" if status == "READY_FOR_DOCLING" else "SKIP_DUPLICATE",
            "evidence_approved": False,
        })

    out = Path(output_dir).expanduser().resolve() if output_dir else root / "TunnelBookAI_Import"
    out.mkdir(parents=True, exist_ok=True)
    plan = out / "ingest_plan.jsonl"
    with plan.open("w", encoding="utf-8") as handle:
        for row in ingest_rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    report = {
        "schema_version": "1.0",
        "contract": contract,
        "manifest_documents": len(manifest),
        "ready_for_docling": sum(1 for row in ingest_rows if row["tunnelbookai_status"] == "READY_FOR_DOCLING"),
        "duplicates_in_handoff": sum(1 for row in ingest_rows if row["tunnelbookai_status"] == "DUPLICATE_IN_HANDOFF"),
        "errors": errors,
        "ingest_plan": str(plan),
        "canonical_corpus_modified": False,
        "semantic_boundary": "Importer validates and plans only. TunnelBookAI must run Docling, quality audit, final classification/evidence gate before canonical ingest.",
    }
    (out / "import_audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate PaperCrawler handoff and create TunnelBookAI ingest plan")
    parser.add_argument("package_root")
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()
    print(json.dumps(validate_package(args.package_root, args.output_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
