#!/usr/bin/env python3
"""Generate the requested old-vs-new comparison without treating count growth as success."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any

OLD={"classification total":2668,"READY_FOR_HANDOFF":687,"METADATA_REFERENCE":136,"RETRY_ACQUISITION":527,"RECLASSIFY":1022,"AUTO_REJECT":296,"MANUAL_REVIEW":0}
def _json(path:Path)->dict[str,Any]:
    try:return json.loads(path.read_text(encoding="utf-8"))
    except (OSError,ValueError):return {}
def generate(root: str|Path="tunel_makaleleri") -> Path:
    root=Path(root); audit=root/"audit"; c=_json(audit/"classification_audit.json"); h=_json(root/"exports"/"TunnelBookAI_Source_Pack"/"99_audit"/"handoff_audit.json"); s=_json(audit/"run_summary.json")
    d=h.get("decision_counts") or {}; new={"classification total":c.get("classification_total",0),"READY_FOR_HANDOFF":h.get("ready_for_handoff",0),"METADATA_REFERENCE":h.get("metadata_references",0),"RETRY_ACQUISITION":d.get("RETRY_ACQUISITION",0),"RECLASSIFY":d.get("RECLASSIFY",0),"AUTO_REJECT":d.get("AUTO_REJECT",0),"MANUAL_REVIEW":d.get("MANUAL_REVIEW",0)}
    lines=["# Book-Agnostic Refactor Comparison","","Counts describe routing outcomes; a larger handoff count alone is not evidence of success.","","| Metric | Old | New |","|---|---:|---:|"]+[f"| {k} | {v} | {new.get(k,0)} |" for k,v in OLD.items()]
    extra={"Qwen calls":c.get("qwen_reviewed_count",0),"average classification prompt size":c.get("average_prompt_chars","not measured"),"old section-dependent fields":"deprecated under legacy only","new topic-only fields":len((c.get("coverage") or {}).get("topics") or {}),"institutional source count":len(s.get("institutions") or {}),"newly added authorities":len(s.get("institutions") or {}),"presentation discoveries":(s.get("discovery") or {}).get("presentations",0),"official sources":(s.get("discovery") or {}).get("official",0),"academic sources":(s.get("discovery") or {}).get("academic",0),"stale artifacts":(s.get("integrity") or {}).get("stale_sidecars",0),"SHA failures":_json(audit/"handoff_quality_gate.json").get("sha_failures",0),"missing provenance":_json(audit/"handoff_quality_gate.json").get("missing_provenance",0)}
    lines += ["","## Architecture and integrity",""]+[f"- {k}: {v}" for k,v in extra.items()]
    path=audit/"book_agnostic_refactor_comparison.md"; path.write_text("\n".join(lines)+"\n",encoding="utf-8"); return path
if __name__=="__main__": print(generate())
