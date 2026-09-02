#!/usr/bin/env python3
"""Book-agnostic focused discovery by broad topic or TunnelBookAI request."""
from __future__ import annotations
import argparse, json
from pathlib import Path
from typing import Any
import free_discovery as discovery
import tunnel_harvest as harvest

def ensure_domain_anchor(query: str, section_context: str = "") -> str:
    text = " ".join(str(query or "").split()); lowered = text.casefold()
    anchors = [str(x).casefold() for x in discovery.relevance._policy().get("tunnel_anchors") or []]
    if any(x in lowered for x in anchors): return text
    tr = any(x in lowered for x in ("bakım","işlet","maliyet","yapım","havalandırma","aydınlatma"))
    if "operation" in str(section_context).casefold() and "operation" not in lowered: text = "operation " + text
    return f"{'karayolu tüneli' if tr else 'road tunnel'} {text}".strip()

def section_gaps(audit: dict[str, Any]) -> list[dict[str, Any]]:
    """Deprecated compatibility API: chapter gaps no longer exist."""
    return []

def _queries_for_section(section_id: str, max_queries: int) -> list[str]:
    """Deprecated compatibility API: returns no chapter-derived queries."""
    return []

def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists(): return []
    return [x for line in path.read_text(encoding="utf-8").splitlines() if line.strip() for x in [json.loads(line)] if isinstance(x,dict)]

def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(x,ensure_ascii=False)+"\n" for x in rows), encoding="utf-8")

def run_gap_discovery(output_dir: str | Path | None = None, *, embedding_server=None, embedding_model=None, use_local_embedding=True, request_path: str | Path | None = None) -> dict[str, Any]:
    if output_dir is not None: harvest.set_output_dir(output_dir)
    root = harvest.OUTPUT_DIR; audit_path = root / "audit" / "classification_audit.json"
    if not audit_path.exists(): raise FileNotFoundError("classification_audit.json not found; run classify_catalog.py first")
    audit = json.loads(audit_path.read_text(encoding="utf-8")); coverage = audit.get("topic_coverage") or {}
    if request_path:
        request = json.loads(Path(request_path).read_text(encoding="utf-8")); query_source = "tunnelbookai_request"
        queries = [ensure_domain_anchor(x) for x in [*(request.get("queries") or []), *[str(t).replace("_"," ") for t in request.get("topics") or []]]]
    else:
        request = None; query_source = "broad_topic_diversity"
        priorities = ["maintenance_cost","life_cycle_cost","electromechanical_maintenance","fire_safety","ventilation","geotechnics","NATM","TBM"]
        queries = [ensure_domain_anchor(x.replace("_"," ")) for x in sorted(priorities, key=lambda x:int((coverage.get(x) or {}).get("handoff") or 0))[:6]]
    existing = _read_jsonl(root / "discovery_catalog.jsonl"); keys = {str(x.get("discovery_key") or x.get("source_url") or "") for x in existing}
    additions, errors = [], []
    for query in dict.fromkeys(queries):
        batch, errs = discovery.discover_academic(query, per_source=8); errors.extend(errs)
        batch, _ = discovery.filter_relevant_records(batch)
        for record in discovery.deduplicate(batch):
            data = record.as_dict(); key = str(data.get("discovery_key") or data.get("source_url") or "")
            if key and key not in keys: additions.append(data | {"acquisition_status":"METADATA_RANKED"}); keys.add(key)
    _write_jsonl(root / "discovery_catalog.jsonl", [*existing,*additions])
    report = {"schema_version":"2.0","mode":query_source,"request_id":(request or {}).get("request_id"),"topics_informational":True,"queries":queries,"catalog_additions":len(additions),"catalog_total":len(existing)+len(additions),"errors":errors,"chapter_taxonomy_used":False}
    audit_dir=root/"audit"; audit_dir.mkdir(parents=True,exist_ok=True); (audit_dir/"topic_discovery_audit.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    return report

def parse_args() -> argparse.Namespace:
    p=argparse.ArgumentParser(description="Search broad tunnel topics or execute a TunnelBookAI request")
    p.add_argument("--output-dir"); p.add_argument("--request"); p.add_argument("--rules-only",action="store_true"); p.add_argument("--embedding-server"); p.add_argument("--embedding-model"); return p.parse_args()

def main() -> None:
    a=parse_args(); print(json.dumps(run_gap_discovery(a.output_dir,embedding_server=a.embedding_server,embedding_model=a.embedding_model,use_local_embedding=not a.rules_only,request_path=a.request),ensure_ascii=False,indent=2))
if __name__ == "__main__": main()
