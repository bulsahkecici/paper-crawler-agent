#!/usr/bin/env python3
"""Shared, deterministic corpus metadata and acceptance policy helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

AUTO_HANDOFF = {"LLM_ACCEPTED", "ACCEPT_WITH_AUDIT", "AUTO_ACCEPT"}
REVIEW_STATUSES = {"NEEDS_REVIEW", "LOCAL_LLM_REVIEW"}
REJECT_STATUSES = {
    "REJECT_IRRELEVANT", "REJECT_NONCONTENT_PAGE", "REJECT_NAVIGATION",
    "REJECT_LEGAL_PAGE", "DOWNLOAD_INVALID", "DUPLICATE",
}

# PaperCrawler-side evidence vocabulary. These describe only what the crawler
# itself inspected before handoff. PaperCrawler must never emit FULL_TEXT or
# PDF_EXTRACT: full-text evidence is established by TunnelBookAI after Docling
# conversion, OCR/vision and table extraction.
CRAWLER_EVIDENCE_LEVELS = {
    "ABSTRACT", "LIGHT_PDF_TEXT", "WEB_SNAPSHOT_TEXT",
    "TITLE_METADATA_ONLY", "ORIGINAL_ACQUIRED",
}
FORBIDDEN_CRAWLER_EVIDENCE_LEVELS = {"FULL_TEXT", "PDF_EXTRACT"}
# Legacy -> canonical evidence-level names accepted from pre-hardening artifacts.
LEGACY_EVIDENCE_ALIASES = {
    "FULL_TEXT": "LIGHT_PDF_TEXT",
    "PDF_EXTRACT": "LIGHT_PDF_TEXT",
    "WEBPAGE_TEXT": "WEB_SNAPSHOT_TEXT",
}
HANDOFF_EVIDENCE = CRAWLER_EVIDENCE_LEVELS
CONFIG_PATH = Path(__file__).resolve().parent / "config" / "classification_policy.yaml"

DOCUMENT_TYPE_MAP = {
    "JOURNAL_ARTICLE": "journal_article", "REVIEW_ARTICLE": "journal_article",
    "CONFERENCE_PAPER": "conference_paper", "THESIS_PHD": "thesis",
    "THESIS_MSC": "thesis", "TECHNICAL_REPORT": "technical_report",
    "STATISTICAL_REPORT": "technical_report", "COST_REPORT": "technical_report",
    "ACCIDENT_REPORT": "technical_report", "TECHNICAL_STANDARD": "standard",
    "OFFICIAL_REGULATION": "standard", "SPECIFICATION": "specification",
    "TECHNICAL_GUIDELINE": "guideline", "INVENTORY": "inventory",
    "WEB_PAGE": "institutional_webpage", "NEWS": "news", "BOOK": "book",
    "BOOK_CHAPTER": "book_chapter", "PREPRINT": "journal_article",
    "DATASET": "dataset", "MANUAL": "manual", "MANUAL_SECTION": "manual_section",
}


def crawler_evidence_level(record: dict[str, Any]) -> str:
    """Canonical PaperCrawler evidence level. Never FULL_TEXT / PDF_EXTRACT.

    This reports only what the crawler inspected for provisional classification;
    it is not a full-text or final-evidence claim.
    """
    explicit = str(record.get("crawler_evidence_level") or "").upper()
    explicit = LEGACY_EVIDENCE_ALIASES.get(explicit, explicit)
    if explicit in CRAWLER_EVIDENCE_LEVELS:
        return explicit

    classification_input = str(record.get("classification_input") or "").upper()
    light_markers = {"LIGHT_PDF_FIRST_PAGES", "LIGHT_PDF_TEXT", "PARTIAL_PDF_TEXT"}
    if record.get("light_pdf_text_extracted") or classification_input in light_markers:
        return "LIGHT_PDF_TEXT"
    if str(record.get("webpage_text") or record.get("raw_text") or "").strip() or record.get("raw_html_path"):
        return "WEB_SNAPSHOT_TEXT"
    if str(record.get("abstract") or record.get("text_excerpt") or "").strip():
        return "ABSTRACT"
    source = record.get("source_path") or record.get("local_pdf_path") or record.get("pdf_path") or record.get("path")
    if source and Path(str(source)).suffix.lower() == ".pdf":
        return "ORIGINAL_ACQUIRED"
    return "TITLE_METADATA_ONLY"


def evidence_level(record: dict[str, Any]) -> str:
    """Backward-compatible alias. Kept so older callers/tests keep working.

    Delegates to :func:`crawler_evidence_level`; it can never return FULL_TEXT or
    PDF_EXTRACT any more.
    """
    return crawler_evidence_level(record)


def source_tier(record: dict[str, Any]) -> str:
    source_class = str(record.get("source_class") or "").upper()
    authority = str(record.get("authority_tier") or "").upper()
    if source_class in {"TR_OFFICIAL", "INT_OFFICIAL", "STANDARD_BODY"} or authority in {"A1", "A2", "A3"}:
        return "TIER_A"
    if source_class in {"ACADEMIC", "UNIVERSITY", "REPOSITORY"} or authority.startswith("B"):
        return "TIER_B"
    if source_class in {"PROFESSIONAL_ORGANIZATION", "SECTOR_MEDIA", "NEWS"} or authority.startswith("C"):
        return "TIER_C"
    return "TIER_D"


def normalized_document_type(record: dict[str, Any]) -> str:
    explicit = str(record.get("document_type") or "UNKNOWN").upper()
    return DOCUMENT_TYPE_MAP.get(explicit, "unknown")


def handoff_decision(record: dict[str, Any]) -> tuple[str, str]:
    try:
        config = (yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}).get("handoff") or {}
    except (OSError, ValueError):
        config = {}
    auto = set(config.get("allowed_classification_statuses") or AUTO_HANDOFF)
    review = set(config.get("review_classification_statuses") or REVIEW_STATUSES)
    rejected = set(config.get("rejected_classification_statuses") or REJECT_STATUSES)
    min_confidence = float(config.get("min_classification_confidence") or 0.72)
    status = str(record.get("classification_status") or "")
    confidence = float(record.get("classification_confidence") or 0.0)
    if status in rejected:
        return "REJECT", status.lower()
    if status in review:
        return "REVIEW", "classification_review_required"
    if confidence < min_confidence:
        return "REVIEW", "low_classification_confidence"
    if status in auto:
        return "AUTO_HANDOFF", "accepted_policy"
    return "REVIEW", "status_not_auto_handoff"
