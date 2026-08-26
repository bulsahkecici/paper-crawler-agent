#!/usr/bin/env python3
"""Deterministic multi-source harvest of open-access tunnel-engineering papers.

Internet MAY be used only for academic discovery and OA PDF download:
  OpenAlex, Europe PMC, DOAJ, Crossref, Unpaywall, PMC, optional Semantic Scholar.
No harvested PDF bytes or TunnelBookAI corpus content are sent to a cloud LLM
from this module. Literature notes (if any) are produced locally by
paper_crawler_agent.py against a loopback-only model.

This is a staging / discovery subsystem. Papers stay corpus_status=STAGING.
They are never ingested into the TunnelBookAI corpus from here.

Layout:
  tunel_makaleleri/pdfs/*.pdf
  tunel_makaleleri/metadata/*.meta.json
  tunel_makaleleri/literature_notes/*.summary.md
  tunel_makaleleri/rejected/
  tunel_makaleleri/catalog.json
  tunel_makaleleri/index.jsonl
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import requests

OUTPUT_DIR = Path(os.getenv("TUNNEL_PAPERS_DIR", "./tunel_makaleleri")).resolve()
PDF_DIR = OUTPUT_DIR / "pdfs"
METADATA_DIR = OUTPUT_DIR / "metadata"
LITERATURE_NOTES_DIR = OUTPUT_DIR / "literature_notes"
REJECTED_DIR = OUTPUT_DIR / "rejected"
# Backward-compatible aliases (old markdown/json names).
MD_DIR = LITERATURE_NOTES_DIR
JSON_DIR = METADATA_DIR

CORPUS_STAGING = "STAGING"
CORPUS_READY = "READY_FOR_INGEST"
CORPUS_INGESTED = "INGESTED"
CORPUS_REJECTED = "REJECTED"
ALLOWED_CORPUS_STATUS = {CORPUS_STAGING, CORPUS_READY, CORPUS_INGESTED, CORPUS_REJECTED}
MAILTO = os.getenv("OPENALEX_MAILTO", "tunnel-crawler@localhost")
REQUEST_TIMEOUT = 30
MAX_PDF_BYTES = 80 * 1024 * 1024
DEFAULT_LIMIT = 800
MAX_LIMIT = 1000
MIN_SCORE = 4

USER_AGENT = (
    "TunnelPaperCrawler/3.0 "
    f"(mailto:{MAILTO}; research harvest for NATM/TBM/SHM)"
)
JSON_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}
PDF_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
}

DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
BLOCKED_HOSTS = (
    "researchgate.net",
    "academia.edu",
    "sciencedirect.com",
    "onlinelibrary.wiley.com",
    "ieeexplore.ieee.org",
    "ascelibrary.org",
)

TUNNEL_RE = re.compile(
    r"\b(tunnell?ings?|tunnels?|tünel(?:ler|cilik)?|túnel(?:es)?|t[uú]neis)\b",
    re.I,
)
METHOD_RE = re.compile(
    r"\b(natm|new austrian|\btbm\b|\bepb\b|tunnel boring|shield[- ]?tunn|"
    r"sequential excavation|rock mass|shotcrete|segmental lining|"
    r"geotechnical|excavation method|disc cutter|rock ?bolts?|"
    r"cutterhead|slurry shield)\b",
    re.I,
)
GEO_RE = re.compile(
    r"(?i)(?:"
    r"\bnatm\b|\bnew austrian|\btbm\b|\bepb\b|tunnel boring|shield[- ]?tunn|"
    r"sequential excavation|rock mass|shotcrete|segmental|"
    r"\bgeotechnical\b|disc cutter|rock ?bolts?|cutterhead|"
    r"tunnel.?linings?|lining cracks?|lining defects?|lining inspect|"
    r"\bmetro\b|\bsubway\b|underground construction|tunnel support|"
    r"tunnel face|ground settlement|surface settlement|ground movement|"
    r"\bsqueezing\b|\brockburst\b|forepoling|pipe roof|overburden|"
    r"drill and blast|bored tunnels?|tunnel construct|tunnel excav|"
    r"tunnel deform|tunnel monitor|tunnel inspect|utility tunnels?|"
    r"rock tunnels?|shallow tunnels?|deep.{0,16}tunnels?|"
    r"in-service.{0,24}tunnels?|shield tunnels?|"
    r"tunnell?.{0,24}cracks?|cracks?.{0,24}tunnell?|"
    r"tunnel shm|structural health.{0,60}tunnell?|"
    r"tünel|túnel|escava[cç][aã]o|tuneladora|"
    r"surrounding rock|face stability|support systems?|mechanized tunn|"
    r"defect detect|highway tunnels?|pipe jacking|mine tunnels?|"
    r"soft ground|mega tunnels?|cross-river|water stain|"
    r"construction delay|loess tunnels?|secondary lining|volume loss|"
    r"\bebpm\b|blast vibration|water pressure tunnel|pre-reinforcement|"
    r"tunneling machine|tunnelling machine|"
    r"tunnel invert|shield construction|pile foundation|"
    r"tunnel defects?|railway tunnels?|point clouds?|"
    r"induced by tunn|interchange tunnels?|dual tunnels?|"
    r"small diameter tunnels?|conventional tunnels?|"
    r"underwater tunnel|tunnel cavity|tunnel behavior|"
    r"during construction|"
    r"highway tunnels?|road tunnels?|karayolu|"
    r"immersed (?:tube )?tunnels?|underwater tunnels?|"
    r"life[- ]cycle cost|construction cost|unit cost|"
    r"operation and maintenance|\bo&m\b|maintenance cost|"
    r"electromechanical|ventilation|lighting|energy consumption|"
    r"fire safety|tunnel fire|tunnel accident|"
    r"yapım maliyeti|bakım[- ]işletme|yaşam döngüsü|"
    r"\bsettlement\b|\bexcavation\b|\binspection\b|\bmonitoring\b"
    r")"
)
COST_RE = re.compile(
    r"\b(life[- ]cycle cost|\blcc\b|construction cost|unit cost|"
    r"cost estimation|cost overrun|tender cost|bill of quantit|"
    r"operation(?:al)? cost|maintenance cost|o&m cost|"
    r"energy cost|yapım maliyeti|işletme maliyeti|bakım maliyeti|"
    r"yaşam döngüsü maliyeti)\b",
    re.I,
)
OAM_RE = re.compile(
    r"\b(operation and maintenance|\bo&m\b|periodic maintenance|"
    r"electromechanical|scada|tunnel ventilation|tunnel lighting|"
    r"energy consumption|energy efficiency|fire detection|"
    r"tunnel fire|road tunnel safety|PIARC|"
    r"bakım[- ]onarım|işletme maliyeti|havalandırma|aydınlatma)\b",
    re.I,
)
HIGHWAY_RE = re.compile(
    r"\b(highway tunnel|road tunnel|motorway tunnel|karayolu tünel|"
    r"state road|provincial road|KGM|general directorate of highways|"
    r"immersed tube|underwater tunnel)\b",
    re.I,
)
SHM_RE = re.compile(
    r"\b(structural health|\bshm\b|crack detection|lining inspection|"
    r"lining defect|damage detection|condition assessment|"
    r"non-destructive|\bndt\b|fiber optic|fibre optic)\b",
    re.I,
)
OFFTOPIC_RE = re.compile(
    r"\b(aircraft|airplane|aero(?:nautic|space)|wind turbine|wind tunnel|"
    r"escherichia|cyanophycin|sars-cov|covid-19|photosynthesis|"
    r"crop yield|agriculture|matrix-variate|ssh tunnel|vpn|"
    r"tcp tunnel|http tunnel|network tunnel|dns tunnel|carpal tunnel|"
    r"tunnel diode|quantum tunn|scanning tunnelling|electron tunn|"
    r"tunnel junction|tunnel fet|magnetic tunnel|magnetoresistance|"
    r"antiferromagnetic|spintronic|black hole|fermion|phonon|"
    r"higgs|lhc|collider|unknotting|knot tunnel|fibered links|"
    r"monodromy|ribosomal|exit tunnel|mesothelioma|pheromone|"
    r"virtual try-on|try-on|reconfigurable intelligent|"
    r"blocking probability|kiln|de sitter|ga[s]?sb|"
    r"malicious dns|conductance-slope)\b",
    re.I,
)

# Queries follow the book outline (Çalışma Kapsamı Tasarısı):
# history & highway tunnels, construction methods, geotech/design,
# construction cost / LCC, O&M / energy / electromechanical, accidents.
SEARCH_QUERIES = (
    "NATM tunnel support shotcrete rock",
    "TBM highway tunnel construction",
    "soft ground tunnelling EPB shield",
    "immersed tube tunnel construction",
    "underwater tunnel construction method",
    "rock tunnel excavation support design",
    "tunnel geotechnical investigation design",
    "highway tunnel alignment route selection",
    "tunnel lining segmental support NATM",
    "highway road tunnel design PIARC",
    "road tunnel fire safety ventilation",
    "highway tunnel accident fire case",
    "karayolu tüneli yapım",
    "karayolu tüneli bakım işletme",
    "tunnel construction cost estimation",
    "road tunnel life cycle cost",
    "tunnel unit cost highway construction",
    "tünel yapım maliyeti",
    "tünel yaşam döngüsü maliyeti",
    "road tunnel operation maintenance cost",
    "highway tunnel ventilation energy consumption",
    "tunnel lighting energy efficiency",
    "tunnel electromechanical systems maintenance",
    "tünel bakım onarım işletme maliyeti",
    "tünel havalandırma enerji tüketimi",
    "tunnel structural maintenance lining",
    "periodic inspection highway tunnel",
)

FILL_QUERIES = (
    "New Austrian Tunnelling Method highway",
    "shield tunnel construction cost",
    "NATM vs TBM cost comparison",
    "tunnel boring machine road tunnel",
    "immersed tunnel cost construction",
    "tunnel ventilation jet fan energy",
    "road tunnel lighting LED energy",
    "tunnel fire detection suppression",
    "Mont Blanc Tauern tunnel fire",
    "tunnel operation and maintenance O&M",
    "life cycle costing underground infrastructure",
    "geotechnical investigation tunnel alignment",
    "soft ground tunnel construction method",
    "shotcrete rock bolt tunnel support",
    "highway tunnel case study Turkey",
    "tünel destek elemanları NATM",
    "tünel işletme maliyeti enerji",
    "tunnel disaster accident safety",
)

SESSION = requests.Session()
SESSION.headers.update(JSON_HEADERS)

_index_lock = threading.Lock()
_stem_lock = threading.Lock()
_used_stems: set[str] = set()


@dataclass
class Paper:
    title: str
    source: str
    authors: list[str] = field(default_factory=list)
    year: str | None = None
    abstract: str = ""
    venue: str = ""
    doi: str | None = None
    pdf_url: str | None = None
    landing_url: str | None = None
    score: int = 0
    query: str = ""

    def __post_init__(self) -> None:
        self.title = _clean_text(self.title) or "untitled"
        self.abstract = _clean_text(self.abstract)
        self.venue = _clean_text(self.venue)

    def key(self) -> str:
        if self.doi:
            return "doi:" + self.doi.lower()
        return "title:" + re.sub(r"\W+", " ", (self.title or "").lower()).strip()


def _clean_text(value: str | None) -> str:
    text = html.unescape(value or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def set_output_dir(path: str | Path) -> Path:
    """Rebind staging directories (tests and TUNNEL_PAPERS_DIR)."""
    global OUTPUT_DIR, PDF_DIR, METADATA_DIR, LITERATURE_NOTES_DIR, REJECTED_DIR, MD_DIR, JSON_DIR
    OUTPUT_DIR = Path(path).resolve()
    PDF_DIR = OUTPUT_DIR / "pdfs"
    METADATA_DIR = OUTPUT_DIR / "metadata"
    LITERATURE_NOTES_DIR = OUTPUT_DIR / "literature_notes"
    REJECTED_DIR = OUTPUT_DIR / "rejected"
    MD_DIR = LITERATURE_NOTES_DIR
    JSON_DIR = METADATA_DIR
    return ensure_output_dir()


def catalog_path() -> Path:
    return OUTPUT_DIR / "catalog.json"


def index_path() -> Path:
    return OUTPUT_DIR / "index.jsonl"


def sidecar_paths(pdf_path: str | Path) -> tuple[Path, Path]:
    stem = Path(pdf_path).stem
    return METADATA_DIR / f"{stem}.meta.json", LITERATURE_NOTES_DIR / f"{stem}.summary.md"


def hash_pdf(path: str | Path) -> tuple[str, int]:
    """Stream SHA256 and byte size from a file on disk."""
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def ensure_output_dir() -> Path:
    for folder in (OUTPUT_DIR, PDF_DIR, METADATA_DIR, LITERATURE_NOTES_DIR, REJECTED_DIR):
        folder.mkdir(parents=True, exist_ok=True)
    migrate_staging_layout()
    migrate_flat_layout()
    return OUTPUT_DIR


def sanitize_filename(title: str, max_length: int = 100) -> str:
    cleaned = html.unescape(title or "untitled_paper")
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    cleaned = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "", cleaned)
    cleaned = re.sub(r"[&]+", "", cleaned)
    cleaned = re.sub(r"\s+", "_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("._")
    return (cleaned or "untitled_paper")[:max_length]


def _artifact_parts(name: str) -> tuple[str, str]:
    if name.endswith(".summary.md"):
        return name[: -len(".summary.md")], ".summary.md"
    if name.endswith(".meta.json"):
        return name[: -len(".meta.json")], ".meta.json"
    suffix = Path(name).suffix
    return Path(name).stem, suffix


def _move_file_safe(src: Path, dest: Path) -> Path:
    """Move src to dest. Never replace a larger/equal dest with a smaller src."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not src.exists():
        return dest
    if src.resolve() == dest.resolve():
        return dest
    if dest.exists():
        if dest.stat().st_size >= src.stat().st_size:
            return dest
        dest.unlink()
    src.replace(dest)
    return dest


