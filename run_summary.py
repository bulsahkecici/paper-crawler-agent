#!/usr/bin/env python3
"""Book-agnostic run summaries and required audit views."""
from __future__ import annotations
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
import yaml
import tunnel_harvest as harvest

def _read(path: Path) -> dict[str, Any]:
    try:
        x=json.loads(path.read_text(encoding="utf-8")); return x if isinstance(x,dict) else {}
    except (OSError,ValueError): return {}
def _rows(path: Path) -> list[dict[str,Any]]:
    try: return [x for line in path.read_text(encoding="utf-8").splitlines() if line.strip() for x in [json.loads(line)] if isinstance(x,dict)]
    except (OSError,ValueError): return []
def _write(path: Path, data: Any) -> None: path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")

def write(output_dir: str | Path | None=None) -> dict[str,Any]:
    if output_dir is not None: harvest.set_output_dir(output_dir)
    root=harvest.OUTPUT_DIR; audit=root/"audit"; audit.mkdir(parents=True,exist_ok=True)
    discovery=_read(audit/"discovery_audit.json"); classification=_read(audit/"classification_audit.json"); dedup=_read(audit/"source_dedup_version_audit.json")
    package=root/"exports"/"TunnelBookAI_Source_Pack"; handoff=_read(package/"99_audit"/"handoff_audit.json"); rows=_rows(root/"classification_index.jsonl")
    source_counts=Counter(str(x.get("source_class") or "UNKNOWN") for x in rows); type_counts=Counter(str(x.get("document_type") or "UNKNOWN") for x in rows); rel_counts=Counter(str(x.get("relevance_status") or "UNASSESSED") for x in rows)
    acq_counts=Counter(str(x.get("acquisition_status") or "UNKNOWN") for x in rows); decisions=handoff.get("decision_counts") or {}
    registry=yaml.safe_load((Path(__file__).parent/"config"/"institutional_sources.yaml").read_text(encoding="utf-8")) or {}; institutions={str(x.get("id")):dict(discovered=0,acquired=0,handoff=0) for x in registry.get("institutions") or [] if x.get("enabled")}
    presentations=defaultdict(Counter)
    for row in rows:
        code=str(row.get("publisher_code") or "")
        if code in institutions:
            institutions[code]["discovered"]+=1; institutions[code]["acquired"]+=int(bool(row.get("handoff_candidate")))
        if "PRESENTATION" in str(row.get("document_type") or ""):
            platform=str((row.get("presentation") or {}).get("platform") or row.get("discovery_source") or "INSTITUTIONAL")
            presentations[platform]["discovered"]+=1; presentations[platform]["relevant"]+=int(str(row.get("relevance_status")) in {"STRONG","PROBABLE"}); presentations[platform]["acquired"]+=int(bool(row.get("handoff_candidate"))); presentations[platform]["metadata_only"]+=int(bool(row.get("metadata_only"))); presentations[platform]["original_source_resolved"]+=int(bool(row.get("original_source_resolved")))
    for row in _rows(package/"00_registry"/"handoff_manifest.jsonl"):
        code=str(row.get("publisher_code") or "")
        if code in institutions: institutions[code]["handoff"]+=1
        if "PRESENTATION" in str(row.get("document_type") or ""): presentations[str((row.get("presentation") or {}).get("platform") or "INSTITUTIONAL")]["handoff"]+=1
    coverage=classification.get("coverage") or {"schema_version":"1.0","basis":"book_agnostic_broad_topics","informational_only":True,"topics":{}}
    summary={"architecture":{"chapter_classification_in_core":False,"taxonomy_dependency":False,"broad_topics_active":True},"discovery":{"queries":discovery.get("queries",0),"discovered":len(rows),"academic":sum(v for k,v in source_counts.items() if k in {"ACADEMIC","UNIVERSITY_REPOSITORY","RESEARCH_REPOSITORY"}),"official":sum(v for k,v in source_counts.items() if k in {"TR_OFFICIAL","FOREIGN_GOVERNMENT","ROAD_AUTHORITY","TRANSPORT_AUTHORITY","INTERNATIONAL_OFFICIAL","STANDARD_BODY"}),"presentations":sum(v for k,v in type_counts.items() if "PRESENTATION" in k)},"classification":{"relevance":dict(rel_counts),"document_types":dict(type_counts),"source_classes":dict(source_counts),"qwen_calls":classification.get("qwen_reviewed_count",0)},"acquisition":dict(acq_counts),"decision_router":decisions,"handoff":{"total_ready":handoff.get("ready_for_handoff",0)},"integrity":{"stale_sidecars":max(0,len(list((root/"classifications").glob("*.classification.json")))-len(rows))},"topic_coverage":coverage.get("topics") or {},"institutions":institutions,"presentations":{k:dict(v) for k,v in presentations.items()},"dedup":dedup}
    _write(audit/"source_summary.json",{"source_classes":dict(source_counts),"document_types":dict(type_counts),"relevance":dict(rel_counts)}); _write(audit/"institution_summary.json",institutions); _write(audit/"presentation_summary.json",summary["presentations"]); _write(audit/"topic_coverage.json",coverage); _write(audit/"acquisition_summary.json",dict(acq_counts)); _write(audit/"duplicate_audit.json",dedup); _write(audit/"provider_health.json",_read(audit/"source_health.json")); _write(audit/"run_summary.json",summary)
    lines=["# PaperCrawler Run Summary","","> PaperCrawler classifies sources. TunnelBookAI classifies chunks and knowledge.","","## Architecture","","- Chapter classification in core: no","- Current book taxonomy dependency: no","- Broad topics active: yes","","## Decisions",""]+[f"- {k}: {decisions.get(k,0)}" for k in ("AUTO_HANDOFF","RETRY_ACQUISITION","RECLASSIFY","AUTO_REJECT","MANUAL_REVIEW","METADATA_REFERENCE")]+["","## Handoff","",f"- READY_FOR_HANDOFF: {handoff.get('ready_for_handoff',0)}",""]
    (audit/"run_summary.md").write_text("\n".join(lines),encoding="utf-8")
    return summary
