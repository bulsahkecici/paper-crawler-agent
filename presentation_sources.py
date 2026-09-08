#!/usr/bin/env python3
"""Public presentation discovery, attribution and provisional slide assets.

PaperCrawler preserves presentation originals and provenance-oriented assets.
The extracted assets are deliberately PROVISIONAL: TunnelBookAI remains the
owner of canonical rendering, OCR, vision and evidence evaluation.
"""
from __future__ import annotations

import hashlib
import io
import mimetypes
import posixpath
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote_plus, urljoin, urlparse
import xml.etree.ElementTree as ET

from PIL import Image
import yaml

import classification_engine as classifier

CONFIG = Path(__file__).resolve().parent / "config" / "presentation_sources.yaml"


def _config() -> dict[str, Any]:
    value = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    return value if isinstance(value, dict) else {}


def platform_for(url: str | None) -> str | None:
    host = (urlparse(str(url or "")).hostname or "").casefold()
    for name, cfg in (_config().get("platforms") or {}).items():
        if any(host == d or host.endswith("." + d) for d in cfg.get("domains") or []):
            return str(name).upper()
    return None


def _presentation_metadata(record: dict[str, Any]) -> dict[str, Any]:
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    nested = extra.get("presentation_metadata") if isinstance(extra.get("presentation_metadata"), dict) else {}
    existing = record.get("presentation") if isinstance(record.get("presentation"), dict) else {}
    return {**nested, **existing, **{k: v for k, v in record.items() if v not in (None, "", [], {})}}


def resolve_presentation(record: dict[str, Any]) -> dict[str, Any]:
    """Resolve the real producer separately from the upload platform."""
    meta = _presentation_metadata(record)
    platform_url = meta.get("platform_url") or meta.get("source_url") or meta.get("landing_url")
    platform = platform_for(platform_url)
    base = classifier.classify_record({**record, **meta}).as_dict()
    producer = base.get("producer") or {}
    name = meta.get("actual_producer") or meta.get("organization") or producer.get("name")
    original = meta.get("original_url") or meta.get("institutional_url")
    if name and base.get("source_class") in {
        "TR_OFFICIAL", "FOREIGN_GOVERNMENT", "ROAD_AUTHORITY",
        "TRANSPORT_AUTHORITY", "INTERNATIONAL_OFFICIAL",
    }:
        dtype = "OFFICIAL_PRESENTATION"
    elif meta.get("conference") or meta.get("event"):
        dtype = "CONFERENCE_PRESENTATION"
    elif name and (meta.get("university") or base.get("source_class") in {
        "ACADEMIC", "UNIVERSITY_REPOSITORY", "RESEARCH_REPOSITORY",
    }):
        dtype = "ACADEMIC_PRESENTATION"
    else:
        dtype = "TECHNICAL_PRESENTATION"
    presentation = {
        "title": meta.get("title"),
        "authors": meta.get("author") or meta.get("authors") or [],
        "organization": name,
        "conference": meta.get("conference"),
        "event": meta.get("event"),
        "year": meta.get("year"),
        "platform": platform,
        "platform_url": platform_url,
        "original_source_url": original,
        "download_url": meta.get("download_url") or meta.get("pdf_url"),
        "slide_count": meta.get("slide_count"),
        "description": meta.get("description") or meta.get("abstract"),
        "transcript_url": meta.get("transcript_url"),
        "doi": meta.get("doi"),
        "related_paper": meta.get("related_paper"),
        "related_report": meta.get("related_report"),
    }
    return {
        "presentation": presentation,
        "document_type": dtype,
        "producer": {**producer, "name": name, "attribution_status": "RESOLVED" if name else "UNKNOWN"},
        "source_class": base.get("source_class") if name else (
            "RESEARCH_REPOSITORY" if platform in {"ZENODO", "FIGSHARE"} else "PRESENTATION_PLATFORM"
        ),
        "authority_tier": base.get("authority_tier") if name else "G",
        "original_source_resolved": bool(original),
        "acquisition_status": record.get("acquisition_status") or (
            "NO_PUBLIC_FULLTEXT" if not presentation["download_url"] else "PUBLIC_DOWNLOAD_AVAILABLE"
        ),
    }


class _SearchHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.images: list[str] = []
        self.meta: dict[str, str] = {}
        self.text_parts: list[str] = []
        self._href: str | None = None
        self._label: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag.lower() == "a" and values.get("href"):
            self._href, self._label = values["href"], []
        if tag.lower() == "meta" and str(values.get("property") or values.get("name") or "").lower() in {
            "og:image", "twitter:image",
        } and values.get("content"):
            self.images.append(str(values["content"]))
        if tag.lower() == "meta" and values.get("content"):
            key = str(values.get("property") or values.get("name") or "").strip().casefold()
            if key:
                self.meta.setdefault(key, str(values["content"]).strip())

    def handle_data(self, data: str) -> None:
        text = re.sub(r"\s+", " ", data).strip()
        if text:
            self.text_parts.append(text)
        if self._href:
            self._label.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            self.links.append((self._href, re.sub(r"\s+", " ", " ".join(self._label)).strip()))
            self._href, self._label = None, []