def migrate_staging_layout() -> None:
    """Move json/→metadata/, markdown/→literature_notes/, catalog/index to staging root."""
    old_json = OUTPUT_DIR / "json"
    old_md = OUTPUT_DIR / "markdown"
    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    LITERATURE_NOTES_DIR.mkdir(parents=True, exist_ok=True)

    for legacy_catalog in (old_json / "catalog.json", METADATA_DIR / "catalog.json"):
        if legacy_catalog.exists() and legacy_catalog.resolve() != catalog_path().resolve():
            preferred = catalog_path()
            if preferred.exists():
                try:
                    old_n = len((json.loads(legacy_catalog.read_text(encoding="utf-8")).get("papers") or []))
                    new_n = len((json.loads(preferred.read_text(encoding="utf-8")).get("papers") or []))
                except (OSError, ValueError):
                    old_n, new_n = 0, 1
                if old_n > new_n:
                    _move_file_safe(legacy_catalog, preferred)
            else:
                _move_file_safe(legacy_catalog, preferred)

    for legacy_index in (old_json / "index.jsonl", METADATA_DIR / "index.jsonl"):
        if legacy_index.exists() and legacy_index.resolve() != index_path().resolve():
            if not index_path().exists() or index_path().stat().st_size < legacy_index.stat().st_size:
                _move_file_safe(legacy_index, index_path())

    for legacy_keys in (old_json / "rejected_keys.json", METADATA_DIR / "rejected_keys.json"):
        dest = OUTPUT_DIR / "rejected_keys.json"
        if legacy_keys.exists() and legacy_keys.resolve() != dest.resolve():
            if not dest.exists() or dest.stat().st_size < legacy_keys.stat().st_size:
                _move_file_safe(legacy_keys, dest)

    if old_json.is_dir():
        for path in list(old_json.iterdir()):
            if not path.is_file():
                continue
            if path.name in {"catalog.json", "index.jsonl", "rejected_keys.json"}:
                continue
            if path.name.endswith(".meta.json") or path.suffix == ".json":
                _move_file_safe(path, METADATA_DIR / path.name)
    if old_md.is_dir():
        for path in list(old_md.iterdir()):
            if path.is_file():
                _move_file_safe(path, LITERATURE_NOTES_DIR / path.name)

    for leftover_dir in (old_json, old_md):
        if leftover_dir.is_dir() and not any(leftover_dir.iterdir()):
            leftover_dir.rmdir()

    _rewrite_catalog_paths()


