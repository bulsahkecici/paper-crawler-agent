#!/usr/bin/env python3
"""Final fail-closed quality gate for TunnelBookAI handoff artifacts."""

from __future__ import annotations

import json
import hashlib
from collections import Counter
from pathlib import Path
from typing import Any


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


def evaluate(output_dir: str | Path, *, package_root: str | Path | None = None) -> dict[str, Any]:
    root = Path(output_dir)
    audit = root / "audit"
    state = _json(audit / "pipeline_state.json")
    classification = _json(audit / "classification_audit.json")
    package = Path(package_root).resolve() if package_root else root / "exports" / "TunnelBookAI_Source_Pack"
    manifest, manifest_ok = _jsonl(package / "00_registry" / "handoff_manifest.jsonl")
    review, _ = _jsonl(package / "99_audit" / "review_queue.jsonl")
    rejected, _ = _jsonl(package / "99_audit" / "rejected_manifest.jsonl")
    index, index_ok = _jsonl(root / "classification_index.jsonl")
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
    canonical_ids = [str(row.get("canonical_id") or "") for row in manifest]
    if "" in canonical_ids or len(canonical_ids) != len(set(canonical_ids)):
        blocking.append("duplicate_or_missing_canonical_id")
    physical_rows = [row for row in manifest if not row.get("metadata_only_official_exception")]
    shas = [str(row.get("sha256") or "") for row in physical_rows]
    if "" in shas or len(shas) != len(set(shas)):
        blocking.append("duplicate_or_missing_sha256")
    sha_failures = 0
    missing_provenance = 0
    for row in manifest:
        if not row.get("metadata_only_official_exception"):
            local_path = package / str(row.get("local_path") or "")
            if not local_path.is_file():
                blocking.append("handoff_file_missing")
                continue
            actual = hashlib.sha256(local_path.read_bytes()).hexdigest()
            if actual != str(row.get("sha256") or ""):
                sha_failures += 1
        if not row.get("provenance"):
            missing_provenance += 1
    if sha_failures:
        blocking.append("handoff_sha256_mismatch")
    if missing_provenance:
        blocking.append("handoff_provenance_missing")
    coverage = classification.get("coverage") or {}
    if not coverage.get("parent_aggregation"):
        blocking.append("coverage_policy_missing")
    for values in (coverage.get("sections") or {}).values():
        chain = [int(values.get(key) or 0) for key in ("raw_matches", "relevant_matches", "acquired_matches", "handoff_candidates")]
        if chain != sorted(chain, reverse=True):
            blocking.append("coverage_internal_inconsistency")
            break
    if review:
        warnings.append("manual_review_queue_not_empty")
    qwen_rate = float(classification.get("qwen_review_rate") or 0.0)
    if qwen_rate > 0.20:
        warnings.append("qwen_review_rate_above_20_percent")
    if len(review) > 100:
        warnings.append("manual_review_queue_above_100")
    evidence = Counter(str(row.get("evidence_level") or "UNKNOWN") for row in index)
    decision = "NO_GO" if blocking else ("CONDITIONAL_GO" if warnings else "GO")
    result = {
        "decision": decision, "total_records": len(index), "canonical_records": len(index),
        "handoff_eligible": len(manifest), "review_required": len(review), "rejected": len(rejected),
        "duplicates_removed": int(reconciliation.get("dedup_removed") or 0),
        "fulltext_records": evidence["FULL_TEXT"] + evidence["PDF_EXTRACT"] + evidence["WEBPAGE_TEXT"],
        "abstract_only_records": evidence["ABSTRACT"], "title_only_records": evidence["TITLE_METADATA_ONLY"],
        "coverage": coverage, "source_health": _json(audit / "source_health.json"),
        "qwen_review_rate": qwen_rate, "sha_failures": sha_failures,
        "missing_provenance": missing_provenance,
        "blocking_issues": sorted(set(blocking)), "warnings": warnings,
    }
    audit.mkdir(parents=True, exist_ok=True)
    (audit / "corpus_quality_gate.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