def _year(value: Any) -> str | None:
    match = re.search(r"(?:19|20)\d{2}", str(value or ""))
    return match.group(0) if match else None


def _zenodo_records(payload: dict[str, Any], query: str, limit: int) -> list[dict[str, Any]]:
    rows = ((payload.get("hits") or {}).get("hits") or [])[:limit]
    found: list[dict[str, Any]] = []
    for item in rows:
        metadata = item.get("metadata") or {}
        resource = metadata.get("resource_type") or {}
        resource_name = " ".join(str(x) for x in (
            metadata.get("upload_type"), resource.get("type") if isinstance(resource, dict) else resource,
            resource.get("title") if isinstance(resource, dict) else "",
        )).casefold()
        candidates = []
        for file_item in item.get("files") or []:
            links = file_item.get("links") or {}
            url = links.get("content") or links.get("self")
            if url:
                candidates.append((str(file_item.get("key") or ""), str(url)))
        deck = next((url for name, url in candidates if Path(urlparse(name).path).suffix.lower() in {".ppt", ".pptx", ".pdf"}), None)
        if "presentation" not in resource_name and not deck:
            continue
        creators = metadata.get("creators") or []
        authors = [str(x.get("name")) for x in creators if isinstance(x, dict) and x.get("name")]
        affiliations = [str(x.get("affiliation")) for x in creators if isinstance(x, dict) and x.get("affiliation")]
        links = item.get("links") or {}
        landing = links.get("html") or links.get("self")
        found.append({
            "title": str(metadata.get("title") or "Untitled presentation"),
            "authors": authors,
            "year": _year(metadata.get("publication_date") or metadata.get("dates")),
            "doi": metadata.get("doi"),
            "description": re.sub(r"<[^>]+>", " ", str(metadata.get("description") or "")),
            "organization": affiliations[0] if affiliations else None,
            "platform_url": landing,
            "source_url": landing,
            "landing_url": landing,
            "download_url": deck,
            "discovery_source": "presentation:ZENODO",
            "discovery_query": query,
            "platform": "ZENODO",
        })
    return found


def _enrich_html_record(row: dict[str, Any], *, safe_get: Callable[..., Any], platform_domains: list[str]) -> dict[str, Any]:
    """Read public page metadata without treating the host platform as producer."""
    response = safe_get(str(row["platform_url"]), respect_robots=True, timeout=12)
    try:
        parser = _SearchHTMLParser()
        parser.feed(response.text)
        meta = parser.meta
        page_url = response.url
        title = meta.get("og:title") or meta.get("twitter:title") or meta.get("citation_title") or row.get("title")
        authors = [x.strip() for x in re.split(r"\s*;\s*", meta.get("citation_author") or meta.get("author") or "") if x.strip()]
        organization = (
            meta.get("citation_author_institution") or meta.get("dc.publisher")
            or meta.get("citation_publisher") or meta.get("organization")
        )
        date = meta.get("citation_publication_date") or meta.get("article:published_time") or meta.get("date")
        description = meta.get("og:description") or meta.get("description") or row.get("description")
        download_url = None
        original_url = None
        preview_urls = list(row.get("preview_image_urls") or [])
        preview_urls.extend(urljoin(page_url, x) for x in parser.images)
        for href, label in parser.links:
            url = urljoin(page_url, href)
            suffix = Path(urlparse(url).path).suffix.lower()
            if not download_url and suffix in {".ppt", ".pptx", ".pdf"} and (
                suffix in {".ppt", ".pptx"} or "download" in label.casefold() or "slide" in label.casefold()
            ):
                download_url = url
            host = (urlparse(url).hostname or "").casefold()
            if not original_url and host and not any(host == d or host.endswith("." + d) for d in platform_domains):
                if any(token in f"{url} {label}".casefold() for token in ("tunnel", "institution", "university", "gov", "org", "report")):
                    original_url = url
        text = " ".join(parser.text_parts)
        count_match = re.search(r"\b(\d{1,4})\s+(?:slides?|sayfa|slayt)\b", text, re.I)
        return {
            **row,
            "title": title,
            "authors": authors or row.get("authors") or [],
            "organization": organization or row.get("organization"),
            "year": _year(date) or row.get("year"),
            "description": description,
            "event": meta.get("citation_conference_title") or meta.get("event"),
            "download_url": download_url or row.get("download_url"),
            "original_url": original_url or row.get("original_url"),
            "slide_count": int(count_match.group(1)) if count_match else row.get("slide_count"),
            "preview_image_urls": list(dict.fromkeys(preview_urls))[:10],
        }
    finally:
        response.close()


