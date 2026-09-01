#!/usr/bin/env python3
"""Enrich harvested PDFs that lack abstracts with lightweight local text extraction.

This is deliberately not a replacement for TunnelBookAI/Docling. It reads only the
first few PDF pages and extracts enough text for provisional classification.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from pypdf import PdfReader

import tunnel_harvest as harvest


def _log(message: str) -> None:
    print(f"[pdf-enrich] {message}", flush=True)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def extract_first_pages(path: Path, max_pages: int = 3, max_chars: int = 12000) -> dict[str, Any]:
    result: dict[str, Any] = {
        "light_text": "",
        "pages_read": 0,
        "text_layer_available": False,
        "extraction_error": None,
    }
    try:
        reader = PdfReader(str(path), strict=False)
        parts: list[str] = []
        for page in reader.pages[:max_pages]:
            text = page.extract_text() or ""
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                parts.append(text)
            result["pages_read"] += 1
            if sum(len(p) for p in parts) >= max_chars:
                break
        combined = "\n".join(parts)[:max_chars]
        result["light_text"] = combined
        result["text_layer_available"] = bool(combined.strip())
    except Exception as exc:  # noqa: BLE001
        result["extraction_error"] = str(exc)
    return result


def _source_path(row: dict[str, Any]) -> Path | None:
    for key in ("source_path", "local_pdf_path", "pdf_path", "path"):
        value = row.get(key)
        if value:
            path = Path(str(value)).expanduser()
            if path.exists() and path.suffix.lower() == ".pdf":
                return path
    return None


def enrich_catalog(output_dir: str | Path | None = None, *, max_pages: int = 3) -> dict[str, Any]:
    if output_dir is not None:
        harvest.set_output_dir(output_dir)
    root = harvest.OUTPUT_DIR
    paths = [root / "discovery_catalog.jsonl"]
    enriched = 0
    no_text_layer = 0
    skipped_with_abstract = 0
    errors: list[dict[str, str]] = []
    metrics = {key: 0 for key in (
        "total_candidates", "pdf_candidates", "downloaded_pdfs", "valid_pdfs", "corrupt_pdfs",
        "extracted_full_text", "abstract_only", "webpage_text", "title_only", "extraction_errors", "no_text",
    )}

    for catalog_path in paths:
        rows = _read_jsonl(catalog_path)
        if not rows:
            continue
        _log(f"catalog={catalog_path} rows={len(rows)}")
        metrics["total_candidates"] += len(rows)
        changed = False
        for row in rows:
            path = _source_path(row)
            if path:
                metrics["pdf_candidates"] += 1
                metrics["downloaded_pdfs"] += 1
            if str(row.get("abstract") or "").strip():
                skipped_with_abstract += 1
                metrics["abstract_only"] += 1
                if path:
                    metrics["valid_pdfs"] += 1
                continue
            if not path:
                if row.get("raw_html_path"):
                    metrics["webpage_text"] += 1
                else:
                    metrics["title_only"] += 1
                continue
            extraction = extract_first_pages(path, max_pages=max_pages)
            row["light_pdf_extraction"] = {
                "pages_read": extraction["pages_read"],
                "text_layer_available": extraction["text_layer_available"],
                "extraction_error": extraction["extraction_error"],
            }
            if extraction["light_text"]:
                row["abstract"] = extraction["light_text"]
                row["classification_input"] = "LIGHT_PDF_FIRST_PAGES"
                enriched += 1
                metrics["extracted_full_text"] += 1
                metrics["valid_pdfs"] += 1
                changed = True
            else:
                row["classification_input"] = "TITLE_METADATA_ONLY"
                row["needs_tunnelbookai_fulltext_review"] = True
                no_text_layer += 1
                metrics["no_text"] += 1
                changed = True
            if extraction["extraction_error"]:
                errors.append({"path": str(path), "error": str(extraction["extraction_error"])})
                metrics["extraction_errors"] += 1
                metrics["corrupt_pdfs"] += 1
            processed = enriched + no_text_layer
            if processed and processed % 25 == 0:
                _log(f"processed={processed} enriched={enriched} no_text={no_text_layer}")
        if changed:
            _write_jsonl(catalog_path, rows)

    report = {
        "schema_version": "1.0",
        "enriched": enriched,
        "no_text_layer": no_text_layer,
        "skipped_with_abstract": skipped_with_abstract,
        "errors": errors,
        **metrics,
        "semantic_boundary": "Light extraction is provisional classification input only; TunnelBookAI still runs full Docling conversion.",
    }
    audit = root / "audit"
    audit.mkdir(parents=True, exist_ok=True)
    (audit / "light_pdf_extract_audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"finished enriched={enriched} no_text={no_text_layer} errors={len(errors)}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--max-pages", type=int, default=3)
    args = parser.parse_args()
    print(json.dumps(enrich_catalog(args.output_dir, max_pages=args.max_pages), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