def migrate_flat_layout() -> None:
    """Move leftover files from the old flat folder into pdfs/literature_notes/metadata."""
    leftovers: list[Path] = []
    for path in OUTPUT_DIR.iterdir():
        if path.is_dir() or path.name in {".gitkeep", ".DS_Store", "catalog.json", "index.jsonl", "rejected_keys.json"}:
            continue
        leftovers.append(path)

    for path in leftovers:
        stem, suffix = _artifact_parts(path.name)
        new_stem = sanitize_filename(stem)
        if suffix == ".pdf":
            dest = PDF_DIR / f"{new_stem}.pdf"
        elif suffix == ".summary.md" or suffix == ".md":
            dest = LITERATURE_NOTES_DIR / (f"{new_stem}.summary.md" if suffix == ".summary.md" else f"{new_stem}.md")
        elif suffix in {".json", ".meta.json"}:
            dest = METADATA_DIR / f"{new_stem}.meta.json"
        else:
            continue
        _move_file_safe(path, dest)

    _rewrite_catalog_paths()


def _rewrite_path(value: str | None) -> str | None:
    if not value:
        return value
    path = Path(value)
    name = path.name
    stem, suffix = _artifact_parts(name)
    clean = sanitize_filename(stem)
    if suffix == ".pdf" or name.lower().endswith(".pdf"):
        return str(PDF_DIR / f"{clean}.pdf")
    if suffix == ".summary.md":
        return str(LITERATURE_NOTES_DIR / f"{clean}.summary.md")
    if suffix in {".json", ".meta.json"}:
        return str(METADATA_DIR / f"{clean}.meta.json")
    return str(path)


