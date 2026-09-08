#!/usr/bin/env python3
"""Loopback-only manual review UI for PaperCrawler reclassification records."""

from __future__ import annotations

import argparse
import json
import mimetypes
import shutil
import sqlite3
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import corpus_policy
import handoff_export
from manual_acquisition import AcquisitionStore

ROOT = Path(__file__).resolve().parent
UI_ROOT = ROOT / "review_ui"
DECISIONS = {"ACCEPT", "REJECT", "RETRY_ACQUISITION", "KEEP_REVIEW"}
RELEVANCE = {"STRONG", "PROBABLE", "WEAK", "IRRELEVANT"}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict): rows.append(value)
    return rows


class ReviewStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.queue_path = self.root / "exports" / "TunnelBookAI_Source_Pack" / "99_audit" / "reclassify_queue.jsonl"
        self.db_path = self.root / "audit" / "manual_review_decisions.sqlite3"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.db.execute("""CREATE TABLE IF NOT EXISTS decisions (
            document_id TEXT PRIMARY KEY, decision TEXT NOT NULL, relevance_status TEXT,
            document_type TEXT, notes TEXT, updated_at TEXT NOT NULL, applied_at TEXT
        )""")
        self.db.commit()

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def records(self) -> list[dict[str, Any]]:
        decisions = {row["document_id"]: dict(row) for row in self.db.execute("SELECT * FROM decisions")}
        classifications = _read_jsonl(self.root / "classification_index.jsonl")
        by_key: dict[str, dict[str, Any]] = {}
        for row in classifications:
            for key in ("document_key", "canonical_id", "doi", "source_sha256"):
                if row.get(key): by_key[str(row[key])] = row
        output = []
        for position, row in enumerate(_read_jsonl(self.queue_path), 1):
            item = dict(row)
            document_id = str(item.get("document_id") or item.get("canonical_id") or f"row:{position}")
            source_row = by_key.get(document_id)
            if source_row:
                item = {**source_row, **item}
            item["document_id"] = document_id
            item["review"] = decisions.get(document_id)
            item["position"] = position
            source = item.get("source_path")
            item["source_available"] = bool(source and Path(str(source)).is_file())
            output.append(item)
        return output

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        document_id = str(payload.get("document_id") or "").strip()
        decision = str(payload.get("decision") or "").upper()
        relevance = str(payload.get("relevance_status") or "").upper()
        document_type = str(payload.get("document_type") or "").upper()[:80]
        notes = str(payload.get("notes") or "")[:2000]
        if not document_id or decision not in DECISIONS:
            raise ValueError("Geçersiz kayıt veya karar")
        if relevance and relevance not in RELEVANCE:
            raise ValueError("Geçersiz relevance değeri")
        now = datetime.now(timezone.utc).isoformat()
        with self.lock:
            self.db.execute("""INSERT INTO decisions VALUES (?,?,?,?,?,?,NULL)
                ON CONFLICT(document_id) DO UPDATE SET decision=excluded.decision,
                relevance_status=excluded.relevance_status, document_type=excluded.document_type,
                notes=excluded.notes, updated_at=excluded.updated_at, applied_at=NULL""",
                (document_id, decision, relevance or None, document_type or None, notes, now))
            self.db.commit()
        return {"document_id":document_id,"decision":decision,"relevance_status":relevance,"document_type":document_type,"notes":notes,"updated_at":now,"applied_at":None}

    @staticmethod
    def _matches(row: dict[str, Any], document_id: str) -> bool:
        values = {str(row.get(key) or "") for key in ("document_key", "canonical_id", "doi", "source_sha256")}
        return document_id in values

    def apply(self) -> dict[str, Any]:
        index_path = self.root / "classification_index.jsonl"
        rows = _read_jsonl(index_path)
        pending = [dict(row) for row in self.db.execute("SELECT * FROM decisions WHERE applied_at IS NULL")]
        if not pending:
            return {"applied":0,"backup":None,"counts":{}}
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = self.root / "audit" / f"classification_index.before_manual_{stamp}.jsonl"
        shutil.copy2(index_path, backup)
        counts: dict[str, int] = {}
        applied_ids: list[str] = []
        for decision in pending:
            row = next((item for item in rows if self._matches(item, decision["document_id"])), None)
            if row is None: continue
            action = decision["decision"]
            row["manual_review_decision"] = action
            row["manual_review"] = {
                "decision": action, "relevance_status": decision.get("relevance_status"),
                "document_type": decision.get("document_type"), "notes": decision.get("notes"),
                "reviewed_at": decision.get("updated_at"), "reviewer": "local_manual_review",
            }
            if action == "ACCEPT":
                row["classification_status"] = "MANUAL_ACCEPTED"
                row["classification_confidence"] = 1.0
                row["manual_relevance_status"] = decision.get("relevance_status") or "PROBABLE"
                if decision.get("document_type"): row["document_type"] = decision["document_type"]
            elif action == "REJECT":
                row["classification_status"] = "REJECT_IRRELEVANT"
                row["classification_confidence"] = 1.0
                row["manual_relevance_status"] = "IRRELEVANT"
            elif action == "KEEP_REVIEW":
                row["classification_status"] = "NEEDS_REVIEW"
                row["manual_relevance_status"] = decision.get("relevance_status") or "WEAK"
            elif action == "RETRY_ACQUISITION":
                row["manual_relevance_status"] = decision.get("relevance_status") or "PROBABLE"
            row["normalized_document_type"] = corpus_policy.normalized_document_type(row)
            sidecar_value = row.get("classification_path")
            if sidecar_value and Path(str(sidecar_value)).is_file():
                Path(str(sidecar_value)).write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
            counts[action] = counts.get(action, 0) + 1
            applied_ids.append(decision["document_id"])
        temporary = index_path.with_suffix(".jsonl.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            for row in rows: handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        temporary.replace(index_path)
        now = datetime.now(timezone.utc).isoformat()
        with self.lock:
            self.db.executemany("UPDATE decisions SET applied_at=? WHERE document_id=?", [(now, value) for value in applied_ids])
            self.db.commit()
        report = {"applied":len(applied_ids),"backup":str(backup),"counts":counts,"applied_at":now}
        (self.root / "audit" / "manual_review_apply_audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report


class ReviewHandler(BaseHTTPRequestHandler):
    store: ReviewStore
    acquisition: AcquisitionStore

    def _json(self, value: Any, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _body(self) -> dict[str, Any]:
        length = min(int(self.headers.get("Content-Length") or 0), 100_000)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/acquisition/records":
            query = parse_qs(parsed.query)
            search = str(query.get("search", [""])[0]).casefold()
            state = str(query.get("state", ["pending"])[0])
            limit = min(1000, max(1, int(query.get("limit", [1000])[0])))
            all_rows = self.acquisition.records()
            scan = self.acquisition.suggestions(all_rows)
            suggestions = scan["suggestions"]
            rows = list(all_rows)
            for row in rows:
                row["download_match"] = suggestions.get(row["document_id"])
            if search:
                rows = [row for row in rows if search in " ".join(str(row.get(key) or "") for key in
                        ("title", "document_id", "doi", "reason", "source_url", "pdf_url", "discovery_source")).casefold()]
            if state == "pending": rows = [row for row in rows if row["manual_download_eligible"] and not row["acquisition_resolved"]]
            elif state == "matched": rows = [row for row in rows if row["manual_download_eligible"] and row.get("download_match") and not row["acquisition_resolved"]]
            elif state == "resolved": rows = [row for row in rows if row["manual_download_eligible"] and row["acquisition_resolved"]]
            elif state == "excluded": rows = [row for row in rows if not row["manual_download_eligible"]]
            elif state == "all": rows = [row for row in rows if row["manual_download_eligible"]]
            eligible = [row for row in all_rows if row["manual_download_eligible"]]
            summary = {
                "TOTAL": len(all_rows),
                "ELIGIBLE": sum(not row["acquisition_resolved"] for row in eligible),
                "HIDDEN": sum(not row["manual_download_eligible"] for row in all_rows),
                "HIDDEN_IRRELEVANT": sum("IRRELEVANT_OR_WEAK" in row["manual_download_exclusion_reasons"] for row in all_rows),
                "HIDDEN_MANUAL": sum("MANUAL_EXCLUDED" in row["manual_download_exclusion_reasons"] for row in all_rows),
                "HIDDEN_NO_SITE": sum("NO_DOWNLOAD_SITE" in row["manual_download_exclusion_reasons"] for row in all_rows),
                "SITES": len({row["download_domain"] for row in eligible if row["download_domain"]}),
                "MATCHED": sum(row["document_id"] in suggestions and row["manual_download_eligible"] and not row["acquisition_resolved"] for row in all_rows),
                "RESOLVED": sum(row["acquisition_resolved"] for row in eligible),
                "FILES": len(scan["files"]),
            }
            return self._json({"records": rows[:limit], "total": len(rows), "summary": summary,
                               "files": scan["files"], "downloads_dir": scan["downloads_dir"]})
        if parsed.path == "/api/acquisition/files":
            return self._json(self.acquisition.suggestions())
        if parsed.path == "/api/records":
            query = parse_qs(parsed.query); search = str(query.get("search", [""])[0]).casefold()
            state = str(query.get("state", ["all"])[0]); offset = max(0, int(query.get("offset", [0])[0])); limit = min(1000, max(1, int(query.get("limit", [25])[0])))
            rows = self.store.records()
            if search: rows = [r for r in rows if search in " ".join(str(r.get(k) or "") for k in ("title","document_id","topics","reason","source_url")).casefold()]
            if state == "pending": rows = [r for r in rows if not r.get("review")]
            elif state == "decided": rows = [r for r in rows if r.get("review")]
            decisions = {}
            for r in self.store.records():
                key = (r.get("review") or {}).get("decision") or "PENDING"; decisions[key] = decisions.get(key, 0) + 1
            return self._json({"records":rows[offset:offset+limit],"total":len(rows),"offset":offset,"limit":limit,"summary":decisions})
        if parsed.path == "/api/source":
            document_id = str(parse_qs(parsed.query).get("id", [""])[0])
            row = next((r for r in self.store.records() if r["document_id"] == document_id), None)
            path = Path(str(row.get("source_path"))).resolve() if row and row.get("source_path") else None
            if not path or not path.is_file() or not path.is_relative_to(self.store.root): return self.send_error(404)
            if path.suffix.lower() not in {".pdf", ".md", ".txt"}: return self.send_error(415)
            body = path.read_bytes()
            mime = "application/pdf" if path.suffix.lower() == ".pdf" else "text/plain; charset=utf-8"
            self.send_response(200); self.send_header("Content-Type", mime); self.send_header("Content-Length", str(len(body)))
            if path.suffix.lower() != ".pdf": self.send_header("Content-Security-Policy", "sandbox")
            self.send_header("Content-Disposition", f'inline; filename="{path.name.replace(chr(34), "")}"')
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers(); self.wfile.write(body); return
        if parsed.path in {"/downloads", "/downloads/"}:
            asset = "downloads.html"
        else:
            asset = "index.html" if parsed.path == "/" else parsed.path.lstrip("/")
        path = (UI_ROOT / asset).resolve()
        if not path.is_file() or not path.is_relative_to(UI_ROOT.resolve()): return self.send_error(404)
        body = path.read_bytes(); mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200); self.send_header("Content-Type", mime); self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        try:
            if self.path == "/api/acquisition/import":
                payload = self._body()
                return self._json(self.acquisition.import_pdf(
                    str(payload.get("document_id") or ""), str(payload.get("file") or "")))
            if self.path == "/api/acquisition/exclude":
                payload = self._body()
                return self._json(self.acquisition.set_excluded(
                    str(payload.get("document_id") or ""), bool(payload.get("excluded", True))))
            if self.path == "/api/acquisition/rebuild":
                report = handoff_export.export_handoff(self.store.root)
                return self._json({key: report.get(key) for key in (
                    "ready_for_handoff", "retry_acquisition", "reclassify", "rejected", "metadata_references")})
            if self.path == "/api/decision": return self._json(self.store.save(self._body()))
            if self.path == "/api/apply": return self._json(self.store.apply())
            if self.path == "/api/rebuild": return self._json(handoff_export.export_handoff(self.store.root))
            return self.send_error(404)
        except (ValueError, json.JSONDecodeError) as exc:
            return self._json({"error":str(exc)}, 400)

    def log_message(self, fmt: str, *args: Any) -> None:
        print("[review-ui] " + fmt % args, flush=True)


def serve(root: str | Path, host: str = "127.0.0.1", port: int = 8765,
          downloads_dir: str | Path | None = None) -> None:
    ReviewHandler.store = ReviewStore(Path(root))
    ReviewHandler.acquisition = AcquisitionStore(Path(root), Path(downloads_dir or Path.home() / "Downloads" / "makaleler"))
    server = ThreadingHTTPServer((host, port), ReviewHandler)
    print(f"Manual review UI: http://{host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="tunel_makaleleri")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--downloads-dir", default=str(Path.home() / "Downloads" / "makaleler"))
    args = parser.parse_args(); serve(args.output_dir, args.host, args.port, args.downloads_dir)
