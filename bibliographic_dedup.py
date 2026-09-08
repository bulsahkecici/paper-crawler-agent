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
    # Exact identifiers and the conservative title/year/author gates let us
    # shortlist every pair that duplicate_reason() could possibly accept.  The
    # final predicate and earliest-canonical-record rule remain unchanged.
    indexes: dict[str, dict[Any, set[int]]] = {
        name: {} for name in ("doi", "sha", "url", "title_year", "title_author", "year_author")
    }
    indexed_keys: list[dict[str, set[Any]]] = []

    def keys_for(record: dict[str, Any]) -> dict[str, set[Any]]:
        doi = harvest.normalize_doi(record.get("doi"))
        sha = str(record.get("source_sha256") or "").lower()
        url = canonical_url(record.get("resolved_url") or record.get("source_url") or record.get("pdf_url") or record.get("landing_url"))
        title = normalized_title(record.get("title"))
        year = str(record.get("year") or "")[:4]
        authors = _authors(record)
        return {
            "doi": {doi} if doi else set(),
            "sha": {sha} if sha else set(),
            "url": {url} if url else set(),
            "title_year": {(title, year)} if title and year else set(),
            "title_author": {(title, author) for author in authors} if title else set(),
            "year_author": {(year, author) for author in authors} if year else set(),
        }

    def add_to_indexes(position: int, keys: dict[str, set[Any]]) -> None:
        for name, values in keys.items():
            for value in values:
                indexes[name].setdefault(value, set()).add(position)

    def remove_from_indexes(position: int, keys: dict[str, set[Any]]) -> None:
        for name, values in keys.items():
            for value in values:
                positions = indexes[name].get(value)
                if positions is not None:
                    positions.discard(position)
                    if not positions:
                        indexes[name].pop(value, None)

    for raw in records:
        record = dict(raw)
        record_keys = keys_for(record)
        candidates: set[int] = set()
        for name, values in record_keys.items():
            for value in values:
                candidates.update(indexes[name].get(value, ()))
        match = None
        for position in sorted(candidates):
            reason = duplicate_reason(canonical[position], record)
            if reason:
                match = canonical[position], reason, position
                break
        if not match:
            canonical.append(record)
            indexed_keys.append(record_keys)
            add_to_indexes(len(canonical) - 1, record_keys)
            continue
        existing, reason, position = match
        assert reason is not None
        counts[reason] = counts.get(reason, 0) + 1
        existing.setdefault("duplicate_sources", []).append(record.get("discovery_source") or record.get("source"))
        existing.setdefault("duplicate_urls", []).extend(filter(None, [record.get("source_url"), record.get("pdf_url"), record.get("landing_url")]))
        existing.setdefault("duplicate_reasons", []).append(reason)
        if len(str(record.get("abstract") or "")) > len(str(existing.get("abstract") or "")):
            remove_from_indexes(position, indexed_keys[position])
            for key, value in record.items():
                if value not in (None, "", [], {}):
                    existing[key] = value
            indexed_keys[position] = keys_for(existing)
            add_to_indexes(position, indexed_keys[position])
    for record in canonical:
        seed = harvest.normalize_doi(record.get("doi")) or str(record.get("source_sha256") or "") or normalized_title(record.get("title"))
        record["canonical_id"] = "CAN_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20].upper()
        record["duplicate_sources"] = sorted({str(x) for x in record.get("duplicate_sources") or [] if x})
        record["duplicate_urls"] = sorted({str(x) for x in record.get("duplicate_urls") or [] if x})
        record["duplicate_reason"] = ",".join(sorted(set(record.get("duplicate_reasons") or []))) or None
    return canonical, counts
