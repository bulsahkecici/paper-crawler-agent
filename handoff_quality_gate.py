#!/usr/bin/env python3
"""Fail-closed quality gate for the PaperCrawler -> TunnelBookAI handoff package.

This gate evaluates PaperCrawler's *handoff package* only. It never evaluates a
canonical TunnelBookAI corpus: that responsibility, and the name
``corpus_quality_gate``, belong to TunnelBookAI's ingest layer.

Authoritative output: ``<package>/99_audit/handoff_quality_gate.json``.
The producer audit directory receives an explicitly marked compatibility copy.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT / "config"

FORBIDDEN_CRAWLER_EVIDENCE_LEVELS = {"FULL_TEXT", "PDF_EXTRACT"}
REQUIRED_CONTRACT_KEYS = {
    "schema_version", "producer", "consumer",
    "producer_responsibilities", "consumer_responsibilities", "semantic_rules",
}


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _jsonl(path: Path) -> tuple[list[dict[str, Any]], bool]:
    if not path.exists():
        return [], False
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    return rows, False
                rows.append(value)
    except (OSError, ValueError):
        return rows, False
    return rows, True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_package_file(package: Path, relative: Any) -> Path | None:
    value = str(relative or "")
    candidate = Path(value)
    if not value or candidate.is_absolute() or ".." in candidate.parts:
        return None
    root = package.resolve()
    resolved = (package / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved


def evaluate_handoff(output_dir: str | Path, *, package_root: str | Path | None = None) -> dict[str, Any]:
    root = Path(output_dir)
    audit = root / "audit"
    state = _json(audit / "pipeline_state.json")
    classification = _json(audit / "classification_audit.json")
    package = Path(package_root).resolve() if package_root else root / "exports" / "TunnelBookAI_Source_Pack"
    manifest, manifest_ok = _jsonl(package / "00_registry" / "handoff_manifest.jsonl")
    contract = _json(package / "00_registry" / "handoff_contract.json")
    release_metadata = _json(package / "00_registry" / "release_metadata.json")
    review, _ = _jsonl(package / "99_audit" / "review_queue.jsonl")
    retry, _ = _jsonl(package / "99_audit" / "retry_acquisition.jsonl")
    metadata_refs, _ = _jsonl(package / "99_audit" / "metadata_references.jsonl")
    rejected, _ = _jsonl(package / "99_audit" / "rejected_manifest.jsonl")
    index, index_ok = _jsonl(root / "classification_index.jsonl")

    blocking: list[str] = []
    warnings: list[str] = []

    # The exporter runs this gate before its caller can mark the handoff stage
    # complete. A readable, fingerprinted manifest is the proof of this stage.
    required_stages = {"free_discovery", "pdf_enrichment", "initial_classification", "source_audit"}
    stages = state.get("stages") or {}
    if not state or any(stages.get(stage) != "COMPLETED" for stage in required_stages):
        blocking.append("pipeline_incomplete")
    if not index_ok:
        blocking.append("classification_index_unreadable")

    reconciliation = classification.get("reconciliation") or {}
    if not reconciliation.get("invariant_ok"):
        blocking.append("reconciliation_invariant_failed")

    if not manifest_ok:
        blocking.append("handoff_manifest_missing_or_invalid")
    manifest_path = package / "00_registry" / "handoff_manifest.jsonl"
    manifest_sha256 = _sha256(manifest_path) if manifest_path.is_file() else ""
    declared_fingerprints = [
        str(value) for value in (
            contract.get("manifest_sha256"), release_metadata.get("manifest_sha256")
        ) if value
    ]
    if declared_fingerprints and any(value != manifest_sha256 for value in declared_fingerprints):
        blocking.append("manifest_fingerprint_mismatch")

    canonical_ids = [str(row.get("canonical_id") or row.get("canonical_hint_id") or "") for row in manifest]
    if "" in canonical_ids or len(canonical_ids) != len(set(canonical_ids)):
        blocking.append("duplicate_or_missing_canonical_id")
    document_ids = [str(row.get("document_id") or "") for row in manifest]
    if "" in document_ids or len(document_ids) != len(set(document_ids)):
        blocking.append("duplicate_or_missing_document_id")

    shas = [str(row.get("sha256") or "") for row in manifest]
    if "" in shas or len(shas) != len(set(shas)):
        blocking.append("duplicate_or_missing_sha256")

    sha_failures = 0
    missing_provenance = 0
    metadata_reference_marked_ready = 0
    source_representation_missing = 0
    chapter_fields_in_handoff = 0
    invalid_fulltext_claim = 0
    presentation_asset_count = 0
    presentation_asset_sha_failures = 0
    presentation_asset_provenance_missing = 0
    normalized_sha_failures = 0
    unsafe_paths = 0
    needs_classification_marked_ready = 0
    for row in manifest:
        status = str(row.get("paper_crawler_status") or "READY_FOR_HANDOFF").upper()
        if status != "READY_FOR_HANDOFF" or row.get("metadata_only_official_exception"):
            metadata_reference_marked_ready += 1
        representation = row.get("source_representation")
        if not isinstance(representation, dict):
            representation = {}
            source_representation_missing += 1
        authoritative_rel = representation.get("original_or_raw") or row.get("local_path") or row.get("source_path")
        authoritative_sha = representation.get("original_or_raw_sha256") or row.get("sha256")
        local_path = _safe_package_file(package, authoritative_rel)
        if local_path is None:
            unsafe_paths += 1
        if local_path is None or not local_path.is_file():
            blocking.append("handoff_file_missing")
        else:
            actual = _sha256(local_path)
            if not authoritative_sha or actual != str(authoritative_sha):
                sha_failures += 1
        if str(row.get("schema_version") or "2.0") != "2.0":
            if row.get("local_path") != authoritative_rel or row.get("sha256") != authoritative_sha:
                blocking.append("authoritative_source_fields_inconsistent")
            if not representation.get("original_or_raw_sha256"):
                blocking.append("authoritative_source_sha256_missing")
        normalized_rel = representation.get("crawler_normalized")
        if normalized_rel:
            normalized_path = _safe_package_file(package, normalized_rel)
            normalized_sha = str(representation.get("crawler_normalized_sha256") or "")
            if normalized_path is None:
                unsafe_paths += 1
            if (
                normalized_path is None
                or not normalized_path.is_file()
                or not normalized_sha
                or _sha256(normalized_path) != normalized_sha
            ):
                normalized_sha_failures += 1
            if str(representation.get("crawler_normalized_status") or "").upper() != "PROVISIONAL":
                blocking.append("crawler_normalized_not_provisional")
        if not row.get("provenance"):
            missing_provenance += 1
        route_parts = {part.upper() for part in Path(str(row.get("route_path") or "")).parts}
        if "NEEDS_CLASSIFICATION" in route_parts:
            needs_classification_marked_ready += 1
        chapter_fields_in_handoff += sum(key in row for key in ("primary_section", "book_sections", "provisional_primary_section", "provisional_secondary_sections", "final_primary_section", "final_secondary_sections", "final_section_status"))
        evidence = str(row.get("crawler_evidence_level") or row.get("evidence_level") or "").upper()
        if evidence in FORBIDDEN_CRAWLER_EVIDENCE_LEVELS:
            invalid_fulltext_claim += 1
        for asset in row.get("presentation_assets") or []:
            presentation_asset_count += 1
            asset_rel = str(asset.get("path") or "")
            asset_path = _safe_package_file(package, asset_rel)
            if asset_path is None:
                presentation_asset_sha_failures += 1
                unsafe_paths += 1
                continue
            if not asset_path.is_file() or _sha256(asset_path) != str(asset.get("sha256") or ""):
                presentation_asset_sha_failures += 1
            if not (asset.get("source_url") and asset.get("deck_sha256") and asset.get("slide_number")):
                presentation_asset_provenance_missing += 1

    for row in index:
        evidence = str(row.get("crawler_evidence_level") or row.get("evidence_level") or "").upper()
        if evidence in FORBIDDEN_CRAWLER_EVIDENCE_LEVELS:
            invalid_fulltext_claim += 1

    if sha_failures:
        blocking.append("handoff_sha256_mismatch")
    if missing_provenance:
        blocking.append("handoff_provenance_missing")
    if metadata_reference_marked_ready:
        blocking.append("metadata_reference_marked_ready")
    if source_representation_missing:
        blocking.append("source_representation_missing")
    if normalized_sha_failures:
        blocking.append("crawler_normalized_sha256_mismatch_or_missing")
    if unsafe_paths:
        blocking.append("unsafe_package_path")
    if needs_classification_marked_ready:
        blocking.append("needs_classification_marked_ready")
    if chapter_fields_in_handoff:
        blocking.append("chapter_fields_present_in_handoff")
    if invalid_fulltext_claim:
        blocking.append("invalid_papercrawler_fulltext_claim")
    if presentation_asset_sha_failures:
        blocking.append("presentation_asset_sha256_mismatch_or_missing")
    if presentation_asset_provenance_missing:
        blocking.append("presentation_asset_provenance_missing")

    if not contract or not REQUIRED_CONTRACT_KEYS.issubset(contract):
        blocking.append("handoff_contract_schema_invalid")
    elif str(contract.get("schema_version") or "").split(".")[0] not in {"2", "3"}:
        warnings.append("handoff_contract_schema_version_unexpected")

    coverage = classification.get("coverage") or {}
    if coverage.get("basis") != "book_agnostic_broad_topics" or not coverage.get("informational_only"):
        blocking.append("book_agnostic_topic_coverage_missing")
    for values in (coverage.get("topics") or {}).values():
        chain = [int(values.get(key) or 0) for key in ("discovered", "relevant", "acquired", "handoff")]
        if chain != sorted(chain, reverse=True):
            blocking.append("coverage_internal_inconsistency")
            break

    invalid_routes = [row.get("route_path") for row in manifest if str(row.get("route_path") or "").startswith("/") or ".." in str(row.get("route_path") or "")]
    if invalid_routes:
        blocking.append("invalid_route_path")

    if review:
        warnings.append("manual_review_queue_not_empty")
    qwen_rate = float(classification.get("qwen_review_rate") or 0.0)
    if len(review) > 100:
        warnings.append("manual_review_queue_above_100")

    evidence_counts = Counter(str(row.get("crawler_evidence_level") or row.get("evidence_level") or "UNKNOWN") for row in index)
    decision = "NO_GO" if blocking else "GO"
    result = {
        "gate": "handoff_quality_gate",
        "gate_meaning": "READY_FOR_HANDOFF means a safe, integrity-checked source package for TunnelBookAI ingest; it is not canonical evidence and not a canonical corpus.",
        "decision": decision,
        "advisory_status": "WARNINGS" if warnings else "CLEAR",
        "release_id": release_metadata.get("release_id") or contract.get("release_id"),
        "manifest_sha256": manifest_sha256,
        "total_records": len(index),
        "handoff_records": len(manifest),
        "ready_for_handoff": len(manifest),
        "handoff_eligible": len(manifest),  # migration alias
        "metadata_references": len(metadata_refs),
        "review_required": len(review),
        "retry_acquisition": len(retry),
        "rejected": len(rejected),
        "duplicates_removed": int(reconciliation.get("dedup_removed") or 0),
        "light_pdf_text_records": evidence_counts["LIGHT_PDF_TEXT"],
        "web_snapshot_records": evidence_counts["WEB_SNAPSHOT_TEXT"],
        "abstract_records": evidence_counts["ABSTRACT"],
        "original_acquired_records": evidence_counts["ORIGINAL_ACQUIRED"],
        "title_metadata_only_records": evidence_counts["TITLE_METADATA_ONLY"],
        "coverage": coverage,
        "source_health": _json(audit / "source_health.json"),
        "qwen_review_rate": qwen_rate,
        "sha_failures": sha_failures,
        "missing_provenance": missing_provenance,
        "presentation_assets": presentation_asset_count,
        "presentation_asset_sha_failures": presentation_asset_sha_failures,
        "presentation_asset_provenance_missing": presentation_asset_provenance_missing,
        "crawler_normalized_sha_failures": normalized_sha_failures,
        "unsafe_paths": unsafe_paths,
        "needs_classification_marked_ready": needs_classification_marked_ready,
        "blocking_issues": sorted(set(blocking)),
        "warnings": sorted(set(warnings)),
    }
    package_audit = package / "99_audit"
    package_audit.mkdir(parents=True, exist_ok=True)
    (package_audit / "handoff_quality_gate.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    audit.mkdir(parents=True, exist_ok=True)
    alias = {
        "compatibility_copy": True,
        "canonical_artifact": "99_audit/handoff_quality_gate.json",
        "note": "Copy of the package-local handoff gate; the release artifact is authoritative.",
        **result,
    }
    (audit / "handoff_quality_gate.json").write_text(json.dumps(alias, ensure_ascii=False, indent=2), encoding="utf-8")
    (audit / "corpus_quality_gate.json").write_text(json.dumps(alias, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


# Backward-compatible entry point used by older callers/tests.
def evaluate(output_dir: str | Path, *, package_root: str | Path | None = None) -> dict[str, Any]:
    return evaluate_handoff(output_dir, package_root=package_root)
