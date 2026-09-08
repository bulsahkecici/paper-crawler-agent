#!/usr/bin/env python3
"""Retry only unresolved queue records that can benefit from a local LLM call."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import corpus_policy
import hybrid_classifier as hybrid
from content_quality_control import SYSTEM_PROMPT, _explicit_tunnel_title, _source_content

REVISION = "targeted_reclassify_v1"


def _read(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _target(row: dict[str, Any]) -> bool:
    status = str(row.get("classification_status") or "").upper()
    review = row.get("llm_review") if isinstance(row.get("llm_review"), dict) else {}
    dtype = str(row.get("normalized_document_type") or row.get("document_type") or "unknown").lower()
    return bool(
        (status == "LOCAL_LLM_REVIEW" and not review.get("used"))
        or review.get("error")
        or (status == "LLM_ACCEPTED" and dtype == "unknown")
    )


def run(root: str | Path, *, server: str, model: str, workers: int = 2) -> dict[str, Any]:
    root = Path(root)
    index_path = root / "classification_index.jsonl"
    rows = _read(index_path)
    targets = [row for row in rows if _target(row)]
    client = hybrid.LocalOpenAIClient(server)
    selected = next((item for item in client.models() if model.casefold() in item.casefold()), None)
    if not selected:
        raise RuntimeError(f"LLM model not available: {model}")
    db_path = root / "audit" / "targeted_reclassify_checkpoint.sqlite3"
    db = sqlite3.connect(db_path, check_same_thread=False)
    db.execute("CREATE TABLE IF NOT EXISTS reviews (fingerprint TEXT PRIMARY KEY, payload TEXT NOT NULL)")
    db.commit()
    lock = threading.Lock()
    hits = 0

    def process(row: dict[str, Any]) -> dict[str, Any]:
        nonlocal hits
        content, extraction_error = _source_content(row, max_pages=5)
        if len(content.strip()) < 200:
            content = "\n".join(str(row.get(key) or "") for key in ("title", "abstract", "publisher", "source_url"))
        fingerprint = hashlib.sha256(f"{REVISION}|{selected}|{row.get('document_key')}|{content}".encode()).hexdigest()
        with lock:
            cached = db.execute("SELECT payload FROM reviews WHERE fingerprint=?", (fingerprint,)).fetchone()
        if cached:
            with lock: hits += 1
            return json.loads(cached[0])
        user = json.dumps({
            "title": row.get("title"), "content": content[:12000],
            "relevance": ["STRONG", "PROBABLE", "WEAK", "IRRELEVANT"],
            "document_type": sorted(hybrid.LLM_DOCUMENT_TYPES),
        }, ensure_ascii=False, separators=(",", ":"))
        try:
            data = client.chat_source_gate_json(selected, SYSTEM_PROMPT, user)
            result = {
                "relevance_status": str(data.get("relevance") or "WEAK").upper(),
                "document_type": str(data.get("document_type") or "UNKNOWN").upper(),
                "confidence": max(0.0, min(1.0, float(data.get("confidence") or 0))),
                "reason_code": str(data.get("reason_code") or "")[:80],
                "error": None, "extraction_error": extraction_error,
            }
        except Exception as exc:
            result = {"relevance_status":"WEAK", "document_type":"UNKNOWN", "confidence":0.0, "reason_code":"LOCAL_LLM_ERROR", "error":str(exc), "extraction_error":extraction_error}
        with lock:
            db.execute("INSERT OR REPLACE INTO reviews VALUES (?,?)", (fingerprint, json.dumps(result, ensure_ascii=False)))
            db.commit()
        return result

    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="targeted-reclassify") as executor:
        reviews = list(executor.map(process, targets))
    db.close()
    counts: dict[str, int] = {}
    for row, review in zip(targets, reviews):
        relevance = review["relevance_status"]
        confidence = float(review["confidence"])
        dtype = review["document_type"]
        if review.get("error"):
            status = "NEEDS_REVIEW"
        elif relevance in {"STRONG", "PROBABLE"} and confidence >= 0.72 and dtype != "UNKNOWN":
            status = "LLM_ACCEPTED"
        elif relevance == "IRRELEVANT" and confidence >= 0.72 and not _explicit_tunnel_title(row):
            status = "REJECT_IRRELEVANT"
        else:
            status = "NEEDS_REVIEW"
        row["classification_status"] = status
        row["classification_confidence"] = confidence
        row["llm_relevance_status"] = relevance
        if dtype != "UNKNOWN": row["document_type"] = dtype
        row["normalized_document_type"] = corpus_policy.normalized_document_type(row)
        row["llm_review"] = {"enabled":True,"used":True,"triggered":True,"trigger_reasons":["targeted_reclassify_queue"],"model":selected,**review}
        row["targeted_reclassification"] = {"revision":REVISION, **review}
        counts[status] = counts.get(status, 0) + 1
        sidecar_value = row.get("classification_path")
        if sidecar_value and Path(str(sidecar_value)).is_file():
            Path(str(sidecar_value)).write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary = index_path.with_suffix(".jsonl.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows: handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    temporary.replace(index_path)
    report = {"revision":REVISION,"targeted":len(targets),"checkpoint_hits":hits,"model":selected,"workers":max(1,workers),"status_counts":counts,"llm_errors":sum(bool(r.get("error")) for r in reviews)}
    (root / "audit" / "targeted_reclassify_audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="tunel_makaleleri")
    parser.add_argument("--llm-server", default=hybrid.DEFAULT_EMBEDDING_SERVER)
    parser.add_argument("--llm-model", default=hybrid.DEFAULT_LLM_MODEL)
    parser.add_argument("--llm-workers", type=int, default=2)
    args = parser.parse_args()
    print(json.dumps(run(args.output_dir, server=args.llm_server, model=args.llm_model, workers=args.llm_workers), ensure_ascii=False, indent=2))


if __name__ == "__main__": main()
