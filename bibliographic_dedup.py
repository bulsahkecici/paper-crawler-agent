#!/usr/bin/env python3
"""Conservative bibliographic canonicalization with auditable merge reasons."""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import tunnel_harvest as harvest


def normalized_title(value: Any) -> str:
    text = unicodedata.normalize("NFKC", html.unescape(str(value or ""))).casefold()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def canonical_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith("utm_")))
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), query, ""))


def _authors(record: dict[str, Any]) -> set[str]:
    values = record.get("authors") or []
    if isinstance(values, str):
        values = [values]
    return {normalized_title(value) for value in values if normalized_title(value)}


def duplicate_reason(left: dict[str, Any], right: dict[str, Any]) -> str | None:
    left_doi, right_doi = harvest.normalize_doi(left.get("doi")), harvest.normalize_doi(right.get("doi"))
    if left_doi and left_doi == right_doi:
        return "DOI"
    left_sha, right_sha = str(left.get("source_sha256") or "").lower(), str(right.get("source_sha256") or "").lower()
    if left_sha and left_sha == right_sha:
        return "SHA256"
    left_url = canonical_url(left.get("resolved_url") or left.get("source_url") or left.get("pdf_url") or left.get("landing_url"))
    right_url = canonical_url(right.get("resolved_url") or right.get("source_url") or right.get("pdf_url") or right.get("landing_url"))
    if left_url and left_url == right_url:
        return "CANONICAL_URL"
    lt, rt = normalized_title(left.get("title")), normalized_title(right.get("title"))
    if not lt or not rt:
        return None
    same_year = str(left.get("year") or "")[:4] == str(right.get("year") or "")[:4] and bool(str(left.get("year") or "")[:4])
    author_overlap = bool(_authors(left) & _authors(right))
    if lt == rt and (same_year or author_overlap):
        return "TITLE_EXACT"
    if same_year and author_overlap and SequenceMatcher(None, lt, rt).ratio() >= 0.94:
        return "TITLE_FUZZY"
    return None


def canonicalize(records: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    canonical: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for raw in records:
        record = dict(raw)
        match = next(((existing, duplicate_reason(existing, record)) for existing in canonical if duplicate_reason(existing, record)), None)
        if not match:
            canonical.append(record)
            continue
        existing, reason = match
        assert reason is not None
        counts[reason] = counts.get(reason, 0) + 1
        existing.setdefault("duplicate_sources", []).append(record.get("discovery_source") or record.get("source"))
        existing.setdefault("duplicate_urls", []).extend(filter(None, [record.get("source_url"), record.get("pdf_url"), record.get("landing_url")]))
        existing.setdefault("duplicate_reasons", []).append(reason)
        if len(str(record.get("abstract") or "")) > len(str(existing.get("abstract") or "")):
            for key, value in record.items():
                if value not in (None, "", [], {}):
                    existing[key] = value
    for record in canonical:
        seed = harvest.normalize_doi(record.get("doi")) or str(record.get("source_sha256") or "") or normalized_title(record.get("title"))
        record["canonical_id"] = "CAN_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20].upper()
        record["duplicate_sources"] = sorted({str(x) for x in record.get("duplicate_sources") or [] if x})
        record["duplicate_urls"] = sorted({str(x) for x in record.get("duplicate_urls") or [] if x})
        record["duplicate_reason"] = ",".join(sorted(set(record.get("duplicate_reasons") or []))) or None
    return canonical, counts
