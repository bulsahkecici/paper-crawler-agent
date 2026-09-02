#!/usr/bin/env python3
"""Safely migrate classification JSON/JSONL chapter fields into ``legacy``."""
from __future__ import annotations
import argparse, json
from pathlib import Path
from typing import Any
import classification_engine

def migrate_payload(value: Any) -> Any:
    if isinstance(value, list): return [migrate_payload(x) for x in value]
    if not isinstance(value, dict): return value
    migrated = classification_engine.migrate_legacy_fields(value)
    return {k:migrate_payload(v) if k != "legacy" else v for k,v in migrated.items()}

def migrate(root: str | Path, *, write: bool=False) -> dict[str,Any]:
    base=Path(root); targets=[]
    index=base/"classification_index.jsonl"
    if index.exists(): targets.append(index)
    targets.extend((base/"classifications").glob("*.classification.json") if (base/"classifications").exists() else [])
    changed=errors=0
    for path in targets:
        try:
            if path.suffix == ".jsonl":
                rows=[json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]; new=[migrate_payload(x) for x in rows]; content="".join(json.dumps(x,ensure_ascii=False)+"\n" for x in new)
            else:
                old=json.loads(path.read_text(encoding="utf-8")); content=json.dumps(migrate_payload(old),ensure_ascii=False,indent=2)
            if content != path.read_text(encoding="utf-8"): changed+=1
            if write: path.write_text(content,encoding="utf-8")
        except (OSError,ValueError): errors+=1
    report={"schema_version":"1.0","mode":"write" if write else "dry_run","files_scanned":len(targets),"files_changed":changed,"errors":errors,"originals_deleted":0}
    audit=base/"audit"; audit.mkdir(parents=True,exist_ok=True); (audit/"book_agnostic_migration.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    return report

def main() -> None:
    p=argparse.ArgumentParser(); p.add_argument("--output-dir",default="tunel_makaleleri"); p.add_argument("--write",action="store_true"); a=p.parse_args(); print(json.dumps(migrate(a.output_dir,write=a.write),indent=2))
if __name__ == "__main__": main()
