#!/usr/bin/env python3
"""Presentation provenance and producer resolution without protected scraping."""
from __future__ import annotations
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import yaml
import classification_engine as classifier

CONFIG = Path(__file__).resolve().parent / "config" / "presentation_sources.yaml"

def _config() -> dict[str, Any]:
    value = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    return value if isinstance(value, dict) else {}

def platform_for(url: str | None) -> str | None:
    host = (urlparse(str(url or "")).hostname or "").casefold()
    for name, cfg in (_config().get("platforms") or {}).items():
        if any(host == d or host.endswith("."+d) for d in cfg.get("domains") or []): return str(name).upper()
    return None

def resolve_presentation(record: dict[str, Any]) -> dict[str, Any]:
    """Resolve producer separately from upload platform using attributable metadata."""
    platform = platform_for(record.get("platform_url") or record.get("source_url") or record.get("landing_url"))
    base = classifier.classify_record(record).as_dict(); producer = base.get("producer") or {}
    name = record.get("actual_producer") or record.get("organization") or producer.get("name")
    original = record.get("original_url") or record.get("institutional_url")
    dtype = str(base.get("document_type") or "PRESENTATION")
    if name and base.get("source_class") in {"TR_OFFICIAL","FOREIGN_GOVERNMENT","ROAD_AUTHORITY","TRANSPORT_AUTHORITY","INTERNATIONAL_OFFICIAL"}: dtype = "OFFICIAL_PRESENTATION"
    elif record.get("conference"): dtype = "CONFERENCE_PRESENTATION"
    elif name and (record.get("university") or base.get("source_class") in {"ACADEMIC","UNIVERSITY_REPOSITORY","RESEARCH_REPOSITORY"}): dtype = "ACADEMIC_PRESENTATION"
    else: dtype = "TECHNICAL_PRESENTATION"
    return {
        "presentation": {"title":record.get("title"),"author":record.get("author") or record.get("authors"),"organization":name,"conference":record.get("conference"),"event":record.get("event"),"year":record.get("year"),"platform":platform,"platform_url":record.get("platform_url") or record.get("source_url"),"original_source_url":original,"download_url":record.get("download_url"),"slide_count":record.get("slide_count"),"description":record.get("description"),"transcript_url":record.get("transcript_url"),"doi":record.get("doi"),"related_paper":record.get("related_paper"),"related_report":record.get("related_report")},
        "document_type": dtype,
        "producer": {**producer,"name":name,"attribution_status":"RESOLVED" if name else "UNKNOWN"},
        "source_class": base.get("source_class") if name else ("RESEARCH_REPOSITORY" if platform in {"ZENODO","FIGSHARE"} else "PRESENTATION_PLATFORM"),
        "authority_tier": base.get("authority_tier") if name else "G",
        "original_source_resolved": bool(original),
        "acquisition_status": record.get("acquisition_status") or ("NO_PUBLIC_FULLTEXT" if not record.get("download_url") else "PUBLIC_DOWNLOAD_AVAILABLE"),
    }