def _html_records(html_text: str, base_url: str, platform: str, query: str, cfg: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    parser = _SearchHTMLParser()
    parser.feed(html_text)
    patterns = [re.compile(str(x), re.I) for x in cfg.get("result_url_patterns") or []]
    domains = [str(x).casefold() for x in cfg.get("domains") or []]
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for href, label in parser.links:
        url = urljoin(base_url, href)
        host = (urlparse(url).hostname or "").casefold()
        if not any(host == domain or host.endswith("." + domain) for domain in domains):
            continue
        if patterns and not any(pattern.search(urlparse(url).path) for pattern in patterns):
            continue
        if url in seen or len(label) < 8:
            continue
        seen.add(url)
        found.append({
            "title": label,
            "platform_url": url,
            "source_url": url,
            "landing_url": url,
            "description": label,
            "preview_image_urls": list(dict.fromkeys(urljoin(base_url, x) for x in parser.images))[:3],
            "discovery_source": f"presentation:{platform}",
            "discovery_query": query,
            "platform": platform,
        })
        if len(found) >= limit:
            break
    return found


def discover_public_presentations(
    queries: Iterable[str], *, per_platform: int,
    safe_get: Callable[..., Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Discover public presentation metadata through bounded platform adapters."""
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    config = _config()
    query_limit = int((config.get("policy") or {}).get("max_queries_per_platform") or 8)
    platform_limit = int((config.get("policy") or {}).get("max_records_per_platform") or 20)
    failure_limit = int((config.get("policy") or {}).get("max_consecutive_failures") or 2)
    for platform, cfg in (config.get("platforms") or {}).items():
        if not cfg.get("enabled", False) or not cfg.get("search_url"):
            continue
        platform_records: list[dict[str, Any]] = []
        consecutive_failures = 0
        for query in list(queries)[:query_limit]:
            url = str(cfg["search_url"]).format(query=quote_plus(query), limit=max(1, per_platform))
            try:
                response = safe_get(url, respect_robots=True, timeout=12)
                if str(cfg.get("adapter")) == "zenodo_api":
                    batch = _zenodo_records(response.json(), query, per_platform)
                else:
                    batch = _html_records(response.text, response.url, str(platform).upper(), query, cfg, per_platform)
                response.close()
                if str(cfg.get("adapter")) != "zenodo_api":
                    enriched: list[dict[str, Any]] = []
                    domains = [str(x).casefold() for x in cfg.get("domains") or []]
                    for row in batch:
                        try:
                            enriched.append(_enrich_html_record(row, safe_get=safe_get, platform_domains=domains))
                        except Exception as exc:
                            errors.append(f"presentation:{platform}:detail:{row.get('platform_url')}: {exc}")
                            enriched.append(row)
                    batch = enriched
                platform_records.extend(batch)
                consecutive_failures = 0
                if len(platform_records) >= platform_limit:
                    break
            except Exception as exc:  # a platform must not stop discovery
                errors.append(f"presentation:{platform}:{query}: {exc}")
                consecutive_failures += 1
                if consecutive_failures >= failure_limit:
                    break
        records.extend(platform_records[:platform_limit])
    unique: dict[str, dict[str, Any]] = {}
    for row in records:
        key = str(row.get("doi") or row.get("download_url") or row.get("platform_url") or row.get("title"))
        unique.setdefault(key.casefold(), row)
    return list(unique.values()), errors


def _validated_image(data: bytes, name: str) -> tuple[str, str, int, int] | None:
    limits = _config().get("asset_policy") or {}
    if not data or len(data) > int(limits.get("max_image_bytes") or 20 * 1024 * 1024):
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            fmt = str(image.format or "").upper()
        if width * height > int(limits.get("max_image_pixels") or 80_000_000):
            return None
        ext = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "WEBP": ".webp", "TIFF": ".tif", "BMP": ".bmp"}.get(fmt)
        if not ext:
            ext = Path(name).suffix.lower()
        if ext not in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff", ".bmp"}:
            return None
        return ext, mimetypes.types_map.get(ext, f"image/{fmt.casefold()}"), width, height
    except Exception:
        return None


def _store_asset(
    data: bytes, *, name: str, slide_number: int, output_dir: Path,
    source_url: str | None, deck_sha256: str, method: str, index: int,
) -> dict[str, Any] | None:
    valid = _validated_image(data, name)
    if not valid:
        return None
    ext, media_type, width, height = valid
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"slide_{slide_number:04d}_image_{index:03d}{ext}"
    path.write_bytes(data)
    return {
        "kind": "presentation_slide_image",
        "path": str(path),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "media_type": media_type,
        "width": width,
        "height": height,
        "slide_number": slide_number,
        "source_url": source_url,
        "deck_sha256": deck_sha256,
        "extraction_method": method,
        "status": "PROVISIONAL",
    }


def _pptx_assets(path: Path, output_dir: Path, source_url: str | None, deck_sha256: str, limit: int) -> tuple[list[dict[str, Any]], int]:
    assets: list[dict[str, Any]] = []
    slide_numbers: list[int] = []
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        slides = (x for x in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", x))
        for slide_name in sorted(slides, key=lambda x: int(re.search(r"\d+", x).group())):
            slide_number = int(re.search(r"\d+", slide_name).group())
            slide_numbers.append(slide_number)
            rel_name = f"ppt/slides/_rels/{Path(slide_name).name}.rels"
            if rel_name not in names:
                continue
            root = ET.fromstring(archive.read(rel_name))
            targets = [str(node.attrib.get("Target") or "") for node in root if "image" in str(node.attrib.get("Type") or "").casefold()]
            for target in targets:
                if len(assets) >= limit:
                    break
                member = posixpath.normpath(posixpath.join(posixpath.dirname(slide_name), target))
                if member not in names or not member.startswith("ppt/media/"):
                    continue
                asset = _store_asset(
                    archive.read(member), name=member, slide_number=slide_number,
                    output_dir=output_dir, source_url=source_url, deck_sha256=deck_sha256,
                    method="pptx_relationship", index=len(assets) + 1,
                )
                if asset:
                    assets.append(asset)
    return assets, max(slide_numbers, default=0)


def _pdf_assets(path: Path, output_dir: Path, source_url: str | None, deck_sha256: str, limit: int) -> tuple[list[dict[str, Any]], int]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    assets: list[dict[str, Any]] = []
    for page_number, page in enumerate(reader.pages, 1):
        try:
            images = list(page.images)
        except Exception:
            images = []
        for image in images:
            if len(assets) >= limit:
                break
            asset = _store_asset(
                image.data, name=str(image.name or "image"), slide_number=page_number,
                output_dir=output_dir, source_url=source_url, deck_sha256=deck_sha256,
                method="pdf_page_xobject", index=len(assets) + 1,
            )
            if asset:
                assets.append(asset)
    return assets, len(reader.pages)


def extract_slide_assets(source_path: str | Path, output_dir: str | Path, *, source_url: str | None, deck_sha256: str) -> dict[str, Any]:
    """Extract bounded, provisional slide images with per-slide provenance."""
    source = Path(source_path)
    limit = int((_config().get("asset_policy") or {}).get("max_assets_per_deck") or 200)
    try:
        if source.suffix.lower() == ".pptx":
            assets, slide_count = _pptx_assets(source, Path(output_dir), source_url, deck_sha256, limit)
            method = "pptx_relationship"
        elif source.suffix.lower() == ".pdf":
            assets, slide_count = _pdf_assets(source, Path(output_dir), source_url, deck_sha256, limit)
            method = "pdf_page_xobject"
        else:
            assets, slide_count, method = [], 0, "unsupported_binary_ppt"
        return {"assets": assets, "slide_count": slide_count or None, "extraction_method": method, "errors": []}
    except Exception as exc:
        return {"assets": [], "slide_count": None, "extraction_method": "failed", "errors": [str(exc)]}


def capture_preview_assets(
    image_urls: Iterable[str], output_dir: str | Path, *, source_url: str | None,
    deck_sha256: str, safe_get: Callable[..., Any],
) -> dict[str, Any]:
    """Capture public platform previews when no downloadable deck is exposed."""
    assets: list[dict[str, Any]] = []
    errors: list[str] = []
    for slide_number, image_url in enumerate(list(dict.fromkeys(str(x) for x in image_urls if x))[:10], 1):
        try:
            response = safe_get(image_url, respect_robots=True, timeout=12)
            try:
                asset = _store_asset(
                    response.content, name=Path(urlparse(response.url).path).name or "preview",
                    slide_number=slide_number, output_dir=Path(output_dir),
                    source_url=source_url, deck_sha256=deck_sha256,
                    method="platform_public_preview", index=len(assets) + 1,
                )
                if asset:
                    assets.append(asset)
            finally:
                response.close()
        except Exception as exc:
            errors.append(f"{image_url}: {exc}")
    return {
        "assets": assets,
        "slide_count": len(assets) or None,
        "extraction_method": "platform_public_preview",
        "errors": errors,
    }
