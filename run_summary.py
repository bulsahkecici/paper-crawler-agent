#!/usr/bin/env python3
"""Write machine-readable and Markdown summaries of one PaperCrawler pipeline run."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
import tunnel_harvest as harvest


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _jsonl_count(path: Path) -> int:
    try:
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    except OSError:
        return 0


def write(output_dir: str | Path | None = None) -> dict[str, Any]:
    if output_dir is not None:
        harvest.set_output_dir(output_dir)
    root = harvest.OUTPUT_DIR
    audit = root / "audit"
    discovery = _read(audit / "discovery_audit.json")
    classification = _read(audit / "classification_audit.json")
    dedup = _read(audit / "source_dedup_version_audit.json")
    gap = _read(audit / "gap_discovery_audit.json")
    handoff = _read(root / "exports" / "TunnelBookAI_Source_Pack" / "99_audit" / "handoff_audit.json")
    handoff_audit = root / "exports" / "TunnelBookAI_Source_Pack" / "99_audit"
    acquisition = discovery.get("acquisition_counts") or {}
    coverage_report = classification.get("coverage") or {}
    coverage_sections = coverage_report.get("sections") or {}
    with (Path(__file__).resolve().parent / "config" / "coverage_targets.yaml").open("r", encoding="utf-8") as handle:
        targets = (yaml.safe_load(handle) or {}).get("sections") or {}
    gaps = sorted(
        [{"section": sid, "current": int((coverage_sections.get(sid) or {}).get("handoff_candidates") or 0), "target": int(target), "gap": max(0, int(target) - int((coverage_sections.get(sid) or {}).get("handoff_candidates") or 0)), **(coverage_sections.get(sid) or {})} for sid, target in targets.items()],
        key=lambda item: item["gap"], reverse=True,
    )
    summary = {
        "discovery": {"queries": discovery.get("queries", 0), "discovered": discovery.get("discovered_unique", 0), "rejected_irrelevant": discovery.get("rejected_irrelevant", 0), "relevant_candidates": discovery.get("relevant_candidates", 0)},
        "acquisition": acquisition,
        "classification": {key: classification.get(key, 0) for key in ("documents", "classification_total", "deterministic_only", "rule_embedding_auto", "rules_embedding_only_count", "qwen_reviewed_count", "qwen_review_rate", "manual_review_count", "irrelevant_rejected_count")},
        "handoff": {
            **{key: handoff.get(key, 0) for key in ("ready_for_handoff", "metadata_references", "retry_acquisition", "manual_review", "rejected")},
            "review_queue": _jsonl_count(handoff_audit / "review_queue.jsonl"),
            "metadata_references_queue": _jsonl_count(handoff_audit / "metadata_references.jsonl"),
            "rejected_manifest": _jsonl_count(handoff_audit / "rejected_manifest.jsonl"),
            "decision_counts": handoff.get("decision_counts") or {},
        },
        "coverage": {"sections_meeting_target": sum(1 for item in gaps if not item["gap"]), "sections_below_target": sum(1 for item in gaps if item["gap"]), "worst_10_gaps": gaps[:10]},
        "dedup": {key: dedup.get(key, 0) for key in ("exact_duplicate_groups", "same_doi_groups", "canonical_url_groups", "fuzzy_review_pairs")},
        "gap_search": {key: gap.get(key) for key in ("gap_search_completed", "catalog_additions", "provider_degraded", "gap_additions_reason")},
    }
    index_rows = _read_jsonl_rows(root / "classification_index.jsonl")
    evidence_counter: dict[str, int] = {}
    acquired_originals = 0
    for row in index_rows:
        level = str(row.get("crawler_evidence_level") or row.get("evidence_level") or "TITLE_METADATA_ONLY")
        evidence_counter[level] = evidence_counter.get(level, 0) + 1
        if str(row.get("acquisition_status") or "").upper() in {"DOWNLOADED_PDF", "SNAPSHOTTED_WEB"}:
            acquired_originals += 1
    summary["evidence"] = {
        "acquired_originals": acquired_originals,
        "light_pdf_text_records": evidence_counter.get("LIGHT_PDF_TEXT", 0),
        "web_snapshot_records": evidence_counter.get("WEB_SNAPSHOT_TEXT", 0),
        "abstract_records": evidence_counter.get("ABSTRACT", 0),
        "original_acquired_records": evidence_counter.get("ORIGINAL_ACQUIRED", 0),
        "title_metadata_only_records": evidence_counter.get("TITLE_METADATA_ONLY", 0),
        "boundary": "PaperCrawler evidence levels never include FULL_TEXT/PDF_EXTRACT; full-text is a TunnelBookAI outcome.",
    }
    classification_rows = _jsonl_count(root / "classification_index.jsonl")
    physical_sidecars = len(list((root / "classifications").glob("*.classification.json")))
    source_files = sum(1 for folder in (root / "pdfs", root / "discovery_sources") if folder.exists() for path in folder.rglob("*") if path.is_file())
    summary["data_integrity"] = {
        "physical_source_files": source_files,
        "current_catalog_records": _jsonl_count(root / "discovery_catalog.jsonl"),
        "current_classification_records": classification_rows,
        "current_unique_works": int((classification.get("reconciliation") or {}).get("classification_output") or classification_rows),
        "current_handoff_records": int(handoff.get("ready_for_handoff") or 0),
        "physical_classification_sidecars": physical_sidecars,
        "stale_classification_sidecars": max(0, physical_sidecars - classification_rows),
    }
    (audit / "coverage.json").write_text(json.dumps(coverage_report, ensure_ascii=False, indent=2), encoding="utf-8")
    (audit / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# PaperCrawler Run Summary", "", "## Discovery", f"- Queries: {summary['discovery']['queries']}", f"- Discovered: {summary['discovery']['discovered']}", f"- Rejected irrelevant: {summary['discovery']['rejected_irrelevant']}", f"- Relevant candidates: {summary['discovery']['relevant_candidates']}", "", "## Acquisition"]
    lines += [f"- {key}: {value}" for key, value in sorted(acquisition.items())]
    decisions = summary["handoff"]["decision_counts"]
    lines += ["", "## Classification", f"- Deterministic/rule + embedding: {summary['classification']['rules_embedding_only_count']}", f"- Qwen reviewed: {summary['classification']['qwen_reviewed_count']} ({summary['classification']['qwen_review_rate']:.1%})", f"- True manual review: {decisions.get('MANUAL_REVIEW', 0)}", f"- Reclassify queue: {decisions.get('RECLASSIFY', 0)}", f"- Rejected irrelevant: {summary['classification']['irrelevant_rejected_count']}", "", "## Decision Router"]
    lines += [f"- {name}: {decisions.get(name, 0)}" for name in ("AUTO_HANDOFF", "RETRY_ACQUISITION", "RECLASSIFY", "AUTO_REJECT", "MANUAL_REVIEW")]
    lines += ["", "## Handoff", f"- READY_FOR_HANDOFF: {summary['handoff']['ready_for_handoff']}", f"- Route distribution: {handoff.get('route_counts') or {}}", "", "## Coverage", f"- Sections meeting target: {summary['coverage']['sections_meeting_target']}", f"- Sections below target: {summary['coverage']['sections_below_target']}"]
    lines += [f"- {item['section']}: {item['current']}/{item['target']} (gap {item['gap']})" for item in gaps[:10]]
    lines += ["", "## Dedup", *[f"- {key}: {value}" for key, value in summary["dedup"].items()], "", "## Health", f"- Provider degradation: {summary['gap_search'].get('provider_degraded') or False}", f"- Stale sidecars: {summary['data_integrity']['stale_classification_sidecars']}", "", "## Final Decision", "- See `audit/handoff_quality_gate.json`; PaperCrawler readiness never implies final evidence approval or a canonical corpus.", ""]
    (audit / "run_summary.md").write_text("\n".join(lines), encoding="utf-8")
    section_values = {sid: int((coverage_sections.get(sid) or {}).get("handoff_candidates") or 0) for sid in ("5", "6", "4.3.5")}
    old_new = [
        ("classification records", "1652", classification.get("documents", 0)),
        ("Qwen reviewed", "1592", classification.get("qwen_reviewed_count", 0)),
        ("Qwen rate", "96.37%", f"{float(classification.get('qwen_review_rate') or 0):.2%}"),
        ("manual/review queue", "905", decisions.get("MANUAL_REVIEW", 0)),
        ("insufficient evidence review", "168", 0),
        ("source missing in human review", "60", sum(1 for row in _read_jsonl_rows(handoff_audit / "review_queue.jsonl") if row.get("reason") == "source_missing")),
        ("irrelevant rejects", "n/a", decisions.get("AUTO_REJECT", 0)),
        ("auto handoff", "124", decisions.get("AUTO_HANDOFF", 0)),
        ("retry acquisition", "n/a", decisions.get("RETRY_ACQUISITION", 0)),
        ("reclassify", "n/a", decisions.get("RECLASSIFY", 0)),
        ("auto reject", "n/a", decisions.get("AUTO_REJECT", 0)),
        ("true manual review", "n/a", decisions.get("MANUAL_REVIEW", 0)),
        ("fuzzy duplicate candidates", "11", summary["dedup"].get("fuzzy_review_pairs", 0)),
        ("section 5 handoff coverage", "0/70", f"{section_values['5']}/70"),
        ("section 6 handoff coverage", "1/70", f"{section_values['6']}/70"),
        ("section 4.3.5 coverage", "0/40", f"{section_values['4.3.5']}/40"),
    ]
    comparison = ["# PaperCrawler Productization Comparison", "", "| Metric | Old | New |", "|---|---:|---:|"]
    comparison += [f"| {name} | {old} | {new} |" for name, old, new in old_new]
    comparison += ["", "Counts are compared by semantic route, not by blindly treating every increase as an improvement. Evidence-tier review was removed because TunnelBookAI owns final evidence approval; missing sources now route to acquisition retry; reclassification and true human judgment are separate queues. Coverage uses relevant acquired handoff candidates with parent roll-up.", ""]
    (audit / "productization_comparison.md").write_text("\n".join(comparison), encoding="utf-8")
    return summary


def _read_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError):
        return []
