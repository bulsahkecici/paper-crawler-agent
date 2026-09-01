#!/usr/bin/env python3
"""Fail-closed quality gate for the PaperCrawler -> TunnelBookAI handoff package.

This gate evaluates PaperCrawler's *handoff package* only. It never evaluates a
canonical TunnelBookAI corpus: that responsibility, and the name
``corpus_quality_gate``, belong to TunnelBookAI's ingest layer.

Authoritative output: ``audit/handoff_quality_gate.json``.
A deprecated compatibility alias ``audit/corpus_quality_gate.json`` is also
written (single evaluation, no second computation).
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

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


def _taxonomy_ids() -> set[str]:
    try:
        payload = yaml.safe_load((CONFIG_DIR / "taxonomy.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return set()
    return {str(sid) for sid in (payload.get("sections") or {})}


def evaluate_handoff(output_dir: str | Path, *, package_root: str | Path | None = None) -> dict[str, Any]:
    root = Path(output_dir)
    audit = root / "audit"
    state = _json(audit / "pipeline_state.json")
    classification = _json(audit / "classification_audit.json")
    package = Path(package_root).resolve() if package_root else root / "exports" / "TunnelBookAI_Source_Pack"
    manifest, manifest_ok = _jsonl(package / "00_registry" / "handoff_manifest.jsonl")
    contract = _json(package / "00_registry" / "handoff_contract.json")
    review, _ = _jsonl(package / "99_audit" / "review_queue.jsonl")
    retry, _ = _jsonl(package / "99_audit" / "retry_acquisition.jsonl")
    metadata_refs, _ = _jsonl(package / "99_audit" / "metadata_references.jsonl")
    rejected, _ = _jsonl(package / "99_audit" / "rejected_manifest.jsonl")
    index, index_ok = _jsonl(root / "classification_index.jsonl")
    taxonomy_ids = _taxonomy_ids()

    blocking: list[str] = []
    warnings: list[str] = []

    required_stages = {"free_discovery", "pdf_enrichment", "initial_classification", "source_audit", "handoff"}
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
    invalid_provisional_section = 0
    invalid_fulltext_claim = 0
    for row in manifest:
        status = str(row.get("paper_crawler_status") or "READY_FOR_HANDOFF").upper()
        if status != "READY_FOR_HANDOFF" or row.get("metadata_only_official_exception"):
            metadata_reference_marked_ready += 1
        local_path = package / str(row.get("local_path") or row.get("source_path") or "")
        if not str(row.get("local_path") or row.get("source_path") or "") or not local_path.is_file():
            blocking.append("handoff_file_missing")
        else:
            actual = hashlib.sha256(local_path.read_bytes()).hexdigest()
            if actual != str(row.get("sha256") or ""):
                sha_failures += 1
        if not row.get("provenance"):
            missing_provenance += 1
        if "source_representation" not in row:
            source_representation_missing += 1
        prov_section = str(row.get("provisional_primary_section") or "")
        if prov_section and taxonomy_ids and prov_section not in taxonomy_ids:
            invalid_provisional_section += 1
        if row.get("final_primary_section") not in (None, "") or str(row.get("final_section_status") or "NOT_EVALUATED").upper() != "NOT_EVALUATED":
            invalid_provisional_section += 1
        evidence = str(row.get("crawler_evidence_level") or row.get("evidence_level") or "").upper()
        if evidence in FORBIDDEN_CRAWLER_EVIDENCE_LEVELS:
            invalid_fulltext_claim += 1

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
    if invalid_provisional_section:
        blocking.append("invalid_provisional_section")
    if invalid_fulltext_claim:
        blocking.append("invalid_papercrawler_fulltext_claim")

    if not contract or not REQUIRED_CONTRACT_KEYS.issubset(contract):
        blocking.append("handoff_contract_schema_invalid")
    elif str(contract.get("schema_version") or "").split(".")[0] not in {"2", "3"}:
        warnings.append("handoff_contract_schema_version_unexpected")

    coverage = classification.get("coverage") or {}
    if not coverage.get("parent_aggregation"):
        blocking.append("coverage_policy_missing")
    for values in (coverage.get("sections") or {}).values():
        chain = [int(values.get(key) or 0) for key in ("raw_matches", "relevant_matches", "acquired_matches", "handoff_candidates")]
        if chain != sorted(chain, reverse=True):
            blocking.append("coverage_internal_inconsistency")
            break

    invalid_routes = [row.get("route_path") for row in manifest if str(row.get("route_path") or "").startswith("/") or ".." in str(row.get("route_path") or "")]
    if invalid_routes:
        blocking.append("invalid_route_path")

    if review:
        warnings.append("manual_review_queue_not_empty")
    qwen_rate = float(classification.get("qwen_review_rate") or 0.0)
    if qwen_rate > 0.20:
        warnings.append("qwen_review_rate_above_20_percent")
    if len(review) > 100:
        warnings.append("manual_review_queue_above_100")

    evidence_counts = Counter(str(row.get("crawler_evidence_level") or row.get("evidence_level") or "UNKNOWN") for row in index)
    decision = "NO_GO" if blocking else ("CONDITIONAL_GO" if warnings else "GO")
    result = {
        "gate": "handoff_quality_gate",
        "gate_meaning": "READY_FOR_HANDOFF means a safe, integrity-checked source package for TunnelBookAI ingest; it is not canonical evidence and not a canonical corpus.",
        "decision": decision,
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
        "blocking_issues": sorted(set(blocking)),
        "warnings": sorted(set(warnings)),
    }
    audit.mkdir(parents=True, exist_ok=True)
    (audit / "handoff_quality_gate.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    alias = {
        "deprecated_alias": True,
        "canonical_artifact": "audit/handoff_quality_gate.json",
        "note": "PaperCrawler evaluates only its handoff package. 'corpus_quality_gate' belongs to TunnelBookAI.",
        **result,
    }
    (audit / "corpus_quality_gate.json").write_text(json.dumps(alias, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


# Backward-compatible entry point used by older callers/tests.
def evaluate(output_dir: str | Path, *, package_root: str | Path | None = None) -> dict[str, Any]:
    return evaluate_handoff(output_dir, package_root=package_root)
