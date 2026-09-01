#!/usr/bin/env python3
"""Focused second discovery pass for under-covered TunnelBookAI sections."""

from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path
from typing import Any

import yaml

import free_discovery as discovery
import source_health
import tunnel_harvest as harvest


def _log(message: str) -> None:
    print(f"[gap] {message}", flush=True)

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
    detailed = ((audit.get("coverage") or {}).get("sections") or {})
    coverage = {
        sid: int(values.get("handoff_candidates", values.get("corpus_eligible_count")) or 0)
        for sid, values in detailed.items() if isinstance(values, dict)
    } or audit.get("handoff_candidate_section_coverage") or audit.get("section_coverage") or {}
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


def ensure_domain_anchor(query: str, section_context: str = "") -> str:
    text = " ".join(str(query or "").split())
    lowered = text.casefold()
    anchors = [str(value).casefold() for value in (discovery.relevance._policy().get("tunnel_anchors") or [])]
    if any(anchor in lowered for anchor in anchors):
        return text
    turkish = any(token in lowered for token in ("bakım", "işlet", "maliyet", "yapım", "uzunluk", "trafik", "havalandırma", "aydınlatma"))
    anchor = "karayolu tüneli" if turkish else "road tunnel"
    context = str(section_context or "").casefold()
    if "operation" in context and "operation" not in lowered:
        text = "operation " + text
    return f"{anchor} {text}".strip()


def _queries_for_section(section_id: str, max_queries: int) -> list[str]:
    taxonomy = _yaml("taxonomy.yaml")
    cfg = (taxonomy.get("sections") or {}).get(section_id) or {}
    values = [*(cfg.get("strong_terms") or []), *(cfg.get("medium_terms") or [])]
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        query = ensure_domain_anchor(str(value).strip(), str(cfg.get("title") or ""))
        if len(query) < 4 or query.lower() in seen:
            continue
        seen.add(query.lower())
        out.append(query)
        if len(out) >= max_queries:
            break
    return out


def _discover_with_retries(query: str, per_source: int, attempts: int = 3, health_registry: source_health.SourceHealthRegistry | None = None) -> tuple[list[discovery.DiscoveryRecord], list[str]]:
    errors: list[str] = []
    for attempt in range(1, attempts + 1):
        batch, batch_errors = discovery.discover_academic(query, per_source=per_source, health_registry=health_registry)
        if batch or not batch_errors or attempt == attempts:
            return batch, [*errors, *batch_errors]
        errors.extend(batch_errors)
        delay = min(4.0, 0.5 * (2 ** (attempt - 1)) + random.uniform(0, 0.2))
        _log(f"query={query!r} retry={attempt + 1}/{attempts} after {delay:.1f}s")
        time.sleep(delay)
    return [], errors


