#!/usr/bin/env python3
"""Audit harvested sources for exact duplicates, same-work records and version groups."""

from __future__ import annotations

import argparse
import difflib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

import tunnel_harvest as harvest

ROOT = Path(__file__).resolve().parent


def _load_policy() -> dict[str, Any]:
    with (ROOT / "config" / "version_policy.yaml").open("r", encoding="utf-8") as handle:
        value = yaml.safe_load(handle) or {}
    return value if isinstance(value, dict) else {}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _norm_title(value: Any) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _author_key(row: dict[str, Any]) -> str:
    authors = row.get("authors") or []
    if isinstance(authors, str):
        authors = [authors]
    return "|".join(sorted(str(x).casefold().strip() for x in authors if str(x).strip())[:3])


def _work_key(row: dict[str, Any]) -> str:
    doi = harvest.normalize_doi(row.get("doi"))
    if doi:
        return "doi:" + doi.casefold()
    return "tay:" + "|".join([_norm_title(row.get("title")), _author_key(row), str(row.get("year") or "")])


def _quality(row: dict[str, Any]) -> tuple[int, int, int, int]:
    return (
        1 if row.get("source_sha256") else 0,
        1 if row.get("source_path") else 0,
        1 if row.get("doi") else 0,
        len(str(row.get("abstract") or "")),
    )


def audit(output_dir: str | Path | None = None) -> dict[str, Any]:
    if output_dir is not None:
        harvest.set_output_dir(output_dir)
    root = harvest.OUTPUT_DIR
    rows = _read_jsonl(root / "classification_index.jsonl")
    policy = _load_policy()
    threshold = float(((policy.get("same_work_rules") or {}).get("title_author_year_similarity_threshold") or 0.94))

    sha_groups: defaultdict[str, list[int]] = defaultdict(list)
    doi_groups: defaultdict[str, list[int]] = defaultdict(list)
    work_groups: defaultdict[str, list[int]] = defaultdict(list)
    for i, row in enumerate(rows):
        sha = str(row.get("source_sha256") or "").strip().lower()
        if sha:
            sha_groups[sha].append(i)
        doi = harvest.normalize_doi(row.get("doi"))
        if doi:
            doi_groups[doi.casefold()].append(i)
        work_groups[_work_key(row)].append(i)

    exact = [idxs for idxs in sha_groups.values() if len(idxs) > 1]
    same_doi = [idxs for idxs in doi_groups.values() if len(idxs) > 1]

    fuzzy: list[dict[str, Any]] = []
    no_doi = [(i, row) for i, row in enumerate(rows) if not harvest.normalize_doi(row.get("doi"))]
    for pos, (i, left) in enumerate(no_doi):
        lt = _norm_title(left.get("title"))
        if len(lt) < 12:
            continue
        for j, right in no_doi[pos + 1:]:
            rt = _norm_title(right.get("title"))
            if len(rt) < 12 or str(left.get("year") or "") != str(right.get("year") or ""):
                continue
            ratio = difflib.SequenceMatcher(None, lt, rt).ratio()
            if ratio >= threshold:
                fuzzy.append({"left": i, "right": j, "similarity": round(ratio, 4), "action": "REVIEW_SAME_WORK"})

    canonical: dict[int, str] = {}
    for group in same_doi + exact:
        best = max(group, key=lambda idx: _quality(rows[idx]))
        for idx in group:
            canonical[idx] = "PRIMARY" if idx == best else "ALTERNATE_SOURCE"

    result = {
        "schema_version": "1.0",
        "documents": len(rows),
        "exact_duplicate_groups": len(exact),
        "same_doi_groups": len(same_doi),
        "fuzzy_review_pairs": len(fuzzy),
        "exact_duplicates": exact,
        "same_doi": same_doi,
        "fuzzy_candidates": fuzzy,
        "canonical_roles": {str(k): v for k, v in canonical.items()},
        "policy": policy,
        "note": "Fuzzy title/author/year candidates are never auto-merged.",
    }
    audit_dir = root / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "source_dedup_version_audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()
    print(json.dumps(audit(args.output_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
