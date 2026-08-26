#!/usr/bin/env python3
"""Focused second discovery pass for under-covered TunnelBookAI sections."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

import free_discovery as discovery
import tunnel_harvest as harvest

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT / "config"


def _yaml(name: str) -> dict[str, Any]:
    with (CONFIG_DIR / name).open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    return payload if isinstance(payload, dict) else {}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _key(row: dict[str, Any]) -> str:
    doi = harvest.normalize_doi(row.get("doi"))
    if doi:
        return "doi:" + doi.lower()
    if row.get("discovery_key"):
        return str(row["discovery_key"])
    if row.get("source_sha256"):
        return "sha:" + str(row["source_sha256"])
    for field in ("pdf_url", "source_url", "landing_url", "source_path"):
        if row.get(field):
            return field + ":" + str(row[field])
    return "title:" + str(row.get("title") or "").lower()


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def section_gaps(audit: dict[str, Any]) -> list[dict[str, Any]]:
    cfg = _yaml("coverage_targets.yaml")
    coverage = audit.get("handoff_candidate_section_coverage") or audit.get("section_coverage") or {}
    gaps: list[dict[str, Any]] = []
    for sid, target in (cfg.get("sections") or {}).items():
        current = int(coverage.get(str(sid)) or 0)
        target_n = int(target or 0)
        if current < target_n:
            gaps.append({
                "section_id": str(sid),
                "current": current,
                "target": target_n,
                "deficit": target_n - current,
                "coverage_ratio": round(current / target_n, 4) if target_n else 1.0,
            })
    gaps.sort(key=lambda row: (row["coverage_ratio"], -row["deficit"]))
    return gaps


def _queries_for_section(section_id: str, max_queries: int) -> list[str]:
    taxonomy = _yaml("taxonomy.yaml")
    cfg = (taxonomy.get("sections") or {}).get(section_id) or {}
    values = [*(cfg.get("strong_terms") or []), *(cfg.get("medium_terms") or [])]
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        query = str(value).strip()
        if len(query) < 4 or query.lower() in seen:
            continue
        seen.add(query.lower())
        out.append(query)
        if len(out) >= max_queries:
            break
    return out


def run_gap_discovery(output_dir: str | Path | None = None) -> dict[str, Any]:
    if output_dir is not None:
        harvest.set_output_dir(output_dir)
    root = harvest.OUTPUT_DIR
    audit_path = root / "audit" / "classification_audit.json"
    if not audit_path.exists():
        raise FileNotFoundError("classification_audit.json not found; run classify_catalog.py first")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    gaps = section_gaps(audit)
    cfg = _yaml("coverage_targets.yaml").get("gap_search") or {}
    max_sections = int(cfg.get("max_sections_per_pass") or 12)
    queries_per_section = int(cfg.get("queries_per_section") or 4)
    per_source = int(cfg.get("per_source") or 12)
    acquire = bool(cfg.get("acquire", True))
    selected = gaps[:max_sections]

    new_records: list[discovery.DiscoveryRecord] = []
    errors: list[str] = []
    query_log: list[dict[str, Any]] = []
    combined_terms: list[str] = []
    for gap in selected:
        sid = str(gap["section_id"])
        queries = _queries_for_section(sid, queries_per_section)
        combined_terms.extend(queries)
        for query in queries:
            batch, batch_errors = discovery.discover_academic(query, per_source=per_source)
            new_records.extend(batch)
            errors.extend(batch_errors)
            query_log.append({"section_id": sid, "query": query, "results": len(batch)})

    institutional = discovery._config().get("institutional_sources") or {}
    unique_terms = list(dict.fromkeys(combined_terms))[:12]
    for name, source_cfg in institutional.items():
        if not source_cfg.get("enabled", False) or not source_cfg.get("internal_search_urls"):
            continue
        batch, batch_errors = discovery.crawl_seed_source(name, source_cfg, terms=unique_terms)
        new_records.extend(batch)
        errors.extend(batch_errors)

    new_records = discovery.deduplicate(new_records)
    existing = _read_jsonl(root / "discovery_catalog.jsonl")
    existing_keys = {_key(row) for row in existing}
    discovery_root = root / "discovery_sources"
    additions: list[dict[str, Any]] = []
    for record in new_records:
        if record.key() in existing_keys:
            continue
        payload = discovery.acquire_record(record, discovery_root) if acquire else record.as_dict() | {"acquisition_status": "NOT_REQUESTED"}
        additions.append(payload)
        existing_keys.add(_key(payload))

    merged = [*existing, *additions]
    _write_jsonl(root / "discovery_catalog.jsonl", merged)
    report = {
        "schema_version": "1.1",
        "coverage_basis": "handoff_candidate_section_coverage",
        "sections_below_target": len(gaps),
        "sections_searched": selected,
        "queries": query_log,
        "new_unique_discoveries": len(new_records),
        "catalog_additions": len(additions),
        "catalog_total": len(merged),
        "errors": errors,
        "next_step": "rerun classify_catalog.py, then inspect handoff-candidate coverage again",
    }
    audit_dir = root / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "gap_discovery_audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search under-covered book sections with free sources.")
    parser.add_argument("--output-dir", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    print(json.dumps(run_gap_discovery(args.output_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