def run_gap_discovery(
    output_dir: str | Path | None = None, *, embedding_server: str | None = None,
    embedding_model: str | None = None, use_local_embedding: bool = True,
) -> dict[str, Any]:
    if output_dir is not None:
        harvest.set_output_dir(output_dir)
    root = harvest.OUTPUT_DIR
    audit_path = root / "audit" / "classification_audit.json"
    if not audit_path.exists():
        raise FileNotFoundError("classification_audit.json not found; run classify_catalog.py first")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    health_registry = source_health.SourceHealthRegistry(root / "audit" / "source_health.json")
    embedding_client, selected_embedding_model = discovery.hybrid.detect_local_embedding(embedding_server, embedding_model) if use_local_embedding else (None, None)
    gaps = section_gaps(audit)
    cfg = _yaml("coverage_targets.yaml").get("gap_search") or {}
    max_sections = int(cfg.get("max_sections_per_pass") or 12)
    queries_per_section = int(cfg.get("queries_per_section") or 4)
    per_source = int(cfg.get("per_source") or 12)
    max_candidates = int(cfg.get("max_new_candidates_per_section") or 30)
    max_downloads = int(cfg.get("max_downloads_per_section") or 8)
    acquire = bool(cfg.get("acquire", True))
    selected = gaps[:max_sections]
    _log(f"coverage gaps={len(gaps)} selected_sections={len(selected)}")

    new_records: list[discovery.DiscoveryRecord] = []
    errors: list[str] = []
    query_log: list[dict[str, Any]] = []
    query_cache: dict[str, tuple[list[discovery.DiscoveryRecord], list[str]]] = {}
    combined_terms: list[str] = []
    for gap in selected:
        sid = str(gap["section_id"])
        _log(f"section={sid} current={gap['current']} target={gap['target']}")
        queries = _queries_for_section(sid, queries_per_section)
        combined_terms.extend(queries)
        for query in queries:
            _log(f"section={sid} query={query!r}")
            normalized_query = " ".join(query.casefold().split())
            cached = normalized_query in query_cache
            if cached:
                batch, batch_errors = query_cache[normalized_query]
            else:
                batch, batch_errors = _discover_with_retries(query, per_source=per_source, health_registry=health_registry)
                query_cache[normalized_query] = (batch, batch_errors)
            new_records.extend(batch[:max_candidates])
            errors.extend(batch_errors)
            query_log.append({"section_id": sid, "query": query, "records_seen": len(batch), "relevant": 0, "acquired": 0, "unique_additions": 0, "provider_call_reused": cached})

    institutional = discovery._config().get("institutional_sources") or {}
    unique_terms = list(dict.fromkeys(combined_terms))[:12]
    for name, source_cfg in institutional.items():
        if not source_cfg.get("enabled", False) or not source_cfg.get("internal_search_urls"):
            continue
        batch, batch_errors = discovery.crawl_seed_source(name, source_cfg, terms=unique_terms)
        new_records.extend(batch)
        errors.extend(batch_errors)

    new_records, rejected_irrelevant = discovery.filter_relevant_records(
        new_records, embedding_client=embedding_client, embedding_model=selected_embedding_model,
        profile_vectors={},
    )
    new_records = discovery.deduplicate(new_records)
    new_records.sort(key=lambda row: (
        float(row.tunnel_relevance_score or 0.0), bool(row.pdf_url), bool(row.doi),
    ), reverse=True)
    existing = _read_jsonl(root / "discovery_catalog.jsonl")
    existing_keys = {_key(row) for row in existing}
    discovery_root = root / "discovery_sources"
    additions: list[dict[str, Any]] = []
    download_budget = max_downloads * max(1, len(selected))
    downloads_requested = 0
    for record in new_records:
        if record.key() in existing_keys:
            continue
        should_acquire = acquire and downloads_requested < download_budget
        payload = discovery.acquire_record(record, discovery_root) if should_acquire else record.as_dict() | {"acquisition_status": "METADATA_RANKED"}
        downloads_requested += int(should_acquire)
        additions.append(payload)
        existing_keys.add(_key(payload))

    for item in query_log:
        query = str(item["query"]).casefold()
        relevant = [row for row in new_records if str(row.discovery_query or "").casefold() == query]
        added = [row for row in additions if str(row.get("discovery_query") or "").casefold() == query]
        item["relevant"] = len(relevant)
        item["unique_additions"] = len(added)
        item["acquired"] = sum(1 for row in added if row.get("acquisition_status") in {"DOWNLOADED_PDF", "SNAPSHOTTED_WEB"})

    merged = [*existing, *additions]
    _write_jsonl(root / "discovery_catalog.jsonl", merged)
    report = {
        "schema_version": "1.2",
        "gap_search_completed": True,
        "coverage_basis": "handoff_candidates",
        "sections_below_target": len(gaps),
        "sections_searched": selected,
        "queries": query_log,
        "new_unique_discoveries": len(new_records),
        "metadata_candidates": len(new_records),
        "downloads_requested": downloads_requested,
        "rejected_irrelevant": rejected_irrelevant,
        "catalog_additions": len(additions),
        "catalog_total": len(merged),
        "errors": errors,
        "provider_degraded": sorted({error.partition(":")[0] for error in errors}),
        "source_health": health_registry.data,
        "relevance_embedding_model": selected_embedding_model,
        "gap_additions_reason": "network_degraded" if not additions and errors else ("no_new_relevant_records" if not additions else "added"),
        "next_step": "rerun classify_catalog.py, then inspect handoff-candidate coverage again",
    }
    audit_dir = root / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "gap_discovery_audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"finished additions={len(additions)} catalog_total={len(merged)}")
    _log(
        f"summary sections_with_gap={len(gaps)} metadata_candidates={len(new_records)} "
        f"relevance_passed={len(new_records)} downloads_requested={downloads_requested} "
        f"downloads_successful={sum(1 for row in additions if row.get('acquisition_status') == 'DOWNLOADED_PDF')}"
    )
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search under-covered book sections with free sources.")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--rules-only", action="store_true")
    parser.add_argument("--embedding-server", default=None)
    parser.add_argument("--embedding-model", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    print(json.dumps(run_gap_discovery(
        args.output_dir, embedding_server=args.embedding_server, embedding_model=args.embedding_model,
        use_local_embedding=not args.rules_only,
    ), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
