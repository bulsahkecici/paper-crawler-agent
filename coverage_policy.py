#!/usr/bin/env python3
"""Coverage accounting by discovery, relevance, acquisition and handoff state."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterable

import corpus_policy

AUTHORITY_WEIGHTS = {
    "A1": 1.0, "A2": 0.95, "A3": 0.90,
    "B1": 0.85, "B2": 0.80, "C1": 0.65, "C2": 0.55,
}


def section_ancestors(section_id: str) -> list[str]:
    parts = [part for part in str(section_id).split(".") if part]
    return [".".join(parts[:index]) for index in range(1, len(parts) + 1)]


def record_sections(record: dict[str, Any]) -> set[str]:
    sections = {str(record.get("primary_section") or "").strip()}
    sections.update(str(row.get("id") or "").strip() for row in (record.get("book_sections") or []) if isinstance(row, dict))
    expanded = {ancestor for sid in sections if sid for ancestor in section_ancestors(sid)}
    return expanded


def calculate(records: Iterable[dict[str, Any]], *, include_review_in_discovered: bool = True) -> dict[str, Any]:
    metrics: defaultdict[str, Counter[str]] = defaultdict(Counter)
    totals: Counter[str] = Counter()
    for record in records:
        status = str(record.get("classification_status") or "")
        decision, _ = corpus_policy.handoff_decision(record)
        evidence = str(record.get("evidence_level") or corpus_policy.evidence_level(record))
        is_rejected = decision == "REJECT"
        is_review = decision == "REVIEW"
        relevance_status = str(record.get("relevance_status") or "")
        relevant = relevance_status in {"STRONG", "PROBABLE"} or (not relevance_status and not is_rejected)
        acquired = bool(record.get("handoff_candidate", False)) or str(record.get("acquisition_status") or "") in {"DOWNLOADED_PDF", "SNAPSHOTTED_WEB"}
        discovered = not is_rejected and (include_review_in_discovered or not is_review)
        eligible = decision == "AUTO_HANDOFF" and relevant and acquired
        authority_weight = AUTHORITY_WEIGHTS.get(str(record.get("authority_tier") or "").upper(), 0.45)
        dimensions = {
            "raw_matches": True,
            "relevant_matches": relevant,
            "acquired_matches": relevant and acquired,
            "handoff_candidates": eligible,
            "discovered_count": discovered,
            "accepted_count": status in corpus_policy.AUTO_HANDOFF,
            "review_count": is_review,
            "corpus_eligible_count": eligible,
            "fulltext_count": evidence in {"FULL_TEXT", "PDF_EXTRACT", "WEBPAGE_TEXT"},
        }
        for name, enabled in dimensions.items():
            if enabled:
                totals[name] += 1
        for sid in record_sections(record):
            for name, enabled in dimensions.items():
                if enabled:
                    metrics[sid][name] += 1
            if eligible:
                metrics[sid]["authority_weighted_score"] += authority_weight
        if eligible:
            totals["authority_weighted_score"] += authority_weight
    normalized_sections: dict[str, dict[str, Any]] = {}
    for sid, values in sorted(metrics.items()):
        normalized_sections[sid] = {
            key: round(value, 3) if key == "authority_weighted_score" else value
            for key, value in values.items()
        }
    return {
        "policy_version": "2.0",
        "parent_aggregation": True,
        "gap_basis": "handoff_candidates",
        "totals": {key: round(value, 3) if key == "authority_weighted_score" else value for key, value in totals.items()},
        "sections": normalized_sections,
    }
