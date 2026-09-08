"""Safe local-PDF matching and import support for the acquisition desk."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pypdf import PdfReader


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().casefold()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _slug(value: Any, limit: int = 105) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", _normalize(value)).strip("_")
    return (cleaned or "manual_source")[:limit].rstrip("_")


def _http_url(value: Any) -> str | None:
    try:
        parsed = urlparse(str(value or ""))
    except ValueError:
        return None
    return str(value) if parsed.scheme in {"http", "https"} and parsed.netloc else None


def _download_site(row: dict[str, Any]) -> tuple[str | None, str | None]:
    for key in ("pdf_url", "source_url", "landing_url"):
        url = _http_url(row.get(key))
        if url:
            domain = urlparse(url).netloc.casefold().removeprefix("www.")
            if domain == "dx.doi.org":
                domain = "doi.org"
            return domain, key
    if row.get("doi"):
        return "doi.org", "doi"
    return None, None


class AcquisitionStore:
    def __init__(self, root: Path, downloads: Path) -> None:
        self.root = root.resolve()
        self.downloads = downloads.expanduser().resolve()
        self.queue_path = self.root / "exports" / "TunnelBookAI_Source_Pack" / "99_audit" / "retry_acquisition.jsonl"
        self.index_path = self.root / "classification_index.jsonl"
        self.exclusions_path = self.root / "audit" / "manual_acquisition_exclusions.json"
        self.lock = threading.Lock()
        self._file_cache: dict[tuple[str, int, int], dict[str, Any]] = {}
        self._suggestion_cache_key: tuple[Any, ...] | None = None
        self._suggestion_cache: dict[str, Any] | None = None

    @staticmethod
    def _keys(row: dict[str, Any]) -> set[str]:
        return {str(row.get(key) or "") for key in ("document_key", "canonical_id", "doi", "source_sha256") if row.get(key)}

    def _manual_exclusions(self) -> dict[str, dict[str, Any]]:
        if not self.exclusions_path.exists():
            return {}
        try:
            value = json.loads(self.exclusions_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def set_excluded(self, document_id: str, excluded: bool) -> dict[str, Any]:
        if not document_id:
            raise ValueError("Kayıt kimliği eksik")
        with self.lock:
            values = self._manual_exclusions()
            if excluded:
                values[document_id] = {"reason":"MANUAL_IRRELEVANT", "updated_at":datetime.now(timezone.utc).isoformat()}
            else:
                values.pop(document_id, None)
            self.exclusions_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.exclusions_path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.exclusions_path)
        return {"document_id": document_id, "excluded": excluded}

    def records(self) -> list[dict[str, Any]]:
        index = _read_jsonl(self.index_path)
        manual_exclusions = self._manual_exclusions()
        by_key = {key: row for row in index for key in self._keys(row)}
        output = []
        for position, queued in enumerate(_read_jsonl(self.queue_path), 1):
            document_id = str(queued.get("document_id") or queued.get("doi") or f"row:{position}")
            current = by_key.get(document_id, {})
            row = {**queued, **current, "document_id": document_id, "position": position}
            source = Path(str(row.get("source_path") or ""))
            row["acquisition_resolved"] = bool(
                source.is_file()
                and str(row.get("acquisition_status") or "").upper()
                in {"DOWNLOADED_PDF", "DOWNLOADED_ORIGINAL", "DOWNLOADED_PRESENTATION"}
            )
            domain, link_kind = _download_site(row)
            relevance = str(row.get("effective_relevance_status") or row.get("relevance_status") or "").upper()
            status = str(row.get("classification_status") or "").upper()
            excluded = []
            if relevance in {"WEAK", "IRRELEVANT", ""} or status.startswith("REJECT_"):
                excluded.append("IRRELEVANT_OR_WEAK")
            if not domain:
                excluded.append("NO_DOWNLOAD_SITE")
            if document_id in manual_exclusions:
                excluded.append("MANUAL_EXCLUDED")
            row["download_domain"] = domain
            row["download_link_kind"] = link_kind
            row["manual_download_eligible"] = not excluded
            row["manual_download_exclusion_reasons"] = excluded
            output.append(row)
        return sorted(output, key=lambda row: (
            row.get("download_domain") or "~no-site",
            _normalize(row.get("title")),
            row.get("position", 0),
        ))

    def _inspect_pdf(self, path: Path) -> dict[str, Any]:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        cached = self._file_cache.get(key)
        if cached:
            return cached
        result: dict[str, Any] = {
            "name": path.name,
            "relative_path": str(path.relative_to(self.downloads)),
            "size_bytes": stat.st_size,
            "valid": False,
        }
        try:
            reader = PdfReader(path)
            text = " ".join((page.extract_text() or "") for page in reader.pages[:2])
            metadata = reader.metadata or {}
            result.update({
                "valid": len(reader.pages) > 0,
                "pages": len(reader.pages),
                "pdf_title": str(metadata.get("/Title") or ""),
                "text_sample": " ".join(text.split())[:4000],
                "sha256": _sha256(path),
            })
        except Exception as exc:
            result["error"] = str(exc)[:200]
        self._file_cache[key] = result
        return result

    def files(self) -> list[dict[str, Any]]:
        if not self.downloads.is_dir():
            return []
        return [self._inspect_pdf(path) for path in sorted(self.downloads.glob("*.pdf"), key=lambda p: p.name.casefold())]

    @staticmethod
    def _match(record: dict[str, Any], file: dict[str, Any]) -> tuple[float, str]:
        doi = str(record.get("doi") or "").casefold()
        sample = str(file.get("text_sample") or "").casefold()
        if doi and doi in sample:
            return 1.0, "DOI"
        title = _normalize(record.get("title"))
        candidates = [
            _normalize(Path(str(file.get("name") or "")).stem),
            _normalize(file.get("pdf_title")),
        ]
        candidates = [candidate for candidate in candidates if candidate]
        if not title or not candidates:
            return 0.0, "NONE"
        sequence = max(SequenceMatcher(None, title, candidate).ratio() for candidate in candidates)
        left = set(title.split())
        token_score = max(len(left & set(candidate.split())) / max(1, len(left | set(candidate.split()))) for candidate in candidates)
        for candidate in candidates:
            length_ratio = min(len(title), len(candidate)) / max(1, max(len(title), len(candidate)))
            if (title in candidate or candidate in title) and length_ratio >= 0.65:
                return max(0.96, sequence), "TITLE"
        score = max(sequence, token_score)
        return round(score, 4), "TITLE_SIMILARITY"

    def suggestions(self, records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        pdf_signature = tuple(
            (path.name, path.stat().st_mtime_ns, path.stat().st_size)
            for path in sorted(self.downloads.glob("*.pdf"), key=lambda item: item.name.casefold())
        ) if self.downloads.is_dir() else ()
        cache_key = (
            self.index_path.stat().st_mtime_ns if self.index_path.exists() else 0,
            self.queue_path.stat().st_mtime_ns if self.queue_path.exists() else 0,
            pdf_signature,
        )
        if self._suggestion_cache_key == cache_key and self._suggestion_cache is not None:
            return self._suggestion_cache
        files = self.files()
        suggestions: dict[str, dict[str, Any]] = {}
        best_by_record: dict[str, tuple[float, str, dict[str, Any]]] = {}
        for record in records if records is not None else self.records():
            ranked = sorted(((*self._match(record, file), file) for file in files), key=lambda item: item[0], reverse=True)
            if ranked and ranked[0][0] >= 0.72:
                best_by_record[record["document_id"]] = ranked[0]
        contenders: dict[str, list[tuple[float, str, str]]] = {}
        for document_id, (score, reason, file) in best_by_record.items():
            contenders.setdefault(file["relative_path"], []).append((score, reason, document_id))
        for document_id, (score, reason, file) in best_by_record.items():
            competing = sorted(contenders[file["relative_path"]], reverse=True)
            if competing[0][2] != document_id:
                continue
            if len(competing) > 1 and competing[0][0] - competing[1][0] < 0.03:
                continue
            suggestions[document_id] = {
                "file": file["relative_path"], "score": score, "reason": reason,
                "valid": file.get("valid"), "pages": file.get("pages"),
            }
        result = {"downloads_dir": str(self.downloads), "files": files, "suggestions": suggestions}
        self._suggestion_cache_key = cache_key
        self._suggestion_cache = result
        return result

    def import_pdf(self, document_id: str, relative_path: str) -> dict[str, Any]:
        source = (self.downloads / relative_path).resolve()
        if not source.is_file() or not source.is_relative_to(self.downloads) or source.suffix.lower() != ".pdf":
            raise ValueError("Geçersiz veya izin verilmeyen PDF yolu")
        inspected = self._inspect_pdf(source)
        if not inspected.get("valid"):
            raise ValueError("PDF doğrulanamadı")
        with self.lock:
            rows = _read_jsonl(self.index_path)
            row = next((item for item in rows if document_id in self._keys(item)), None)
            if row is None:
                raise ValueError("Kayıt sınıflandırma indeksinde bulunamadı")
            sha = str(inspected["sha256"])
            duplicate = next((item for item in rows if item.get("source_sha256") == sha and Path(str(item.get("source_path") or "")).is_file()), None)
            if duplicate:
                destination = Path(str(duplicate["source_path"]))
                import_status = "EXACT_DUPLICATE_ALREADY_PRESENT"
            else:
                year = str(row.get("year") or "undated")
                destination = self.root / "pdfs" / f"{year}_{_slug(row.get('title'))}.pdf"
                if destination.exists() and _sha256(destination) != sha:
                    destination = destination.with_name(f"{destination.stem}_{sha[:10]}.pdf")
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists():
                    shutil.copy2(source, destination)
                import_status = "IMPORTED"
            now = datetime.now(timezone.utc).isoformat()
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            backup = self.root / "audit" / f"classification_index.before_acquisition_import_{stamp}.jsonl"
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.index_path, backup)
            previous = {key: row.get(key) for key in ("source_path", "source_sha256", "acquisition_status")}
            row["manual_source_import"] = {
                "origin": str(source), "imported_at": now, "sha256": sha,
                "status": import_status, "previous_source": previous,
            }
            for key in ("source_path", "local_pdf_path", "pdf_path", "path"):
                row[key] = str(destination)
            row.update({
                "source_sha256": sha, "source_size_bytes": destination.stat().st_size,
                "acquisition_status": "DOWNLOADED_PDF", "metadata_only": False,
                "paper_crawler_status": "STAGING",
            })
            row.pop("acquisition_error", None)
            if row.get("manual_review_decision") == "RETRY_ACQUISITION":
                row.pop("manual_review_decision", None)
                row["manual_acquisition_retry_resolved_at"] = now
            temporary = self.index_path.with_suffix(".jsonl.acquisition.tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                for item in rows:
                    handle.write(json.dumps(item, ensure_ascii=False) + "\n")
            temporary.replace(self.index_path)
            sidecar = Path(str(row.get("classification_path") or ""))
            if sidecar.is_file():
                sidecar.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
            result = {
                "document_id": document_id, "title": row.get("title"), "status": import_status,
                "source_path": str(destination), "sha256": sha, "backup": str(backup), "pages": inspected.get("pages"),
            }
            audit = self.root / "audit" / "manual_acquisition_imports.jsonl"
            with audit.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({**result, "imported_at": now, "origin": str(source)}, ensure_ascii=False) + "\n")
            return result
