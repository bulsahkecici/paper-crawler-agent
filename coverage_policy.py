#!/usr/bin/env python3
"""Informational broad-topic coverage; never a product acceptance threshold."""
from __future__ import annotations
from collections import Counter, defaultdict
from typing import Any, Iterable
import corpus_policy

AUTHORITY_WEIGHTS = {"A1":1.0,"A2":.95,"A3":.90,"A4":.88,"B1":.85,"B2":.80,"C1":.65,"C2":.55,"D1":.70,"D2":.55,"D3":.50,"E1":.60,"E2":.48,"E3":.40}

def record_topics(record: dict[str, Any]) -> set[str]: return {str(x).strip() for x in record.get("topics") or [] if str(x).strip()}

def calculate(records: Iterable[dict[str, Any]], *, include_review_in_discovered: bool = True) -> dict[str, Any]:
    metrics: defaultdict[str, Counter[str]] = defaultdict(Counter); totals: Counter[str] = Counter()
    for record in records:
        decision, _ = corpus_policy.handoff_decision(record); relevance = str(record.get("relevance_status") or "")
        relevant = relevance in {"STRONG","PROBABLE"}; acquired = bool(record.get("handoff_candidate")) or str(record.get("acquisition_status") or "") in {"DOWNLOADED_PDF","SNAPSHOTTED_WEB","DOWNLOADED_ORIGINAL"}
        handoff = decision == "AUTO_HANDOFF" and relevant and acquired
        flags = {"discovered": decision != "REJECT", "relevant": relevant, "acquired": relevant and acquired, "handoff": handoff}
        for key, enabled in flags.items(): totals[key] += int(enabled)
        for topic in record_topics(record):
            for key, enabled in flags.items(): metrics[topic][key] += int(enabled)
            if handoff: metrics[topic]["authority_weight_milli"] += int(AUTHORITY_WEIGHTS.get(str(record.get("authority_tier") or ""), .35)*1000)
    topics = {}
    for topic, values in sorted(metrics.items()):
        row = dict(values); row["authority_weighted_handoff"] = round(row.pop("authority_weight_milli",0)/1000,3); topics[topic] = row
    return {"schema_version":"1.0","basis":"book_agnostic_broad_topics","informational_only":True,"targets_required_for_go":False,"topics":topics,"totals":dict(totals)}

# Legacy function names remain importable but no chapter expansion occurs.
def record_sections(record: dict[str, Any]) -> set[str]: return set()
def section_ancestors(section_id: str) -> list[str]: return []
