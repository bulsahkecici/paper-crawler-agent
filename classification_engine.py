#!/usr/bin/env python3
"""Deterministic first-pass classification for harvested tunnel sources.

This module is intentionally independent of the local LLM. It classifies source
metadata into document type, authority tier, TunnelBookAI book sections, topic
tags, and a deterministic routing path. LLM/embedding review can be layered on
later only for low-confidence or conflicting cases.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    import yaml
except ImportError as exc:  # pragma: no cover - dependency failure is explicit
    raise RuntimeError("PyYAML is required for classification config") from exc

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT / "config"

DOCUMENT_TYPES = {
    "OFFICIAL_REGULATION",
    "TECHNICAL_STANDARD",
    "TECHNICAL_GUIDELINE",
    "INVENTORY",
    "STATISTICAL_REPORT",
    "COST_REPORT",
    "ACCIDENT_REPORT",
    "JOURNAL_ARTICLE",
    "REVIEW_ARTICLE",
    "CONFERENCE_PAPER",
    "THESIS_PHD",
    "THESIS_MSC",
    "TECHNICAL_REPORT",
    "BOOK",
    "BOOK_CHAPTER",
    "NEWS",
    "WEB_PAGE",
    "PREPRINT",
    "DISCOVERY_RECORD",
    "AI_NOTE",
    "UNKNOWN",
}

TOPIC_TERMS: dict[str, tuple[str, ...]] = {
    "history": ("history", "tarihçe", "historical", "ancient"),
    "classification": ("classification", "sınıflandır"),
    "NATM": ("natm", "new austrian"),
    "TBM": ("tbm", "tunnel boring machine"),
    "EPB": ("epb", "earth pressure balance"),
    "shield": ("shield tunnel", "shield tunn"),
    "drill_and_blast": ("drill and blast", "drill-and-blast"),
    "immersed_tube": ("immersed tube", "immersed tunnel"),
    "rock_tunnel": ("rock tunnel", "hard rock", "rock mass"),
    "soft_ground": ("soft ground", "soft soil"),
    "underwater_tunnel": ("underwater tunnel", "subaqueous", "cross-river tunnel"),
    "shotcrete": ("shotcrete", "püskürtme beton"),
    "rock_bolt": ("rock bolt", "rockbolt", "kaya bulonu"),
    "lining": ("lining", "kaplama"),
    "waterproofing": ("waterproof", "waterproofing", "yalıtım"),
    "geology": ("geology", "geological", "jeoloji", "jeolojik"),
    "geotechnics": ("geotechnical", "geotechnics", "jeoteknik"),
    "route_selection": ("route selection", "alignment selection", "güzergah"),
    "monitoring": ("monitoring", "instrumentation", "izleme", "shm"),
    "construction_cost": ("construction cost", "yapım maliyeti", "unit cost", "capex"),
    "cost_driver": ("cost driver", "cost factor", "cost overrun", "maliyete etki"),
    "life_cycle_cost": ("life cycle cost", "lifecycle cost", "whole life cost", "yaşam döngüsü"),
    "structural_maintenance": ("structural maintenance", "lining repair", "yapısal bakım"),
    "electromechanical": ("electromechanical", "electro-mechanical", "elektromekanik", "scada"),
    "periodic_maintenance": ("periodic maintenance", "scheduled maintenance", "periyodik bakım"),
    "operation_cost": ("operation cost", "operating cost", "opex", "işletme maliyeti"),
    "maintenance_cost": ("maintenance cost", "bakım maliyeti", "repair cost"),
    "energy": ("energy", "enerji", "electricity"),
    "ventilation": ("ventilation", "havalandırma", "jet fan"),
    "lighting": ("lighting", "aydınlatma", "led"),
    "safety": ("safety", "güvenlik", "emergency"),
    "fire": ("tunnel fire", "yangın", "fire safety"),
    "accident": ("accident", "disaster", "fatal", "kaza", "felaket"),
    "KGM": ("kgm", "karayolları genel müdürlüğü", "general directorate of highways"),
    "Turkey": ("turkey", "türkiye", "turkish"),
}

DOC_TYPE_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("TECHNICAL_STANDARD", ("technical specification", "teknik şartname", "şartname", "standard specification")),
    ("TECHNICAL_GUIDELINE", ("guideline", "manual", "yönerge", "rehber", "design guide")),
    ("INVENTORY", ("inventory", "envanter")),
    ("COST_REPORT", ("cost report", "maliyet kitabı", "birim fiyat", "unit price", "maintenance cost report")),
    ("ACCIDENT_REPORT", ("accident investigation", "investigation report", "kaza raporu", "fire investigation")),
    ("STATISTICAL_REPORT", ("statistics", "istatistik", "statistical report")),
    ("REVIEW_ARTICLE", ("systematic review", "literature review", "state of the art", "review article")),
    ("THESIS_PHD", ("doctoral thesis", "phd thesis", "doktora tezi")),
    ("THESIS_MSC", ("master's thesis", "masters thesis", "yüksek lisans tezi")),
    ("CONFERENCE_PAPER", ("conference paper", "proceedings", "symposium")),
    ("BOOK_CHAPTER", ("book chapter",)),
    ("PREPRINT", ("preprint", "arxiv")),
    ("NEWS", ("news", "haber", "press release", "basın açıklaması")),
]


@dataclass
class SectionScore:
    id: str
    title: str
    score: float
    strong_matches: list[str]
    medium_matches: list[str]


@dataclass
class ClassificationResult:
    document_type: str
    source_class: str
    authority_tier: str
    evidence_priority: int
    publisher_code: str | None
    primary_section: str | None
    book_sections: list[dict[str, Any]]
    topics: list[str]
    classification_confidence: float
    classification_status: str
    route_path: str
    methods: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _load_yaml(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"{name} must contain a mapping")
    return payload


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _blob(record: dict[str, Any]) -> str:
    parts = [
        record.get("title"),
        record.get("abstract"),
        record.get("keywords"),
        record.get("venue"),
        record.get("publisher"),
        record.get("source"),
        record.get("landing_url"),
        record.get("pdf_url"),
    ]
    if isinstance(record.get("keywords"), list):
        parts[2] = " ".join(str(x) for x in record["keywords"])
    return _norm(" ".join(str(x or "") for x in parts))


def detect_document_type(record: dict[str, Any]) -> tuple[str, float, str]:
    explicit = str(record.get("document_type") or "").upper()
    if explicit in DOCUMENT_TYPES and explicit != "UNKNOWN":
        return explicit, 1.0, "explicit_metadata"

    source = _norm(record.get("source"))
    venue = _norm(record.get("venue"))
    title = _norm(record.get("title"))
    blob = _blob(record)

    if source == "arxiv" or "arxiv" in venue:
        return "PREPRINT", 0.99, "source_metadata"
    if source in {"openalex", "crossref", "doaj", "europe_pmc", "semantic_scholar", "core", "openaire"}:
        if any(term in blob for term in DOC_TYPE_RULES[6][1]):
            return "REVIEW_ARTICLE", 0.92, "academic_metadata_plus_title"
        return "JOURNAL_ARTICLE", 0.92, "academic_source_metadata"

    degree = _norm(record.get("degree") or record.get("thesis_level"))
    if "doctor" in degree or "phd" in degree or "doktora" in degree:
        return "THESIS_PHD", 0.99, "degree_metadata"
    if "master" in degree or "yüksek lisans" in degree:
        return "THESIS_MSC", 0.99, "degree_metadata"

    for doc_type, terms in DOC_TYPE_RULES:
        matches = [term for term in terms if term in blob]
        if matches:
            confidence = 0.90 if any(term in title for term in matches) else 0.76
            return doc_type, confidence, "title_content_rules"

    url = _norm(record.get("landing_url") or record.get("source_url"))
    if "/haber" in url or "/news" in url or "/press" in url:
        return "NEWS", 0.86, "url_pattern"
    if url.startswith("http"):
        return "WEB_PAGE", 0.58, "web_fallback"
    return "UNKNOWN", 0.25, "insufficient_metadata"


def _hostname(record: dict[str, Any]) -> str:
    for key in ("source_url", "landing_url", "pdf_url"):
        value = str(record.get(key) or "")
        if not value:
            continue
        parsed = urlparse(value)
        if parsed.hostname:
            return parsed.hostname.lower()
    return ""


def detect_source_class(record: dict[str, Any], document_type: str) -> tuple[str, str | None, float]:
    policy = _load_yaml("source_policy.yaml")
    host = _hostname(record)
    source = _norm(record.get("source"))
    publisher = _norm(record.get("publisher"))
    blob = f"{host} {publisher} {_blob(record)}"

    publisher_code = None
    if "kgm.gov.tr" in host or "karayolları genel müdürlüğü" in blob or re.search(r"\bkgm\b", blob):
        return "TR_OFFICIAL", "KGM", 0.99

    classes = policy.get("source_classes") or {}
    for cls_name, cfg in classes.items():
        if cls_name in {"ACADEMIC", "UNKNOWN"}:
            continue
        for domain in cfg.get("known_domains") or []:
            if host == domain or host.endswith("." + domain):
                return cls_name, publisher_code, 0.97
        for suffix in cfg.get("domain_suffixes") or []:
            suffix = str(suffix).lstrip(".")
            if host == suffix or host.endswith("." + suffix):
                return cls_name, publisher_code, 0.91
        for fragment in cfg.get("domain_fragments") or []:
            if fragment in host:
                return cls_name, publisher_code, 0.86

    academic_sources = set((classes.get("ACADEMIC") or {}).get("discovery_sources") or [])
    if source in academic_sources or document_type in {"JOURNAL_ARTICLE", "REVIEW_ARTICLE", "PREPRINT"}:
        return "ACADEMIC", publisher_code, 0.92
    if document_type in {"THESIS_PHD", "THESIS_MSC"}:
        return "UNIVERSITY_REPOSITORY", publisher_code, 0.88
    if document_type == "DISCOVERY_RECORD":
        return "DISCOVERY_ONLY", publisher_code, 0.95
    if document_type == "NEWS":
        return "NEWS", publisher_code, 0.72
    return "UNKNOWN", publisher_code, 0.35


def authority_for(source_class: str, document_type: str) -> tuple[str, int]:
    policy = _load_yaml("source_policy.yaml")
    overrides = policy.get("document_type_tier_overrides") or {}
    # Official/standards authority should not be downgraded by generic document type.
    if source_class in {"TR_OFFICIAL", "INT_OFFICIAL", "STANDARD_BODY", "PROFESSIONAL_ORGANIZATION"}:
        tier = (policy.get("source_classes", {}).get(source_class) or {}).get("default_tier", "E")
    else:
        tier = overrides.get(document_type) or (policy.get("source_classes", {}).get(source_class) or {}).get("default_tier", "E")
    priority = int((policy.get("authority_tiers", {}).get(tier) or {}).get("evidence_priority", 0))
    return str(tier), priority


def score_sections(record: dict[str, Any], max_sections: int = 5) -> list[SectionScore]:
    taxonomy = _load_yaml("taxonomy.yaml")
    blob = _blob(record)
    title = _norm(record.get("title"))
    scored: list[SectionScore] = []
    for section_id, cfg in (taxonomy.get("sections") or {}).items():
        strong = [str(t).lower() for t in cfg.get("strong_terms") or [] if _norm(t) in blob]
        medium = [str(t).lower() for t in cfg.get("medium_terms") or [] if _norm(t) in blob]
        if not strong and not medium:
            continue
        raw = 0.0
        for term in strong:
            raw += 5.0 if term in title else 3.0
        for term in medium:
            raw += 2.0 if term in title else 1.0
        # Saturating score: interpretable 0..1 without requiring training data.
        score = min(0.99, raw / (raw + 4.0))
        scored.append(
            SectionScore(
                id=str(section_id),
                title=str(cfg.get("title") or section_id),
                score=round(score, 4),
                strong_matches=strong,
                medium_matches=medium,
            )
        )
    scored.sort(key=lambda x: x.score, reverse=True)
    return scored[:max_sections]


def detect_topics(record: dict[str, Any]) -> list[str]:
    blob = _blob(record)
    return [topic for topic, terms in TOPIC_TERMS.items() if any(_norm(term) in blob for term in terms)]


def route_for(document_type: str, source_class: str, publisher_code: str | None) -> str:
    routing = _load_yaml("routing.yaml")
    values = {
        "document_type": document_type,
        "source_class": source_class,
        "publisher_code": publisher_code,
    }
    for rule in routing.get("routes") or []:
        match = rule.get("match") or {}
        if all(values.get(key) == value for key, value in match.items()):
            return str(rule["path"])
    return str(routing.get("fallback") or "90_STAGING/NEEDS_CLASSIFICATION")


def classify_record(record: dict[str, Any]) -> ClassificationResult:
    doc_type, doc_conf, doc_method = detect_document_type(record)
    source_class, publisher_code, source_conf = detect_source_class(record, doc_type)
    tier, priority = authority_for(source_class, doc_type)
    sections = score_sections(record)
    topics = detect_topics(record)

    primary = sections[0].id if sections else None
    section_conf = sections[0].score if sections else 0.0
    combined = round((doc_conf * 0.30) + (source_conf * 0.25) + (section_conf * 0.45), 4)

    if primary is None or doc_type == "UNKNOWN":
        status = "NEEDS_REVIEW"
    elif combined >= 0.90:
        status = "AUTO_ACCEPT"
    elif combined >= 0.75:
        status = "ACCEPT_WITH_AUDIT"
    elif combined >= 0.55:
        status = "LOCAL_LLM_REVIEW"
    else:
        status = "NEEDS_REVIEW"

    route = route_for(doc_type, source_class, publisher_code)
    return ClassificationResult(
        document_type=doc_type,
        source_class=source_class,
        authority_tier=tier,
        evidence_priority=priority,
        publisher_code=publisher_code,
        primary_section=primary,
        book_sections=[asdict(item) for item in sections],
        topics=topics,
        classification_confidence=combined,
        classification_status=status,
        route_path=route,
        methods={
            "document_type": doc_method,
            "source_class": "domain_and_metadata_rules",
            "sections": "taxonomy_keyword_rules",
            "topics": "controlled_vocabulary_rules",
        },
    )


def write_classification(record: dict[str, Any], destination: str | Path) -> Path:
    result = classify_record(record)
    dest = Path(destination)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(result.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return dest