def _rewrite_catalog_paths() -> None:
    path = catalog_path()
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    changed = False
    for key in ("papers", "downloaded"):
        rows = data.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            for field_name in ("path", "pdf_path"):
                if row.get(field_name):
                    new = _rewrite_path(str(row[field_name]))
                    if new != row[field_name]:
                        row[field_name] = new
                        changed = True
    if isinstance(data.get("downloaded"), list) and not data.get("papers"):
        data["papers"] = data["downloaded"]
        changed = True
    if changed:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    for meta in METADATA_DIR.glob("*.meta.json"):
        try:
            payload = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        pdf = payload.get("pdf_path")
        if pdf:
            new = _rewrite_path(str(pdf))
            if new != pdf:
                payload["pdf_path"] = new
                meta.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_catalog() -> dict[str, Any]:
    ensure_output_dir()
    candidates = [
        catalog_path(),
        METADATA_DIR / "catalog.json",
        OUTPUT_DIR / "json" / "catalog.json",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            continue
    return {}


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", str(value).strip(), flags=re.I)
    match = DOI_RE.search(text)
    return match.group(0) if match else None


def _host_blocked(url: str) -> bool:
    host = urllib.parse.urlparse(url).netloc.lower()
    return any(blocked in host for blocked in BLOCKED_HOSTS)


def relevance_score(title: str, abstract: str = "") -> int:
    """Higher is better. 0 means drop.

    The word "tunnel" alone is not enough (DNS, quantum, knot theory, LHC).
    A paper must have underground-construction or tunnel-SHM context.
    """
    title = title or ""
    blob = f"{title} {abstract or ''}"
    if OFFTOPIC_RE.search(title):
        return 0
    if OFFTOPIC_RE.search(blob) and not GEO_RE.search(title):
        return 0
    if not (GEO_RE.search(blob) or METHOD_RE.search(title)):
        return 0
    if not (TUNNEL_RE.search(blob) or METHOD_RE.search(blob)):
        return 0

    score = 0
    if TUNNEL_RE.search(title):
        score += 3
    elif TUNNEL_RE.search(blob):
        score += 1
    if METHOD_RE.search(title):
        score += 3
    elif METHOD_RE.search(blob):
        score += 1
    if GEO_RE.search(title):
        score += 2
    if COST_RE.search(title):
        score += 4
    elif COST_RE.search(blob):
        score += 2
    if OAM_RE.search(title):
        score += 4
    elif OAM_RE.search(blob):
        score += 2
    if HIGHWAY_RE.search(title):
        score += 3
    elif HIGHWAY_RE.search(blob):
        score += 1
    if SHM_RE.search(title):
        score += 2
    elif SHM_RE.search(blob):
        score += 1
    if re.search(r"\b(deep learning|yolo|unet)\b", title, re.I) and not HIGHWAY_RE.search(blob) and not COST_RE.search(blob):
        score -= 2
    if re.search(r"\bcrack", title, re.I) and not TUNNEL_RE.search(title) and not re.search(
        r"\blining\b", title, re.I
    ):
        score -= 3
    return score if score >= MIN_SCORE else 0


def _get_json(url: str, params: dict[str, Any] | None = None, timeout: int = REQUEST_TIMEOUT) -> dict[str, Any]:
    response = SESSION.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object")
    return payload


def _openalex_abstract(inverted: dict[str, Any] | None) -> str:
    if not inverted:
        return ""
    pairs: list[tuple[int, str]] = []
    for word, positions in inverted.items():
        for pos in positions or []:
            pairs.append((int(pos), str(word)))
    pairs.sort()
    return " ".join(word for _, word in pairs)


def _strip_xml(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "")


def _paper_key_from_record(row: dict[str, Any]) -> str:
    doi = normalize_doi(row.get("doi"))
    if doi:
        return "doi:" + doi.lower()
    title = _clean_text(str(row.get("title") or ""))
    return "title:" + re.sub(r"\W+", " ", title.lower()).strip()


def load_existing_sha256s(rows: list[dict[str, Any]] | None = None) -> dict[str, str]:
    """Map source_sha256 → local PDF path for exact-duplicate detection."""
    mapping: dict[str, str] = {}
    for row in rows or load_existing_papers():
        sha = row.get("source_sha256")
        pdf = row.get("local_pdf_path") or row.get("path") or row.get("pdf_path")
        if sha and pdf:
            mapping[str(sha)] = str(pdf)
            continue
        if pdf and Path(str(pdf)).exists():
            digest, _ = hash_pdf(pdf)
            mapping[digest] = str(pdf)
    return mapping


def load_existing_papers() -> list[dict[str, Any]]:
    ensure_output_dir()
    by_key: dict[str, dict[str, Any]] = {}
    catalog = load_catalog()
    for row in catalog.get("papers") or catalog.get("downloaded") or []:
        if not isinstance(row, dict) or not (row.get("path") or row.get("pdf_path")):
            continue
        if relevance_score(str(row.get("title") or ""), str(row.get("abstract") or "")) <= 0:
            continue
        by_key[_paper_key_from_record(row)] = row
    for meta in METADATA_DIR.glob("*.meta.json"):
        try:
            payload = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        pdf = payload.get("pdf_path") or payload.get("path")
        if not pdf or not Path(str(pdf)).exists():
            stem = meta.name[: -len(".meta.json")]
            candidate = PDF_DIR / f"{stem}.pdf"
            if candidate.exists():
                payload["pdf_path"] = str(candidate)
                payload["path"] = str(candidate)
                pdf = str(candidate)
            else:
                continue
        payload.setdefault("path", pdf)
        if relevance_score(str(payload.get("title") or ""), str(payload.get("abstract") or "")) <= 0:
            continue
        key = _paper_key_from_record(payload)
        previous = by_key.get(key)
        if previous is None:
            by_key[key] = payload
    return list(by_key.values())


def rejected_keys_path() -> Path:
    return OUTPUT_DIR / "rejected_keys.json"


def load_rejected_keys() -> set[str]:
    for path in (rejected_keys_path(), METADATA_DIR / "rejected_keys.json", OUTPUT_DIR / "json" / "rejected_keys.json"):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return set(data if isinstance(data, list) else data.get("keys") or [])
        except (OSError, ValueError):
            continue
    return set()


def update_literature_note_flags(pdf_path: str, sha256: str | None = None) -> None:
    """Mark metadata after a Qwen literature note is written. Never evidence-eligible."""
    meta_path, _ = sidecar_paths(pdf_path)
    payload: dict[str, Any] = {}
    if meta_path.exists():
        try:
            loaded = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
        except (OSError, ValueError):
            payload = {}
    payload["ai_generated_literature_note"] = True
    payload["evidence_eligible"] = False
    if sha256:
        payload["source_sha256"] = sha256
    payload.setdefault("corpus_status", CORPUS_STAGING)
    payload.setdefault("local_pdf_path", pdf_path)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def save_rejected_keys(keys: set[str]) -> None:
    rejected_keys_path().write_text(
        json.dumps(sorted(keys), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _relocate_rejected(row: dict[str, Any]) -> None:
    pdf = row.get("path") or row.get("pdf_path")
    if not pdf:
        return
    pdf_path = Path(str(pdf))
    stem = pdf_path.stem
    for src in (pdf_path, *sidecar_paths(pdf_path)):
        if not src.exists() or REJECTED_DIR in src.parents:
            continue
        dest = REJECTED_DIR / src.name
        if dest.exists():
            src.unlink(missing_ok=True)
            continue
        src.replace(dest)


def purge_offtopic() -> tuple[list[dict[str, Any]], int]:
    """Drop physics/CS/biology 'tunnel' papers from the live catalog."""
    ensure_output_dir()
    rejected = load_rejected_keys()
    kept: list[dict[str, Any]] = []
    removed = 0
    seen: set[str] = set()
    catalog = load_catalog()
    rows = list(catalog.get("papers") or catalog.get("downloaded") or [])
    for meta in METADATA_DIR.glob("*.meta.json"):
        try:
            payload = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = _paper_key_from_record(row)
        if key in seen:
            continue
        seen.add(key)
        title = str(row.get("title") or "")
        abstract = str(row.get("abstract") or "")
        if not abstract:
            pdf = row.get("path") or row.get("pdf_path")
            if pdf:
                meta, _ = sidecar_paths(pdf)
                if meta.exists():
                    try:
                        abstract = str(json.loads(meta.read_text(encoding="utf-8")).get("abstract") or "")
                    except (OSError, ValueError):
                        abstract = ""
        if relevance_score(title, abstract) <= 0:
            row["corpus_status"] = CORPUS_REJECTED
            rejected.add(key)
            _relocate_rejected(row)
            removed += 1
            continue
        row.setdefault("path", row.get("pdf_path"))
        kept.append(row)
    save_rejected_keys(rejected)
    _save_catalog(kept, {"purged": removed, "kept": len(kept)})
    if removed:
        print(f"Purged {removed} off-topic papers → {REJECTED_DIR}  (kept {len(kept)})")
    return kept, removed


def search_openalex(
    query: str,
    limit: int = 50,
    from_date: str = "1995-01-01",
    to_date: str | None = None,
) -> list[Paper]:
    papers: list[Paper] = []
    cursor = "*"
    while len(papers) < limit:
        page = min(50, limit - len(papers))
        payload = _get_json(
            "https://api.openalex.org/works",
            {
                "search": query,
                "filter": (
                    "open_access.is_oa:true,type:article,"
                    f"from_publication_date:{from_date}"
                    + (f",to_publication_date:{to_date}" if to_date else "")
                ),
                "per_page": page,
                "cursor": cursor,
                "mailto": MAILTO,
            },
        )
        results = payload.get("results") or []
        if not results:
            break
        for work in results:
            loc = work.get("best_oa_location") or work.get("primary_location") or {}
            pdf_url = loc.get("pdf_url") or (work.get("open_access") or {}).get("oa_url")
            if pdf_url and "mdpi.com" in str(pdf_url).lower():
                for extra in work.get("locations") or []:
                    extra_pdf = extra.get("pdf_url")
                    if extra_pdf and "mdpi.com" not in extra_pdf.lower() and not _host_blocked(extra_pdf):
                        pdf_url = extra_pdf
                        break
            authors = [
                (a.get("author") or {}).get("display_name")
                for a in (work.get("authorships") or [])
                if (a.get("author") or {}).get("display_name")
            ]
            papers.append(
                Paper(
                    title=work.get("display_name") or "untitled",
                    source="openalex",
                    authors=authors,
                    year=str(work.get("publication_year") or "") or None,
                    abstract=_openalex_abstract(work.get("abstract_inverted_index")),
                    venue=((loc.get("source") or {}).get("display_name") or ""),
                    doi=normalize_doi(work.get("doi")),
                    pdf_url=pdf_url,
                    landing_url=loc.get("landing_page_url"),
                    query=query,
                )
            )
        cursor = (payload.get("meta") or {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(0.12)
    return papers


def search_europe_pmc(query: str, limit: int = 50) -> list[Paper]:
    papers: list[Paper] = []
    cursor = "*"
    epmc_q = f"({query}) AND OPEN_ACCESS:Y AND HAS_FT:Y"
    while len(papers) < limit:
        page = min(100, limit - len(papers))
        payload = _get_json(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            {
                "query": epmc_q,
                "format": "json",
                "resultType": "core",
                "pageSize": page,
                "cursorMark": cursor,
            },
        )
        hits = (payload.get("resultList") or {}).get("result") or []
        if not hits:
            break
        for hit in hits:
            pdf_url = None
            for link in (hit.get("fullTextUrlList") or {}).get("fullTextUrl") or []:
                if link.get("availabilityCode") == "OA" and str(link.get("documentStyle") or "").lower() == "pdf":
                    pdf_url = link.get("url")
                    break
            if not pdf_url and hit.get("pmcid"):
                pdf_url = f"https://europepmc.org/articles/{hit['pmcid']}?pdf=render"
            authors = []
            for item in (hit.get("authorList") or {}).get("author") or []:
                name = item.get("fullName") or " ".join(
                    p for p in (item.get("firstName"), item.get("lastName")) if p
                )
                if name:
                    authors.append(name)
            papers.append(
                Paper(
                    title=hit.get("title") or "untitled",
                    source="europe_pmc",
                    authors=authors,
                    year=str(hit.get("pubYear") or "") or None,
                    abstract=hit.get("abstractText") or "",
                    venue=hit.get("journalTitle") or hit.get("bookTitle") or "",
                    doi=normalize_doi(hit.get("doi")),
                    pdf_url=pdf_url,
                    landing_url=(
                        f"https://europepmc.org/articles/{hit['pmcid']}" if hit.get("pmcid") else None
                    ),
                    query=query,
                )
            )
        nxt = payload.get("nextCursorMark")
        if not nxt or nxt == cursor:
            break
        cursor = nxt
        time.sleep(0.12)
    return papers


def search_doaj(query: str, limit: int = 50) -> list[Paper]:
    papers: list[Paper] = []
    encoded = urllib.parse.quote(query, safe="()\"")
    page = 1
    while len(papers) < limit:
        size = min(50, limit - len(papers))
        payload = _get_json(
            f"https://doaj.org/api/search/articles/{encoded}",
            {"pageSize": size, "page": page},
        )
        items = payload.get("results") or []
        if not items:
            break
        for item in items:
            bib = item.get("bibjson") or {}
            doi = next(
                (i.get("id") for i in (bib.get("identifier") or []) if str(i.get("type") or "").lower() == "doi"),
                None,
            )
            pdf_url = None
            landing = None
            for lnk in bib.get("link") or []:
                url = lnk.get("url")
                if not url:
                    continue
                ctype = str(lnk.get("content_type") or "").lower()
                if ctype == "pdf" or ".pdf" in url.lower():
                    pdf_url = url
                elif lnk.get("type") == "fulltext" and not landing:
                    landing = url
            papers.append(
                Paper(
                    title=bib.get("title") or "untitled",
                    source="doaj",
                    authors=[a.get("name") for a in (bib.get("author") or []) if a.get("name")],
                    year=str(bib.get("year") or "") or None,
                    abstract=bib.get("abstract") or "",
                    venue=(bib.get("journal") or {}).get("title") or "",
                    doi=normalize_doi(doi),
                    pdf_url=pdf_url or landing,
                    landing_url=landing,
                    query=query,
                )
            )
        total = int((payload.get("total") or 0) or 0)
        if page * size >= total or len(items) < size:
            break
        page += 1
        time.sleep(0.12)
    return papers


def search_crossref(query: str, limit: int = 40) -> list[Paper]:
    papers: list[Paper] = []
    offset = 0
    while len(papers) < limit:
        rows = min(50, limit - len(papers))
        payload = _get_json(
            "https://api.crossref.org/works",
            {
                "query": query,
                "filter": "from-pub-date:1995,type:journal-article",
                "rows": rows,
                "offset": offset,
                "select": "DOI,title,author,issued,abstract,container-title,link,URL",
            },
        )
        items = (payload.get("message") or {}).get("items") or []
        if not items:
            break
        for item in items:
            year = None
            parts = ((item.get("issued") or {}).get("date-parts") or [[]])[0]
            if parts:
                year = str(parts[0])
            pdf_url = None
            for link in item.get("link") or []:
                if "pdf" in str(link.get("content-type") or "").lower() or str(link.get("URL") or "").lower().endswith(
                    ".pdf"
                ):
                    pdf_url = link.get("URL")
                    break
            authors = []
            for person in item.get("author") or []:
                name = " ".join(p for p in (person.get("given"), person.get("family")) if p)
                if name:
                    authors.append(name)
            titles = item.get("title") or ["untitled"]
            papers.append(
                Paper(
                    title=titles[0] if titles else "untitled",
                    source="crossref",
                    authors=authors,
                    year=year,
                    abstract=_strip_xml(item.get("abstract") or ""),
                    venue=" ".join(item.get("container-title") or []),
                    doi=normalize_doi(item.get("DOI")),
                    pdf_url=pdf_url,
                    landing_url=item.get("URL"),
                    query=query,
                )
            )
        offset += len(items)
        if len(items) < rows:
            break
        time.sleep(0.15)
    return papers


def search_arxiv(query: str, limit: int = 30) -> list[Paper]:
    """arXiv Atom API — extra OA PDFs for tunnel construction / SHM."""
    search = f"all:({query})"
    url = "http://export.arxiv.org/api/query"
    try:
        response = SESSION.get(
            url,
            params={"search_query": search, "start": 0, "max_results": min(limit, 50)},
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": USER_AGENT, "Accept": "application/atom+xml"},
        )
        response.raise_for_status()
    except requests.RequestException:
        return []
    ns = {"a": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError:
        return []
    papers: list[Paper] = []
    for entry in root.findall("a:entry", ns):
        title = (entry.findtext("a:title", default="", namespaces=ns) or "").strip()
        abstract = (entry.findtext("a:summary", default="", namespaces=ns) or "").strip()
        published = entry.findtext("a:published", default="", namespaces=ns) or ""
        year = published[:4] if published[:4].isdigit() else None
        authors = [
            (el.findtext("a:name", default="", namespaces=ns) or "").strip()
            for el in entry.findall("a:author", ns)
        ]
        pdf_url = None
        landing = None
        for link in entry.findall("a:link", ns):
            href = link.attrib.get("href")
            if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
                pdf_url = href
            elif link.attrib.get("rel") == "alternate":
                landing = href
        arxiv_id = (entry.findtext("a:id", default="", namespaces=ns) or "").rsplit("/", 1)[-1]
        if not pdf_url and arxiv_id:
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
        papers.append(
            Paper(
                title=title or "untitled",
                source="arxiv",
                authors=[a for a in authors if a],
                year=year,
                abstract=abstract,
                venue="arXiv",
                doi=normalize_doi(entry.findtext("{http://arxiv.org/schemas/atom}doi")),
                pdf_url=pdf_url,
                landing_url=landing,
                query=query,
            )
        )
    return papers


def search_semantic_scholar(query: str, limit: int = 20) -> list[Paper]:
    """Best-effort; skipped by the caller if the unauthenticated API rate-limits."""
    key = os.getenv("SEMANTIC_SCHOLAR_API_KEY")
    headers = dict(JSON_HEADERS)
    if key:
        headers["x-api-key"] = key
    response = SESSION.get(
        "https://api.semanticscholar.org/graph/v1/paper/search",
        params={
            "query": query,
            "limit": min(limit, 100),
            "fields": "title,year,authors,abstract,openAccessPdf,externalIds,venue,isOpenAccess",
        },
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code == 429:
        raise RuntimeError("semantic_scholar_rate_limited")
    response.raise_for_status()
    payload = response.json()
    papers: list[Paper] = []
    for item in payload.get("data") or []:
        pdf = (item.get("openAccessPdf") or {}).get("url")
        if not item.get("isOpenAccess") and not pdf:
            continue
        authors = [a.get("name") for a in (item.get("authors") or []) if a.get("name")]
        papers.append(
            Paper(
                title=item.get("title") or "untitled",
                source="semantic_scholar",
                authors=authors,
                year=str(item.get("year") or "") or None,
                abstract=item.get("abstract") or "",
                venue=item.get("venue") or "",
                doi=normalize_doi((item.get("externalIds") or {}).get("DOI")),
                pdf_url=pdf,
                query=query,
            )
        )
    return papers


_pmc_cache: dict[str, str | None] = {}


def pmc_pdf_url(doi: str | None) -> str | None:
    doi = normalize_doi(doi)
    if not doi:
        return None
    if doi in _pmc_cache:
        return _pmc_cache[doi]
    url: str | None = None
    try:
        payload = _get_json(
            "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/",
            {
                "ids": doi,
                "format": "json",
                "tool": "tunnel-crawler",
                "email": MAILTO,
            },
            timeout=15,
        )
        pmcid = (payload.get("records") or [{}])[0].get("pmcid")
        if pmcid:
            url = f"https://europepmc.org/articles/{pmcid}?pdf=render"
    except (requests.RequestException, ValueError, IndexError, AttributeError):
        url = None
    _pmc_cache[doi] = url
    return url


def unpaywall_pdf_url(doi: str | None) -> str | None:
    doi = normalize_doi(doi)
    if not doi:
        return None
    try:
        payload = _get_json(
            f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi)}",
            {"email": MAILTO},
            timeout=15,
        )
    except (requests.RequestException, ValueError):
        return None
    preferred: str | None = None
    for loc in [payload.get("best_oa_location"), *(payload.get("oa_locations") or [])]:
        if not loc:
            continue
        pdf = loc.get("url_for_pdf")
        if not pdf:
            continue
        if "mdpi.com" not in pdf.lower() and not _host_blocked(pdf):
            return pdf
        preferred = preferred or pdf
    return preferred


def resolve_pdf_url(paper: Paper) -> str | None:
    pmc = pmc_pdf_url(paper.doi)
    guessed = paper.pdf_url
    mdpi = bool(guessed and "mdpi.com" in guessed.lower())
    if pmc and (mdpi or not guessed):
        return pmc
    if guessed and not _host_blocked(guessed) and not mdpi:
        return guessed
    unpaywall = unpaywall_pdf_url(paper.doi)
    if unpaywall and "mdpi.com" not in unpaywall.lower() and not _host_blocked(unpaywall):
        return unpaywall
    return pmc or guessed or unpaywall


def allocate_stem(title: str, year: str | None = None) -> str:
    base = sanitize_filename(title, max_length=90)
    if year and str(year).isdigit():
        base = f"{year}_{base}"
    with _stem_lock:
        stem = base
        n = 2
        while stem in _used_stems or (PDF_DIR / f"{stem}.pdf").exists():
            stem = f"{base}_{n}"
            n += 1
        _used_stems.add(stem)
        return stem


def download_pdf(url: str, title: str, year: str | None = None, stem: str | None = None) -> dict[str, Any]:
    ensure_output_dir()
    stem = stem or allocate_stem(title, year)
    dest = PDF_DIR / f"{stem}.pdf"
    result: dict[str, Any] = {
        "ok": False,
        "url": url,
        "path": None,
        "stem": stem,
        "error": None,
        "skipped": None,
    }
    if dest.exists() and dest.stat().st_size > 1000:
        sha, size = hash_pdf(dest)
        result.update(
            ok=True,
            path=str(dest),
            skipped="already_downloaded",
            source_sha256=sha,
            source_size_bytes=size,
        )
        return result
    if not url or not url.lower().startswith("http"):
        result["error"] = "missing url"
        return result
    if _host_blocked(url):
        result["error"] = f"blocked host: {urllib.parse.urlparse(url).netloc}"
        return result
    try:
        parsed = urllib.parse.urlparse(url)
        response = SESSION.get(
            url,
            headers={**PDF_HEADERS, "Referer": f"{parsed.scheme}://{parsed.netloc}/"},
            timeout=REQUEST_TIMEOUT,
            stream=True,
            allow_redirects=True,
        )
        response.raise_for_status()
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_PDF_BYTES:
                result["error"] = "file too large"
                return result
            chunks.append(chunk)
        content = b"".join(chunks)
        if not content.startswith(b"%PDF"):
            result["error"] = "response is not a PDF"
            return result
        dest.write_bytes(content)
        sha, size = hash_pdf(dest)
        result.update(ok=True, path=str(dest), source_sha256=sha, source_size_bytes=size)
        return result
    except requests.RequestException as exc:
        result["error"] = f"download failed: {exc}"
        return result
    except OSError as exc:
        result["error"] = f"write failed: {exc}"
        return result


def _write_sidecar(paper: Paper, pdf_path: str, download: dict[str, Any]) -> Path:
    meta_path, summary_path = sidecar_paths(pdf_path)
    sha = download.get("source_sha256")
    size = download.get("source_size_bytes")
    if (not sha or not size) and Path(pdf_path).exists():
        sha, size = hash_pdf(pdf_path)
    payload = {
        **asdict(paper),
        "pdf_path": pdf_path,
        "path": pdf_path,
        "local_pdf_path": pdf_path,
        "discovery_source": paper.source,
        "discovery_query": paper.query,
        "relevance_score": paper.score,
        "source_sha256": sha,
        "source_size_bytes": size,
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "corpus_status": download.get("corpus_status") or CORPUS_STAGING,
        "ai_generated_literature_note": False,
        "evidence_eligible": False,
        "sha256_duplicate": bool(download.get("skipped") == "duplicate_sha256"),
        "duplicate_of": download.get("duplicate_of"),
        "download": {k: download.get(k) for k in ("ok", "skipped", "error", "url")},
    }
    meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if not summary_path.exists():
        abstract = (paper.abstract or "").strip() or "No abstract provided by the source API."
        authors = ", ".join(paper.authors) or "n/a"
        body = [
            "---",
            "ai_generated_literature_note: false",
            "evidence_eligible: false",
            f"source_pdf: {pdf_path}",
            f"source_sha256: {sha or 'n/a'}",
            "note_kind: source_abstract",
            "---",
            "",
            f"# {paper.title}",
            "",
            f"- Authors: {authors}",
            f"- Year: {paper.year or 'n/a'}",
            f"- Source: {paper.source}",
            f"- Venue: {paper.venue or 'n/a'}",
            f"- DOI: {paper.doi or 'n/a'}",
            f"- PDF: {pdf_path}",
            f"- Score: {paper.score}",
            "",
            "## Abstract",
            "",
            abstract[:2500],
            "",
        ]
        summary_path.write_text("\n".join(body), encoding="utf-8")
    return meta_path


def _append_index(record: dict[str, Any]) -> None:
    path = index_path()
    with _index_lock:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


SOURCE_FNS: list[tuple[str, Callable[[str, int], list[Paper]]]] = [
    ("openalex", search_openalex),
    ("europe_pmc", search_europe_pmc),
    ("doaj", search_doaj),
    ("crossref", search_crossref),
]


def _per_query_budget(limit: int) -> dict[str, int]:
    if limit <= 80:
        return {"openalex": 20, "europe_pmc": 15, "doaj": 15, "crossref": 12, "arxiv": 10, "semantic_scholar": 10}
    if limit <= 200:
        return {"openalex": 40, "europe_pmc": 30, "doaj": 25, "crossref": 20, "arxiv": 20, "semantic_scholar": 20}
    return {"openalex": 120, "europe_pmc": 60, "doaj": 50, "crossref": 40, "semantic_scholar": 30}


def _ingest_batch(
    batch: list[Paper],
    unique: dict[str, Paper],
    found: list[Paper],
    skip_keys: set[str],
) -> None:
    found.extend(batch)
    for paper in batch:
        paper.score = relevance_score(paper.title, paper.abstract)
        if paper.score <= 0 or paper.key() in skip_keys:
            continue
        key = paper.key()
        prev = unique.get(key)
        if prev is None or paper.score > prev.score or (paper.pdf_url and not prev.pdf_url):
            unique[key] = paper


def collect_candidates(
    target: int = 1500,
    limit: int = DEFAULT_LIMIT,
    skip_keys: set[str] | None = None,
    existing_count: int = 0,
) -> tuple[list[Paper], dict[str, Any]]:
    stats: dict[str, Any] = {"errors": [], "raw": 0, "by_source": {}}
    found: list[Paper] = []
    unique: dict[str, Paper] = {}
    skip_keys = skip_keys or set()
    budgets = _per_query_budget(limit)
    fns = list(SOURCE_FNS)
    if os.getenv("SEMANTIC_SCHOLAR_API_KEY"):
        fns.append(("semantic_scholar", search_semantic_scholar))

    queries = list(SEARCH_QUERIES) + list(FILL_QUERIES)

    skip_s2 = False
    for query in queries:
        print(f"  query: {query}")
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {}
            for name, fn in fns:
                if name == "semantic_scholar" and skip_s2:
                    continue
                futures[pool.submit(fn, query, budgets.get(name, 30))] = name
            for fut in as_completed(futures):
                name = futures[fut]
                try:
                    batch = fut.result()
                except Exception as exc:  # noqa: BLE001
                    msg = f"{name}: {exc}"
                    stats["errors"].append(msg)
                    print(f"    ! {msg}")
                    if name == "semantic_scholar" and "rate_limited" in str(exc):
                        skip_s2 = True
                    continue
                stats["by_source"][name] = stats["by_source"].get(name, 0) + len(batch)
                stats["raw"] += len(batch)
                print(f"    {name}: {len(batch)}")
                _ingest_batch(batch, unique, found, skip_keys)
        print(f"    unique relevant: {len(unique)}")
        if len(unique) >= target:
            print(f"  reached candidate target ({target}); stopping search")
            break
        time.sleep(0.2)

    if len(unique) < target:
        print("  OpenAlex year-window fill pass")
        windows = (("1995-01-01", "2012-12-31"), ("2013-01-01", "2019-12-31"), ("2020-01-01", "2026-12-31"))
        for query in FILL_QUERIES:
            if len(unique) >= target:
                break
            for start, end in windows:
                if len(unique) >= target:
                    break
                try:
                    batch = search_openalex(query, limit=50, from_date=start, to_date=end)
                except Exception as exc:  # noqa: BLE001
                    stats["errors"].append(f"openalex:{exc}")
                    continue
                stats["by_source"]["openalex"] = stats["by_source"].get("openalex", 0) + len(batch)
                stats["raw"] += len(batch)
                _ingest_batch(batch, unique, found, skip_keys)
                time.sleep(0.12)
            print(f"    unique relevant: {len(unique)}")
    return found, stats


def merge_and_rank(papers: list[Paper]) -> list[Paper]:
    best: dict[str, Paper] = {}
    for paper in papers:
        paper.score = relevance_score(paper.title, paper.abstract)
        if paper.score <= 0:
            continue
        key = paper.key()
        previous = best.get(key)
        if previous is None or paper.score > previous.score or (paper.pdf_url and not previous.pdf_url):
            best[key] = paper
    ranked = list(best.values())
    ranked.sort(key=lambda p: (p.score, bool(p.pdf_url), p.year or ""), reverse=True)
    return ranked


def _record_from_paper(paper: Paper, pdf_path: str, download: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": paper.title,
        "authors": paper.authors,
        "year": paper.year,
        "venue": paper.venue,
        "source": paper.source,
        "discovery_source": paper.source,
        "discovery_query": paper.query,
        "doi": paper.doi,
        "score": paper.score,
        "relevance_score": paper.score,
        "path": pdf_path,
        "pdf_path": pdf_path,
        "local_pdf_path": pdf_path,
        "pdf_url": paper.pdf_url,
        "landing_url": paper.landing_url,
        "query": paper.query,
        "skipped": download.get("skipped"),
        "source_sha256": download.get("source_sha256"),
        "source_size_bytes": download.get("source_size_bytes"),
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "corpus_status": download.get("corpus_status") or CORPUS_STAGING,
        "ai_generated_literature_note": False,
        "evidence_eligible": False,
        "sha256_duplicate": bool(download.get("skipped") == "duplicate_sha256"),
        "duplicate_of": download.get("duplicate_of"),
    }


def _save_catalog(downloaded: list[dict[str, Any]], stats: dict[str, Any]) -> Path:
    path = catalog_path()
    path.write_text(
        json.dumps(
            {
                "downloaded": downloaded,
                "papers": downloaded,
                "stats": stats,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def harvest(limit: int = DEFAULT_LIMIT, per_query: int = 12) -> dict[str, Any]:
    """Search all sources, rank tunnel-relevant OA hits, download up to ``limit`` PDFs."""
    del per_query  # budget is derived from limit
    ensure_output_dir()
    limit = max(5, min(int(limit), MAX_LIMIT))
    purge_offtopic()
    existing = load_existing_papers()
    existing_keys = {_paper_key_from_record(row) for row in existing}
    skip_keys = existing_keys | load_rejected_keys()
    seen_hashes = load_existing_sha256s(existing)
    print(f"\n=== Harvest (limit={limit}, already={len(existing)}) ===")
    print("Queries:", len(SEARCH_QUERIES) + len(FILL_QUERIES), " Sources: OpenAlex, Europe PMC, DOAJ, Crossref")
    print("Scope: highway-tunnel book (methods, cost/LCC, O&M, energy, safety)")
    print(f"Folders: pdfs/  metadata/  literature_notes/  → {OUTPUT_DIR}")
    print("corpus_status default=STAGING (not ingested into TunnelBookAI)")

    if len(existing) >= limit:
        stats = {"ranked": 0, "failed_pdf": 0, "raw": 0, "by_source": {}, "errors": []}
        catalog = _save_catalog(existing, stats)
        print(f"Already have {len(existing)} papers (>= {limit}). Nothing new to download.")
        return {
            "ok": True,
            "limit": limit,
            "raw_hits": 0,
            "ranked": 0,
            "downloaded": len(existing),
            "failed_pdf": 0,
            "by_source": {},
            "errors": [],
            "catalog": str(catalog),
            "papers": existing,
        }

    needed = limit - len(existing)
    target = max(needed * 6, 1200)
    raw, stats = collect_candidates(
        target=target,
        limit=limit,
        skip_keys=skip_keys,
        existing_count=len(existing),
    )
    ranked = [paper for paper in merge_and_rank(raw) if paper.key() not in skip_keys]
    print(f"Candidates after relevance filter: {len(ranked)} (from {stats['raw']} raw hits)")
    print(f"Need {needed} more PDFs to reach {limit}")

    downloaded: list[dict[str, Any]] = list(existing)
    failed = 0
    for paper in ranked:
        if len(downloaded) >= limit:
            break
        pdf_url = resolve_pdf_url(paper)
        paper.pdf_url = pdf_url
        result = download_pdf(pdf_url or "", paper.title, paper.year)
        if not result.get("ok"):
            failed += 1
            continue
        sha = result.get("source_sha256")
        dest_path = Path(str(result["path"]))
        if sha and sha in seen_hashes:
            original = Path(seen_hashes[sha])
            if dest_path.resolve() != original.resolve() and dest_path.exists():
                dest_path.unlink(missing_ok=True)
            result["path"] = str(original)
            result["skipped"] = "duplicate_sha256"
            result["duplicate_of"] = str(original)
            result["corpus_status"] = CORPUS_STAGING
            # Identical bytes already in staging; do not add a second catalog row.
            if original.exists():
                continue
        result["corpus_status"] = CORPUS_STAGING
        _write_sidecar(paper, result["path"], result)
        record = _record_from_paper(paper, result["path"], result)
        downloaded.append(record)
        existing_keys.add(paper.key())
        if sha:
            seen_hashes[str(sha)] = result["path"]
        _append_index(record)
        flag = "skip" if result.get("skipped") else "new"
        print(f"  [{len(downloaded):03d}/{limit}] {flag}  {paper.source:16}  {paper.title[:78]}")
        if not result.get("skipped"):
            time.sleep(0.25)
        if len(downloaded) % 25 == 0:
            _save_catalog(downloaded, {**stats, "ranked": len(ranked), "failed_pdf": failed})

    catalog = _save_catalog(downloaded, {**stats, "ranked": len(ranked), "failed_pdf": failed})
    summary = {
        "ok": True,
        "limit": limit,
        "raw_hits": stats["raw"],
        "ranked": len(ranked),
        "downloaded": len(downloaded),
        "failed_pdf": failed,
        "by_source": stats["by_source"],
        "errors": stats["errors"],
        "catalog": str(catalog),
        "papers": downloaded,
    }
    print(f"\nDownloaded {len(downloaded)} PDFs → {PDF_DIR}")
    print(f"Metadata → {METADATA_DIR}")
    print(f"Literature notes → {LITERATURE_NOTES_DIR}")
    return summary


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Harvest OA tunnel papers into pdfs/metadata/literature_notes staging.")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Target PDF count (default 800, max 1000).")
    args = parser.parse_args()
    harvest(limit=args.limit)
