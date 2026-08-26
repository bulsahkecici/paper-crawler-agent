#!/usr/bin/env python3
"""Evaluate a small real-world PaperCrawler pilot and issue GO/NO-GO."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import tunnel_harvest as harvest


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def validate(output_dir: str | Path | None = None) -> dict[str, Any]:
    if output_dir is not None:
        harvest.set_output_dir(output_dir)
    root = harvest.OUTPUT_DIR
    discovery = _load(root / "audit" / "discovery_audit.json")
    classification = _load(root / "audit" / "classification_audit.json")
    dedup = _load(root / "audit" / "source_dedup_version_audit.json")
    handoff = _load(root / "exports" / "TunnelBookAI_Source_Pack" / "99_audit" / "handoff_audit.json")

    checks: list[dict[str, Any]] = []
    def add(name: str, ok: bool, detail: Any) -> None:
        checks.append({"check": name, "status": "PASS" if ok else "FAIL", "detail": detail})

    discovered = int(discovery.get("discovered_unique") or 0)
    add("discovery_nonempty", discovered > 0, discovered)
    add("no_paid_search_api", discovery.get("paid_search_apis_used") is False, discovery.get("paid_search_apis_used"))

    docs = int(classification.get("documents") or 0)
    add("classification_nonempty", docs > 0, docs)
    missing_section = int(classification.get("missing_section") or classification.get("missing_section_count") or 0)
    add("missing_section_bounded", docs == 0 or missing_section / max(docs, 1) <= 0.20, missing_section)

    ready = int(handoff.get("ready_for_handoff") or 0)
    add("handoff_nonempty", ready > 0, ready)
    rejections = handoff.get("rejections") or []
    checksum_errors = [r for r in rejections if str(r.get("reason")) == "sha256_mismatch"]
    add("no_checksum_mismatch", not checksum_errors, len(checksum_errors))

    fuzzy = int(dedup.get("fuzzy_review_pairs") or 0)
    add("fuzzy_duplicates_not_auto_merged", dedup.get("note") == "Fuzzy title/author/year candidates are never auto-merged.", fuzzy)

    decision = "GO" if all(row["status"] == "PASS" for row in checks) else "NO-GO"
    result = {
        "schema_version": "1.0",
        "decision": decision,
        "checks": checks,
        "recommendation": "Proceed to a larger harvest only after GO; NO-GO items must be reviewed first.",
    }
    audit = root / "audit"
    audit.mkdir(parents=True, exist_ok=True)
    (audit / "pilot_readiness_audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()
    print(json.dumps(validate(args.output_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
