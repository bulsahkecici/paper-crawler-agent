#!/usr/bin/env python3
"""Reproducible per-run manifests without credentials or corpus copies."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

AUDIT_FILES = (
    "run_summary.json", "run_summary.md", "provider_health.json", "source_health.json",
    "coverage.json", "source_dedup_version_audit.json", "duplicate_audit.json",
    "decision_summary.json", "handoff_quality_gate.json", "corpus_quality_gate.json",
)


def _git(*args: str) -> str | None:
    try:
        return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _jsonl_count(path: Path) -> int:
    try:
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    except OSError:
        return 0


def start(root: Path, *, models: dict[str, Any], query_count: int) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    run_id = now.strftime("RUN_%Y%m%d_%H%M%S")
    run_dir = root / "audit" / "runs" / run_id
    suffix = 1
    while run_dir.exists():
        run_dir = root / "audit" / "runs" / f"{run_id}_{suffix:02d}"
        suffix += 1
    run_dir.mkdir(parents=True)
    config_root = Path(__file__).resolve().parent / "config"
    config_hashes = {path.name: _hash(path) for path in sorted(config_root.glob("*.yaml"))}
    manifest = {
        "schema_version": "1.0", "run_id": run_dir.name,
        "started_at": now.isoformat(), "finished_at": None,
        "git_commit": _git("rev-parse", "HEAD"), "branch": _git("branch", "--show-current"),
        "configuration_hashes": config_hashes, "models": models,
        "query_count": query_count, "input_catalog_counts": {
            "discovery_catalog": _jsonl_count(root / "discovery_catalog.jsonl"),
            "classification_index": _jsonl_count(root / "classification_index.jsonl"),
        }, "output_counts": {},
    }
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"run_dir": run_dir, "manifest": manifest}


def finish(context: dict[str, Any], root: Path, *, summary: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    run_dir: Path = context["run_dir"]
    manifest = dict(context["manifest"])
    manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
    manifest["output_counts"] = {
        "current_catalog_records": _jsonl_count(root / "discovery_catalog.jsonl"),
        "current_classification_records": _jsonl_count(root / "classification_index.jsonl"),
        "current_handoff_records": int(decision.get("handoff_eligible") or 0),
    }
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    audit = root / "audit"
    package_audit = root / "exports" / "TunnelBookAI_Source_Pack" / "99_audit"
    for name in AUDIT_FILES:
        source = audit / name
        if not source.exists():
            source = package_audit / name
        if source.exists() and source.resolve() != (run_dir / name).resolve():
            shutil.copy2(source, run_dir / name)
    return manifest
