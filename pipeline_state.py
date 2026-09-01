#!/usr/bin/env python3
"""Small atomic checkpoint store for the seven-stage handoff pipeline."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STAGES = (
    "free_discovery", "institutional_discovery", "pdf_enrichment",
    "initial_classification", "gap_discovery", "reclassification", "source_audit", "handoff",
)


class PipelineState:
    def __init__(self, output_dir: str | Path, *, fresh: bool = False) -> None:
        self.path = Path(output_dir) / "audit" / "pipeline_state.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and not fresh:
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.data = self._new()
        else:
            self.data = self._new()
            self.save()

    @staticmethod
    def _new() -> dict[str, Any]:
        return {
            "schema_version": "1.0", "run_id": uuid.uuid4().hex,
            "updated_at": None, "stages": {stage: "NOT_STARTED" for stage in STAGES},
        }

    def save(self) -> None:
        self.data["updated_at"] = datetime.now(timezone.utc).isoformat()
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def status(self, stage: str) -> str:
        return str((self.data.get("stages") or {}).get(stage) or "NOT_STARTED")

    def completed(self, stage: str) -> bool:
        return self.status(stage) == "COMPLETED"

    def mark(self, stage: str, status: str, **details: Any) -> None:
        if status not in {"NOT_STARTED", "RUNNING", "PARTIAL", "COMPLETED", "FAILED"}:
            raise ValueError(f"invalid pipeline stage status: {status}")
        self.data.setdefault("stages", {})[stage] = status
        self.data.setdefault("stage_details", {})[stage] = details
        self.save()

    def run(self, stage: str, fn: Any) -> Any:
        if self.completed(stage):
            return None
        self.mark(stage, "RUNNING")
        try:
            value = fn()
        except KeyboardInterrupt:
            self.mark(stage, "PARTIAL", reason="KeyboardInterrupt")
            raise
        except Exception as exc:
            self.mark(stage, "FAILED", reason=type(exc).__name__, message=str(exc)[:500])
            raise
        self.mark(stage, "COMPLETED")
        return value

    def bootstrap_legacy(self) -> dict[str, str]:
        """Adopt a pre-checkpoint run after validating its durable artifacts."""
        root = self.path.parent.parent
        audit = root / "audit"

        def valid_json(path: Path) -> bool:
            try:
                return isinstance(json.loads(path.read_text(encoding="utf-8")), dict)
            except (OSError, ValueError):
                return False

        def valid_jsonl(path: Path) -> int:
            count = 0
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        if not isinstance(json.loads(line), dict):
                            return 0
                        count += 1
            except (OSError, ValueError):
                return 0
            return count

        discovery_rows = valid_jsonl(root / "discovery_catalog.jsonl")
        classification_rows = valid_jsonl(root / "classification_index.jsonl")
        classification_audit = {}
        try:
            classification_audit = json.loads((audit / "classification_audit.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass

        if discovery_rows and valid_json(audit / "discovery_audit.json"):
            self.mark("free_discovery", "COMPLETED", adopted_from_legacy=True, rows=discovery_rows)
            # Supplemental discovery had no standalone audit in the legacy format;
            # the combined discovery catalog is its durable result.
            self.mark("institutional_discovery", "COMPLETED", adopted_from_legacy=True, evidence="combined_discovery_catalog")
        if valid_json(audit / "light_pdf_extract_audit.json"):
            self.mark("pdf_enrichment", "COMPLETED", adopted_from_legacy=True)
        expected = int(classification_audit.get("documents") or 0) if isinstance(classification_audit, dict) else 0
        if classification_rows and expected == classification_rows:
            self.mark("initial_classification", "COMPLETED", adopted_from_legacy=True, rows=classification_rows)
            self.mark("gap_discovery", "PARTIAL", adopted_from_legacy=True, reason="legacy_run_interrupted_during_gap")
            self.mark("reclassification", "NOT_STARTED", adopted_from_legacy=True)
            self.mark("source_audit", "NOT_STARTED", adopted_from_legacy=True, reason="stale_after_initial_classification")
            self.mark("handoff", "NOT_STARTED", adopted_from_legacy=True)
        self.data["legacy_bootstrap"] = True
        self.save()
        return {stage: self.status(stage) for stage in STAGES}
