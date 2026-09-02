#!/usr/bin/env python3
"""Supplemental free discovery for news feeds and books.

This module extends the main discovery layer without any paid API:
- RSS/Atom feed discovery from curated institutional seed sites
- Crossref book/book-chapter discovery
- Open Library book metadata discovery
- Internet Archive text/book metadata discovery
- ISBN-aware dedup before appending to discovery_catalog.jsonl

The module intentionally reuses free_discovery's network-safety and acquisition
rules. Book records are metadata-first unless a clearly public source URL is
returned; restricted full text is never bypassed.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable

import requests

import free_discovery
import tunnel_harvest as harvest


def _norm_isbn(value: Any) -> str | None:
    raw = re.sub(r"[^0-9Xx]", "", str(value or ""))
    if len(raw) in {10, 13}:
        return raw.upper()
    return None


def _first_isbn(values: Iterable[Any]) -> str | None:
    for value in values:
        isbn = _norm_isbn(value)
        if isbn:
            return isbn
    return None


def _record_identity(row: dict[str, Any]) -> str:
    doi = harvest.normalize_doi(row.get("doi"))
    if doi:
        return "doi:" + doi.lower()
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    isbn = _norm_isbn(row.get("isbn")) or _norm_isbn(extra.get("isbn"))
    if isbn:
        return "isbn:" + isbn
    for key in ("pdf_url", "source_url", "landing_url", "resolved_url"):
        url = row.get(key)
        if url:
            try:
                return "url:" + free_discovery.normalize_url(str(url))
            except Exception:
                pass
    title = re.sub(r"\W+", " ", str(row.get("title") or "").lower()).strip()
    authors = row.get("authors") or []
    if isinstance(authors, str):
        authors = [authors]
    author = re.sub(r"\W+", " ", str(authors[0] if authors else "").lower()).strip()
    year = str(row.get("year") or "")[:4]
    return f"work:{title}|{author}|{year}"


def _append_catalog(output_dir: str | Path | None, rows: list[dict[str, Any]]) -> dict[str, int]:
    if output_dir is not None:
        harvest.set_output_dir(output_dir)
    path = harvest.OUTPUT_DIR / "discovery_catalog.jsonl"
    existing: list[dict[str, Any]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                existing.append(item)
    best: dict[str, dict[str, Any]] = {_record_identity(row): row for row in existing}
    added = 0
    replaced = 0
    for row in rows:
        key = _record_identity(row)
        prev = best.get(key)
        if prev is None:
            best[key] = row
            added += 1
            continue
        # Prefer acquired/richer records over metadata-only duplicates.
        rank = (
            row.get("acquisition_status") in {"DOWNLOADED_PDF", "SNAPSHOTTED_WEB"},
            not bool(row.get("metadata_only")),
            bool(row.get("source_url") or row.get("landing_url")),
            len(str(row.get("abstract") or "")),
        )
        prev_rank = (
            prev.get("acquisition_status") in {"DOWNLOADED_PDF", "SNAPSHOTTED_WEB"},
            not bool(prev.get("metadata_only")),
            bool(prev.get("source_url") or prev.get("landing_url")),
            len(str(prev.get("abstract") or "")),
        )
        if rank > prev_rank:
            best[key] = row
            replaced += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in best.values():
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return {"added": added, "replaced": replaced, "total": len(best)}


def _feed_urls_from_html(base_url: str, html_text: str) -> list[str]:
    urls: list[str] = []
    # Detect <link rel="alternate" type="application/rss+xml|atom+xml" href="...">
    for match in re.finditer(r"<link\b[^>]*>", html_text, re.I):
        tag = match.group(0)
        if not re.search(r"rel\s*=\s*[\"'][^\"']*alternate", tag, re.I):
            continue
        if not re.search(r"type\s*=\s*[\"']application/(?:rss|atom)\+xml", tag, re.I):
            continue
        href = re.search(r"href\s*=\s*[\"']([^\"']+)", tag, re.I)
        if href:
            urls.append(urllib.parse.urljoin(base_url, href.group(1)))
    return list(dict.fromkeys(urls))


def discover_feed_urls(seed_url: str) -> list[str]:
    if not free_discovery.is_public_web_url(seed_url):
        return []
    urls: list[str] = []
    try:
        response = free_discovery.safe_get(seed_url)
        urls.extend(_feed_urls_from_html(response.url, response.text))
    except Exception:
        pass
    parsed = urllib.parse.urlparse(seed_url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    for path in ("/feed", "/feed/", "/rss", "/rss.xml", "/feed.xml", "/atom.xml", "/news/rss", "/haber/rss"):
        candidate = root + path
        try:
            response = free_discovery.safe_get(candidate, respect_robots=False, timeout=8)
            content_type = str(response.headers.get("Content-Type") or "").lower()
            head = response.text[:500].lower()
            if "xml" in content_type or "<rss" in head or "<feed" in head:
                urls.append(response.url)
        except Exception:
            continue
    return list(dict.fromkeys(urls))


def parse_feed(xml_bytes: bytes, feed_url: str, publisher: str, source_class: str, limit: int = 100) -> list[free_discovery.DiscoveryRecord]:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    out: list[free_discovery.DiscoveryRecord] = []
    for node in root.iter():
        local = node.tag.split("}")[-1].lower()
        if local not in {"item", "entry"}:
            continue
        fields: dict[str, list[str]] = {}
        for child in node.iter():
            name = child.tag.split("}")[-1].lower()
            text = re.sub(r"\s+", " ", (child.text or "")).strip()
            if text:
                fields.setdefault(name, []).append(text)
            if name == "link":
                href = child.attrib.get("href")
                if href:
                    fields.setdefault("link", []).append(href)
        title = " ".join(fields.get("title") or []).strip()
        links = fields.get("link") or fields.get("guid") or []
        url = next((urllib.parse.urljoin(feed_url, x) for x in links if str(x).startswith(("http://", "https://", "/"))), None)
        summary = " ".join((fields.get("description") or []) + (fields.get("summary") or []) + (fields.get("content") or []))
        if not title or not free_discovery.relevant_text(f"{title} {summary} {url or ''}"):
            continue
        date_text = " ".join((fields.get("pubdate") or []) + (fields.get("published") or []) + (fields.get("updated") or []) + (fields.get("date") or []))
        year_match = re.search(r"\b(?:19|20)\d{2}\b", date_text)
        out.append(free_discovery.DiscoveryRecord(
            title=title,
            source="rss_atom",
            discovery_source="rss_atom",
            discovery_query="institutional_feed",
            source_url=url,
            landing_url=url,
            publisher=publisher,
            abstract=re.sub(r"<[^>]+>", " ", summary)[:4000],
            year=year_match.group(0) if year_match else None,
            document_type="NEWS",
            source_class=source_class,
            metadata_only=url is None,
            access_kind="rss_atom",
            extra={"feed_url": feed_url},
        ))
        if len(out) >= limit:
            break
    return out


def discover_institutional_news(limit_per_feed: int = 100) -> tuple[list[free_discovery.DiscoveryRecord], list[str]]:
    cfg = free_discovery._config().get("institutional_sources") or {}
    records: list[free_discovery.DiscoveryRecord] = []
    errors: list[str] = []
    seen_feeds: set[str] = set()
    for name, source_cfg in cfg.items():
        if not source_cfg.get("enabled", False):
            continue
        publisher = str(source_cfg.get("publisher") or name)
        source_class = str(source_cfg.get("source_class") or "UNKNOWN")
        for seed in source_cfg.get("seed_urls") or []:
            for feed_url in discover_feed_urls(str(seed)):
                if feed_url in seen_feeds:
                    continue
                seen_feeds.add(feed_url)
                try:
                    response = free_discovery.safe_get(feed_url, respect_robots=False, timeout=15)
                    records.extend(parse_feed(response.content, feed_url, publisher, source_class, limit_per_feed))
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"feed {feed_url}: {exc}")
    return records, errors


def search_crossref_books(query: str, limit: int = 20) -> list[free_discovery.DiscoveryRecord]:
    endpoint = "https://api.crossref.org/works"
    params = {"query.bibliographic": query, "rows": min(max(limit, 1), 100)}
    response = free_discovery.safe_get(endpoint + "?" + urllib.parse.urlencode(params), respect_robots=False)
    items = ((response.json().get("message") or {}).get("items") or [])
    out: list[free_discovery.DiscoveryRecord] = []
    for item in items:
        typ = str(item.get("type") or "").lower()
        if typ not in {"book", "book-chapter", "reference-book", "monograph", "edited-book"}:
            continue
        titles = item.get("title") or []
        title = str(titles[0] if titles else "untitled")
        blob = f"{title} {' '.join(item.get('subject') or [])}"
        if not free_discovery.relevant_text(blob):
            continue
        authors = []
        for author in item.get("author") or []:
            name = " ".join(x for x in (str(author.get("given") or ""), str(author.get("family") or "")) if x).strip()
            if name:
                authors.append(name)
        doi = harvest.normalize_doi(item.get("DOI"))
        url = str(item.get("URL") or ("https://doi.org/" + doi if doi else "")) or None
        isbn = _first_isbn(item.get("ISBN") or [])
        issued = item.get("issued") or {}
        parts = issued.get("date-parts") or []
        year = str(parts[0][0]) if parts and parts[0] else None
        out.append(free_discovery.DiscoveryRecord(
            title=title,
            source="crossref_books",
            discovery_source="crossref_books",
            discovery_query=query,
            source_url=url,
            landing_url=url,
            publisher=str(item.get("publisher") or ""),
            authors=authors,
            year=year,
            doi=doi,
            document_type="BOOK_CHAPTER" if typ == "book-chapter" else "BOOK",
            source_class="ACADEMIC",
            metadata_only=True,
            access_kind="book_metadata",
            extra={"isbn": isbn, "crossref_type": typ},
        ))
    return out


def search_openlibrary(query: str, limit: int = 20) -> list[free_discovery.DiscoveryRecord]:
    endpoint = "https://openlibrary.org/search.json"
    params = {"q": query, "limit": min(max(limit, 1), 100), "fields": "key,title,author_name,first_publish_year,isbn,publisher,subject"}
    response = free_discovery.safe_get(endpoint + "?" + urllib.parse.urlencode(params), respect_robots=False)
    docs = response.json().get("docs") or []
    out: list[free_discovery.DiscoveryRecord] = []
    for item in docs:
        title = str(item.get("title") or "untitled")
        subjects = item.get("subject") or []
        if not free_discovery.relevant_text(f"{title} {' '.join(str(x) for x in subjects[:30])}"):
            continue
        key = str(item.get("key") or "")
        url = "https://openlibrary.org" + key if key.startswith("/") else None
        isbn = _first_isbn(item.get("isbn") or [])
        publishers = item.get("publisher") or []
        out.append(free_discovery.DiscoveryRecord(
            title=title,
            source="openlibrary",
            discovery_source="openlibrary",
            discovery_query=query,
            source_url=url,
            landing_url=url,
            publisher=str(publishers[0] if publishers else ""),
            authors=[str(x) for x in (item.get("author_name") or [])],
            year=str(item.get("first_publish_year") or "") or None,
            document_type="BOOK",
            source_class="ACADEMIC",
            metadata_only=True,
            access_kind="book_metadata",
            extra={"isbn": isbn, "openlibrary_key": key, "subjects": subjects[:30]},
        ))
    return out


def search_internet_archive(query: str, limit: int = 20) -> list[free_discovery.DiscoveryRecord]:
    endpoint = "https://archive.org/advancedsearch.php"
    q = f"({query}) AND mediatype:texts"
    params = {"q": q, "fl[]": ["identifier", "title", "creator", "date", "publisher", "subject", "isbn"], "rows": min(max(limit, 1), 100), "page": 1, "output": "json"}
    # urlencode with doseq keeps repeated fl[] parameters.
    response = free_discovery.safe_get(endpoint + "?" + urllib.parse.urlencode(params, doseq=True), respect_robots=False)
    docs = ((response.json().get("response") or {}).get("docs") or [])
    out: list[free_discovery.DiscoveryRecord] = []
    for item in docs:
        title = str(item.get("title") or "untitled")
        subjects = item.get("subject") or []
        if isinstance(subjects, str):
            subjects = [subjects]
        if not free_discovery.relevant_text(f"{title} {' '.join(str(x) for x in subjects)}"):
            continue
        identifier = str(item.get("identifier") or "")
        url = f"https://archive.org/details/{identifier}" if identifier else None
        creators = item.get("creator") or []
        if isinstance(creators, str):
            creators = [creators]
        isbn_values = item.get("isbn") or []
        if isinstance(isbn_values, str):
            isbn_values = [isbn_values]
        date = str(item.get("date") or "")
        year_match = re.search(r"\b(?:19|20)\d{2}\b", date)
        out.append(free_discovery.DiscoveryRecord(
            title=title,
            source="internet_archive",
            discovery_source="internet_archive",
            discovery_query=query,
            source_url=url,
            landing_url=url,
            publisher=str(item.get("publisher") or ""),
            authors=[str(x) for x in creators],
            year=year_match.group(0) if year_match else None,
            document_type="BOOK",
            source_class="ACADEMIC",
            metadata_only=True,
            access_kind="book_metadata",
            extra={"isbn": _first_isbn(isbn_values), "archive_identifier": identifier, "subjects": subjects[:30]},
        ))
    return out


def discover_books(queries: list[str], per_source: int = 10) -> tuple[list[free_discovery.DiscoveryRecord], list[str]]:
    records: list[free_discovery.DiscoveryRecord] = []
    errors: list[str] = []
    # Book discovery uses a bounded subset of broad tunnel-topic queries.
    for query in queries:
        for name, fn in (("crossref_books", search_crossref_books), ("openlibrary", search_openlibrary), ("internet_archive", search_internet_archive)):
            try:
                records.extend(fn(query, per_source))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{name}:{query}: {exc}")
    return records, errors


def run_supplemental_discovery(
    *,
    output_dir: str | Path | None = None,
    max_book_queries: int = 18,
    per_source: int = 8,
    acquire_news: bool = True,
) -> dict[str, Any]:
    if output_dir is not None:
        harvest.set_output_dir(output_dir)
    records: list[free_discovery.DiscoveryRecord] = []
    errors: list[str] = []

    news, news_errors = discover_institutional_news()
    records.extend(news)
    errors.extend(news_errors)

    queries = free_discovery.taxonomy_queries(max_queries=max_book_queries)
    books, book_errors = discover_books(queries, per_source=per_source)
    records.extend(books)
    errors.extend(book_errors)

    # Deduplicate the supplemental batch using DOI/ISBN/URL identity.
    batch_best: dict[str, free_discovery.DiscoveryRecord] = {}
    for record in records:
        payload = record.as_dict()
        isbn = _norm_isbn((record.extra or {}).get("isbn"))
        key = "isbn:" + isbn if isbn else _record_identity(payload)
        previous = batch_best.get(key)
        if previous is None or len(record.abstract or "") > len(previous.abstract or ""):
            batch_best[key] = record

    acquired_rows: list[dict[str, Any]] = []
    for record in batch_best.values():
        # News pages can be snapshotted; book catalog records remain metadata-first.
        if acquire_news and record.document_type == "NEWS" and not record.metadata_only:
            acquired_rows.append(free_discovery.acquire_record(record, harvest.OUTPUT_DIR / "discovery_sources"))
        else:
            row = record.as_dict()
            row["paper_crawler_status"] = "DISCOVERED"
            row["acquisition_status"] = "METADATA_ONLY"
            acquired_rows.append(row)

    merge_report = _append_catalog(output_dir, acquired_rows)
    counts: dict[str, int] = {}
    for row in acquired_rows:
        key = str(row.get("document_type") or "UNKNOWN")
        counts[key] = counts.get(key, 0) + 1
    return {
        "records": len(acquired_rows),
        "document_type_counts": counts,
        "catalog": str(harvest.OUTPUT_DIR / "discovery_catalog.jsonl"),
        "catalog_merge": merge_report,
        "errors": errors,
    }
