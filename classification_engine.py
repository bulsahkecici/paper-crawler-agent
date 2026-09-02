#!/usr/bin/env python3
"""Book-agnostic deterministic classification for tunnel-engineering sources."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT / "config"

DOCUMENT_TYPES = {
    "OFFICIAL_REGULATION", "TECHNICAL_STANDARD", "TECHNICAL_GUIDELINE", "TECHNICAL_MANUAL",
    "TECHNICAL_SPECIFICATION", "INVENTORY", "STATISTICAL_REPORT", "COST_REPORT",
    "ACCIDENT_REPORT", "SAFETY_REPORT", "JOURNAL_ARTICLE", "REVIEW_ARTICLE",
    "CONFERENCE_PAPER", "THESIS_PHD", "THESIS_MSC", "PREPRINT", "TECHNICAL_REPORT",
    "PROJECT_REPORT", "CASE_STUDY", "BOOK", "BOOK_CHAPTER", "NEWS", "WEB_PAGE",
    "PRESENTATION", "OFFICIAL_PRESENTATION", "ACADEMIC_PRESENTATION",
    "CONFERENCE_PRESENTATION", "TECHNICAL_PRESENTATION", "DATASET", "UNKNOWN",
}

TOPIC_TERMS: dict[str, tuple[str, ...]] = {
    "tunnel_definition": ("tunnel definition",), "tunnel_history": ("tunnel history", "history of tunnelling", "tünel tarih"),
    "tunnel_classification": ("tunnel classification", "tünel sınıflandır"),
    "road_tunnel": ("road tunnel", "highway tunnel", "karayolu tüneli", "wegtunnel", "straßentunnel", "tunnel routier", "vegtunnel"),
    "rail_tunnel": ("rail tunnel", "railway tunnel"), "metro_tunnel": ("metro tunnel", "subway tunnel"),
    "underwater_tunnel": ("underwater tunnel", "subaqueous tunnel"), "immersed_tube": ("immersed tube", "immersed tunnel"),
    "NATM": ("natm", "new austrian tunnelling", "new austrian tunneling"), "TBM": ("tbm", "tunnel boring machine"),
    "EPB": ("epb", "earth pressure balance"), "shield_tunnelling": ("shield tunnelling", "shield tunneling", "shield tunnel"),
    "drill_and_blast": ("drill and blast", "drill-and-blast"), "cut_and_cover": ("cut and cover", "cut-and-cover"),
    "rock_tunnel": ("rock tunnel", "hard rock tunnel"), "soft_ground": ("soft ground tunnel", "soft soil tunnel"),
    "geology": ("geology", "geological", "jeoloji"), "engineering_geology": ("engineering geology",),
    "geotechnics": ("geotechnical", "geotechnics", "jeoteknik", "geoteknik"), "site_investigation": ("site investigation", "ground investigation"),
    "route_selection": ("route selection", "güzergah seç"), "alignment": ("tunnel alignment",),
    "excavation": ("tunnel excavation", "excavation performance"), "shotcrete": ("shotcrete", "sprayed concrete", "püskürtme beton"),
    "rock_bolt": ("rock bolt", "rockbolt", "kaya bulonu"), "steel_rib": ("steel rib", "steel set"),
    "segmental_lining": ("segmental lining", "segment lining", "tunnel segment"), "final_lining": ("final lining", "secondary lining"),
    "waterproofing": ("waterproofing", "waterproof", "su yalıt"), "drainage": ("tunnel drainage",),
    "monitoring": ("tunnel monitoring", "structural health monitoring"), "instrumentation": ("instrumentation", "inclinometer", "extensometer"),
    "deformation": ("tunnel deformation", "lining deformation"), "settlement": ("tunnel settlement", "ground settlement"),
    "structural_defect": ("structural defect", "lining defect", "lining crack"), "inspection": ("tunnel inspection", "lining inspection"),
    "construction_management": ("construction management",), "construction_risk": ("construction risk", "tunnelling risk"),
    "construction_cost": ("tunnel construction cost", "yapım maliyeti", "tunnel capex"), "cost_estimation": ("cost estimation", "cost estimate"),
    "cost_driver": ("cost driver", "cost overrun", "cost factor"), "unit_cost": ("unit cost", "unit price", "birim fiyat"),
    "life_cycle_cost": ("life cycle cost", "lifecycle cost", "whole life cost", "yaşam döngüsü"),
    "maintenance": ("tunnel maintenance", "tünel bakım"), "structural_maintenance": ("structural maintenance", "lining repair"),
    "electromechanical_maintenance": ("electromechanical maintenance", "electro-mechanical maintenance"),
    "periodic_maintenance": ("periodic maintenance", "scheduled maintenance", "periyodik bakım"),
    "rehabilitation": ("tunnel rehabilitation", "tunnel renovation"), "operation": ("tunnel operation", "tünel işletme"),
    "operation_cost": ("operation cost", "operating cost", "tunnel opex"), "maintenance_cost": ("maintenance cost", "bakım maliyeti"),
    "energy": ("tunnel energy", "energy consumption", "energy optimization", "electricity cost"), "energy_efficiency": ("energy efficiency", "energy saving"),
    "ventilation": ("tunnel ventilation", "havalandırma"), "jet_fan": ("jet fan", "impulse fan"),
    "lighting": ("tunnel lighting", "aydınlatma"), "electrical_system": ("tunnel electrical", "electrical system"),
    "SCADA": ("scada",), "ITS": ("intelligent transport system",), "fire": ("tunnel fire",),
    "fire_safety": ("fire safety", "yangın güven"), "fire_detection": ("fire detection",), "fire_suppression": ("fire suppression",),
    "emergency_response": ("emergency response", "emergency plan"), "evacuation": ("tunnel evacuation",),
    "traffic_management": ("traffic management",), "road_safety": ("road safety", "tunnel safety"),
    "accident": ("tunnel accident", "kaza"), "disaster": ("tunnel disaster", "earthquake tunnel"),
    "asset_management": ("asset management",), "sustainability": ("sustainability", "sustainable tunnel"),
    "Turkey": ("turkey", "türkiye", "turkish"), "KGM": ("kgm", "karayolları genel müdürlüğü"),
}

DOC_TYPE_RULES = [
    ("OFFICIAL_REGULATION", ("regulation", "directive", "yönetmelik", "resmî gazete")),
    ("TECHNICAL_SPECIFICATION", ("technical specification", "teknik şartname", "standard specification")),
    ("TECHNICAL_STANDARD", ("tunnel standard", "dmrb", "n500")), ("TECHNICAL_MANUAL", ("manual", "handbook")),
    ("TECHNICAL_GUIDELINE", ("guideline", "design guide", "rehber", "yönerge")),
    ("SAFETY_REPORT", ("safety report", "safety assessment")), ("ACCIDENT_REPORT", ("accident investigation", "fire investigation")),
    ("COST_REPORT", ("cost report", "unit price", "birim fiyat")), ("STATISTICAL_REPORT", ("statistical report", "statistics", "istatistik")),
    ("INVENTORY", ("inventory", "envanter")), ("REVIEW_ARTICLE", ("systematic review", "literature review", "review article")),
    ("THESIS_PHD", ("doctoral thesis", "phd thesis", "doktora tezi")), ("THESIS_MSC", ("master's thesis", "masters thesis", "yüksek lisans tezi")),
    ("CONFERENCE_PRESENTATION", ("conference presentation", "conference slides", "symposium presentation")),
    ("CONFERENCE_PAPER", ("conference paper", "proceedings paper")), ("ACADEMIC_PRESENTATION", ("university presentation", "lecture slides")),
    ("PRESENTATION", ("presentation", "slide deck", "slides")), ("CASE_STUDY", ("case study",)),
    ("PROJECT_REPORT", ("project report",)), ("BOOK_CHAPTER", ("book chapter",)), ("PREPRINT", ("preprint", "arxiv")),
    ("DATASET", ("dataset", "data set")), ("NEWS", ("news", "press release", "haber")),
]

@dataclass
class ClassificationResult:
    document_type: str
    source_class: str
    authority_tier: str
    evidence_priority: int
    publisher_code: str | None
    producer: dict[str, Any]
    topics: list[str]
    classification_confidence: float
    classification_status: str
    route_path: str
    methods: dict[str, str]
    schema_version: str = "3.0"
    def as_dict(self) -> dict[str, Any]: return asdict(self)

@lru_cache(maxsize=8)
def _load_yaml(name: str) -> dict[str, Any]:
    payload = yaml.safe_load((CONFIG_DIR / name).read_text(encoding="utf-8")) or {}
    return payload if isinstance(payload, dict) else {}

def _norm(value: Any) -> str: return re.sub(r"\s+", " ", str(value or "")).strip().casefold()

def _blob(record: dict[str, Any]) -> str:
    keys = ("title", "abstract", "classification_text", "keywords", "venue", "publisher", "source", "description", "conference", "organization", "landing_url", "source_url")
    return _norm(" ".join(" ".join(map(str, record[k])) if isinstance(record.get(k), list) else str(record.get(k) or "") for k in keys))

def _hostname(record: dict[str, Any]) -> str:
    for key in ("original_url", "source_url", "landing_url", "pdf_url"):
        host = urlparse(str(record.get(key) or "")).hostname
        if host: return host.casefold()
    return ""

@lru_cache(maxsize=1)
def _institution_registry() -> tuple[dict[str, Any], ...]: return tuple(_load_yaml("institutional_sources.yaml").get("institutions") or [])

def _institution(record: dict[str, Any]) -> dict[str, Any] | None:
    host, blob = _hostname(record), _blob(record)
    for item in _institution_registry():
        domains = [str(x).casefold().lstrip(".") for x in item.get("domains") or []]
        aliases = [str(item.get("id") or "").casefold(), str(item.get("name") or "").casefold(), *[str(x).casefold() for x in item.get("aliases") or []]]
        if any(host == d or host.endswith("." + d) for d in domains) or any(a and re.search(rf"(?<!\w){re.escape(a)}(?!\w)", blob) for a in aliases): return item
    return None

def detect_document_type(record: dict[str, Any]) -> tuple[str, float, str]:
    explicit = str(record.get("document_type") or "").upper()
    if explicit in DOCUMENT_TYPES and explicit != "UNKNOWN": return explicit, 1.0, "explicit_metadata"
    source, venue, title, blob = _norm(record.get("source")), _norm(record.get("venue")), _norm(record.get("title")), _blob(record)
    ext = Path(urlparse(str(record.get("source_url") or record.get("landing_url") or "")).path).suffix.casefold()
    if ext in {".ppt", ".pptx"}: return ("OFFICIAL_PRESENTATION" if _institution(record) else "PRESENTATION"), .96, "file_extension"
    if source == "arxiv" or "arxiv" in venue: return "PREPRINT", .99, "source_metadata"
    if source in {"openalex", "crossref", "doaj", "europe_pmc", "semantic_scholar", "core", "openaire"}:
        return ("REVIEW_ARTICLE" if any(x in blob for x in ("systematic review", "literature review", "review article")) else "JOURNAL_ARTICLE"), .92, "academic_source_metadata"
    degree = _norm(record.get("degree") or record.get("thesis_level"))
    if any(x in degree for x in ("doctor", "phd", "doktora")): return "THESIS_PHD", .99, "degree_metadata"
    if "master" in degree or "yüksek lisans" in degree: return "THESIS_MSC", .99, "degree_metadata"
    for kind, terms in DOC_TYPE_RULES:
        matches = [x for x in terms if x in blob]
        if matches: return kind, (.90 if any(x in title for x in matches) else .76), "controlled_rules"
    if ext in {".xls", ".xlsx", ".csv"}: return "DATASET", .82, "file_extension"
    if record.get("doi"): return "JOURNAL_ARTICLE", .72, "doi_fallback"
    if _hostname(record): return "WEB_PAGE", .58, "web_fallback"
    return "UNKNOWN", .25, "insufficient_metadata"

def detect_source_class(record: dict[str, Any], document_type: str) -> tuple[str, str | None, float]:
    inst = _institution(record)
    if inst: return str(inst.get("source_class") or "UNKNOWN"), str(inst.get("id") or "") or None, .99
    host, source = _hostname(record), _norm(record.get("source"))
    if any(host == x or host.endswith("." + x) for x in ("slideshare.net", "slideserve.com", "speakerdeck.com")): return "PRESENTATION_PLATFORM", None, .98
    if host.endswith("zenodo.org") or host.endswith("figshare.com"): return "RESEARCH_REPOSITORY", None, .98
    if source in {"openalex", "crossref", "doaj", "europe_pmc", "semantic_scholar", "core", "openaire", "arxiv"}: return "ACADEMIC", None, .92
    if document_type in {"THESIS_PHD", "THESIS_MSC"}: return "UNIVERSITY_REPOSITORY", None, .88
    if document_type == "NEWS": return "NEWS", None, .72
    if document_type in {"PRESENTATION", "TECHNICAL_PRESENTATION"}: return "PRESENTATION_PLATFORM", None, .55
    return "UNKNOWN", None, .35

def authority_for(source_class: str, document_type: str, institution: dict[str, Any] | None = None) -> tuple[str, int]:
    if institution and institution.get("authority_tier"): tier = str(institution["authority_tier"])
    elif source_class == "TR_OFFICIAL": tier = "A1"
    elif source_class in {"FOREIGN_GOVERNMENT", "ROAD_AUTHORITY", "TRANSPORT_AUTHORITY"}: tier = "A2"
    elif source_class == "INTERNATIONAL_OFFICIAL": tier = "A3"
    elif source_class == "STANDARD_BODY": tier = "A4"
    elif document_type == "REVIEW_ARTICLE": tier = "B1"
    elif document_type == "JOURNAL_ARTICLE": tier = "B2"
    elif document_type == "THESIS_PHD": tier = "C1"
    elif document_type == "THESIS_MSC": tier = "C2"
    elif source_class == "PROFESSIONAL_ORGANIZATION": tier = "D1"
    elif document_type in {"CONFERENCE_PAPER", "CONFERENCE_PRESENTATION"}: tier = "D2"
    elif document_type in {"TECHNICAL_REPORT", "PROJECT_REPORT", "CASE_STUDY"}: tier = "D3"
    elif document_type == "OFFICIAL_PRESENTATION": tier = "E1"
    elif document_type == "ACADEMIC_PRESENTATION": tier = "E2"
    elif document_type in {"PRESENTATION", "TECHNICAL_PRESENTATION"}: tier = "G"
    elif source_class in {"NEWS", "COMMERCIAL_TECHNICAL"}: tier = "F"
    else: tier = "G"
    priorities = {"A1":100,"A2":95,"A3":90,"A4":88,"B1":85,"B2":80,"C1":70,"C2":60,"D1":75,"D2":58,"D3":55,"E1":65,"E2":52,"E3":45,"F":30,"G":10,"X":0}
    return tier, priorities.get(tier, 0)

def detect_topics(record: dict[str, Any]) -> list[str]:
    blob = f" {_blob(record)} "
    return [topic for topic, terms in TOPIC_TERMS.items() if any(_norm(term) in blob for term in terms)]

def producer_for(record: dict[str, Any], institution: dict[str, Any] | None) -> dict[str, Any]:
    name = record.get("actual_producer") or record.get("organization") or (institution or {}).get("name") or record.get("publisher")
    return {"name": name, "country": (institution or {}).get("country") or record.get("country"), "institution_type": (institution or {}).get("source_class") or record.get("institution_type"), "attribution_status": "RESOLVED" if name else "UNKNOWN"}

def route_for(document_type: str, source_class: str, publisher_code: str | None) -> str:
    if "PRESENTATION" in document_type: return "E_PRESENTATIONS"
    if source_class in {"TR_OFFICIAL", "FOREIGN_GOVERNMENT", "ROAD_AUTHORITY", "TRANSPORT_AUTHORITY"}: return "A_OFFICIAL"
    if source_class in {"INTERNATIONAL_OFFICIAL", "STANDARD_BODY", "PROFESSIONAL_ORGANIZATION"}: return "B_INTERNATIONAL_TECHNICAL"
    if source_class in {"ACADEMIC", "UNIVERSITY_REPOSITORY", "RESEARCH_REPOSITORY"}: return "C_ACADEMIC"
    return "90_STAGING/NEEDS_CLASSIFICATION"

def migrate_legacy_fields(record: dict[str, Any]) -> dict[str, Any]:
    out, legacy = dict(record), dict(record.get("legacy") or {})
    for key in ("primary_section", "book_sections", "section_confidence", "provisional_primary_section", "provisional_secondary_sections", "provisional_section_confidence"):
        if key in out: legacy.setdefault(key, out.pop(key))
    if legacy: out["legacy"] = legacy
    return out

def classify_record(record: dict[str, Any]) -> ClassificationResult:
    document_type, doc_conf, doc_method = detect_document_type(record)
    source_class, code, source_conf = detect_source_class(record, document_type)
    institution = _institution(record)
    tier, priority = authority_for(source_class, document_type, institution)
    topics = detect_topics(record)
    relevance = str(record.get("relevance_status") or "").upper()
    relevance_conf = float(record.get("tunnel_relevance_score") or record.get("relevance_score") or 0.0)
    combined = round(.34 * doc_conf + .33 * source_conf + .33 * relevance_conf, 4)
    ambiguous = document_type == "UNKNOWN" or source_class == "UNKNOWN" or relevance in {"", "WEAK"}
    status = "REJECT_IRRELEVANT" if relevance == "IRRELEVANT" else ("LOCAL_LLM_REVIEW" if ambiguous else ("AUTO_ACCEPT" if combined >= .78 else "ACCEPT_WITH_AUDIT"))
    return ClassificationResult(document_type, source_class, tier, priority, code, producer_for(record, institution), topics, combined, status, route_for(document_type, source_class, code), {"document_type": doc_method, "source_class": "institution_registry_and_metadata", "authority": "deterministic_source_identity", "topics": "controlled_vocabulary_rules"})

def write_classification(record: dict[str, Any], destination: str | Path) -> Path:
    dest = Path(destination); dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(classify_record(record).as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return dest
