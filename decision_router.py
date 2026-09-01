#!/usr/bin/env python3
"""Explainable PaperCrawler decision routing.

This router decides what PaperCrawler should do next.  It deliberately does not
make a final scientific-evidence decision; that belongs to TunnelBookAI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import corpus_policy

AUTO_HANDOFF = "AUTO_HANDOFF"
RETRY_ACQUISITION = "RETRY_ACQUISITION"
RECLASSIFY = "RECLASSIFY"
AUTO_REJECT = "AUTO_REJECT"
MANUAL_REVIEW = "MANUAL_REVIEW"

ACCEPTED_CLASSIFICATIONS = {"AUTO_ACCEPT", "ACCEPT_WITH_AUDIT", "LLM_ACCEPTED"}
REVIEW_CLASSIFICATIONS = {"NEEDS_REVIEW", "LOCAL_LLM_REVIEW"}
RETRYABLE_ACQUISITION = {
    "SOURCE_MISSING", "METADATA_ONLY", "HTTP_403", "HTTP_404", "HTTP_429",
    "DNS_FAILURE", "CONNECT_TIMEOUT", "READ_TIMEOUT", "ROBOTS_BLOCKED",
}
HARD_REJECT_ACQUISITION = {
    "SECURITY_BLOCKED", "BLOCKED_DOMAIN", "INVALID_CONTENT_TYPE", "DOWNLOAD_INVALID",
    "CORRUPT", "EMPTY_PAGE", "LOGIN_ONLY", "SUBSCRIBE_PAGE",
}
HARD_REJECT_CLASSIFICATIONS = {
    "REJECT_IRRELEVANT", "REJECT_NONCONTENT_PAGE", "REJECT_NAVIGATION",
    "REJECT_LEGAL_PAGE", "DOWNLOAD_INVALID", "DUPLICATE",
}


def _source_path(record: dict[str, Any]) -> Path | None:
    value = record.get("source_path") or record.get("local_pdf_path") or record.get("pdf_path") or record.get("path")
    return Path(str(value)).expanduser() if value else None


def _metadata_only_official_exception(record: dict[str, Any]) -> bool:
    """Require an explicit, auditable exception instead of silently weakening the gate."""
    return bool(
        record.get("metadata_only_handoff_exception")
        and str(record.get("source_class") or "").upper() in {"TR_OFFICIAL", "INT_OFFICIAL", "STANDARD_BODY"}
        and (record.get("source_url") or record.get("landing_url"))
    )


def route(record: dict[str, Any], *, source_exists: bool | None = None, sha_valid: bool | None = None) -> dict[str, Any]:
    status = str(record.get("classification_status") or "").upper()
    relevance = str(record.get("relevance_status") or "").upper()
    acquisition = str(record.get("acquisition_status") or "").upper()
    document_type = str(record.get("normalized_document_type") or corpus_policy.normalized_document_type(record)).lower()
    confidence = float(record.get("classification_confidence") or 0.0)
    path = _source_path(record)
    exists = bool(path and path.is_file()) if source_exists is None else source_exists

    if sha_valid is False:
        return _decision(AUTO_REJECT, "sha256_mismatch", "restore or reacquire the intact source")
    if status in HARD_REJECT_CLASSIFICATIONS or relevance == "IRRELEVANT":
        return _decision(AUTO_REJECT, "explicit_irrelevant_or_noncontent", "retain provenance in rejected manifest")
    if acquisition in HARD_REJECT_ACQUISITION:
        return _decision(AUTO_REJECT, "hard_acquisition_or_security_failure", "retain failure details; do not retry automatically")
    if relevance in {"", "WEAK"}:
        if record.get("rule_embedding_disagreement") or record.get("relevance_conflict"):
            return _decision(RECLASSIFY, "relevance_classifier_conflict", "recompute deterministic relevance and taxonomy mapping")
        return _decision(AUTO_REJECT, "insufficient_tunnel_relevance", "retain metadata in rejected manifest")
    if not exists and not _metadata_only_official_exception(record):
        if relevance in {"STRONG", "PROBABLE"}:
            return _decision(RETRY_ACQUISITION, "source_missing", "retry bounded acquisition or official-page snapshot")
        return _decision(AUTO_REJECT, "source_missing_and_not_relevant", "retain metadata in rejected manifest")
    if acquisition in RETRYABLE_ACQUISITION and not exists and not _metadata_only_official_exception(record):
        return _decision(RETRY_ACQUISITION, acquisition.lower(), "retry with provider backoff and content validation")
    if not record.get("primary_section"):
        return _decision(RECLASSIFY, "primary_section_missing", "rerun rules and embedding classification")
    if record.get("rule_embedding_disagreement") and status != "LLM_ACCEPTED":
        return _decision(RECLASSIFY, "rule_embedding_disagreement", "rerun ambiguity arbitration")
    if status in REVIEW_CLASSIFICATIONS:
        if relevance == "STRONG":
            return _decision(RECLASSIFY, "clear_tunnel_source_needs_taxonomy_resolution", "rerun deterministic classification; use local Qwen only if still ambiguous")
        return _decision(MANUAL_REVIEW, "unresolved_probable_classification", "human relevance and section judgment")
    if status not in ACCEPTED_CLASSIFICATIONS:
        return _decision(MANUAL_REVIEW, "unknown_classification_status", "inspect unusual classification metadata")
    if document_type == "unknown":
        return _decision(MANUAL_REVIEW, "document_type_unresolved", "identify unusual document type or authority")
    if relevance == "PROBABLE" and confidence < 0.72:
        return _decision(MANUAL_REVIEW, "probable_low_confidence", "human relevance and section judgment")
    return _decision(AUTO_HANDOFF, "handoff_requirements_satisfied", "send to TunnelBookAI for final evidence evaluation")


def _decision(decision: str, reason: str, action: str) -> dict[str, str]:
    return {"decision": decision, "reason": reason, "recommended_action": action}


def queue_entry(record: dict[str, Any], routed: dict[str, str]) -> dict[str, Any]:
    sections = record.get("book_sections") or []
    return {
        "document_id": record.get("document_key") or record.get("canonical_id") or record.get("doi") or record.get("source_sha256"),
        "title": record.get("title"),
        "source_path": record.get("source_path"),
        "source_sha256": record.get("source_sha256"),
        "discovery_source": record.get("discovery_source") or record.get("source"),
        "source_url": record.get("source_url"),
        "landing_url": record.get("landing_url"),
        "pdf_url": record.get("pdf_url"),
        "document_type": record.get("normalized_document_type") or record.get("document_type"),
        "source_class": record.get("source_class"),
        "authority_tier": record.get("authority_tier"),
        "relevance_score": record.get("tunnel_relevance_score", record.get("relevance_score")),
        "relevance_status": record.get("relevance_status"),
        "primary_section": record.get("primary_section"),
        "book_sections": sections,
        "classification_status": record.get("classification_status"),
        "acquisition_status": record.get("acquisition_status"),
        "decision": routed["decision"],
        "reason": routed["reason"],
        "recommended_action": routed["recommended_action"],
    }
