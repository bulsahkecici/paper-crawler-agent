#!/usr/bin/env python3
"""Classify harvested papers plus free-discovery sources without moving originals.

Rules always run. If a loopback embedding model is available, section scores are
fused with local semantic similarity. Ambiguous cases may then be reviewed by a
loopback-only Qwen model. No cloud fallback is allowed.

Inputs:
- catalog.json (classic academic harvest)
- discovery_catalog.jsonl (institutional/OAI/sitemap/web discovery)
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

import classification_engine as classifier
import bibliographic_dedup
import corpus_policy
import coverage_policy
import hybrid_classifier as hybrid
import relevance_engine as relevance
import tunnel_harvest as harvest


def _classification_dir(output_dir: Path) -> Path:
    path = output_dir / "classifications"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _audit_dir(output_dir: Path) -> Path:
    path = output_dir / "audit"
    path.mkdir(parents=True, exist_ok=True)
    return path


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


def _record_key(record: dict[str, Any], index: int) -> str:
    doi = harvest.normalize_doi(record.get("doi"))
    if doi:
        return "doi:" + doi.lower()
    sha = str(record.get("source_sha256") or "").strip().lower()
    if sha:
        return "sha:" + sha
    discovery_key = str(record.get("discovery_key") or "").strip()
    if discovery_key:
        return discovery_key
    for key in ("source_path", "local_pdf_path", "pdf_path", "path", "source_url", "landing_url"):
        value = str(record.get(key) or "").strip()
        if value:
            return key + ":" + value
    return f"index:{index}:{record.get('title') or ''}"


def _exact_work_key(record: dict[str, Any]) -> str | None:
    """Conservatively collapse identical title/year/author variants before review.

    DOI/SHA remain authoritative.  This only handles exact bibliographic
    duplicates, keeps the chosen source's provenance, and records alternates.
    """
    if harvest.normalize_doi(record.get("doi")) or record.get("source_sha256"):
        return None
    title = classifier._norm(str(record.get("title") or ""))
    year = str(record.get("year") or "")[:4]
    authors = record.get("authors") or []
    if isinstance(authors, str):
        authors = [authors]
    author_key = "|".join(sorted(classifier._norm(str(a)) for a in authors if str(a).strip())[:3])
    if not title or len(title) < 16 or not year or not author_key:
        return None
    return f"work:{title}|{author_key}|{year}"


def _merge_records(classic: list[Any], discovered: list[dict[str, Any]], reconciliation: Counter[str] | None = None) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    sequence = [*classic, *discovered]
    for index, record in enumerate(sequence, 1):
        if not isinstance(record, dict):
            continue
        key = _record_key(record, index)
        exact_work_key = _exact_work_key(record)
        if exact_work_key and exact_work_key in merged:
            key = exact_work_key
        previous = merged.get(key)
        if previous is None:
            merged[key] = dict(record)
            if exact_work_key:
                merged.setdefault(exact_work_key, merged[key])
            continue
        if reconciliation is not None:
            if key.startswith("doi:"):
                reconciliation["doi_dedup_removed"] += 1
            elif key.startswith("sha:"):
                reconciliation["sha_dedup_removed"] += 1
            elif key.startswith("work:"):
                reconciliation["title_author_year_dedup_removed"] += 1
            else:
                reconciliation["url_or_key_dedup_removed"] += 1
        current_rank = (
            bool(record.get("source_path") or record.get("local_pdf_path") or record.get("path")),
            len(str(record.get("abstract") or record.get("text_excerpt") or "")),
            bool(record.get("discovery_source")),
        )
        previous_rank = (
            bool(previous.get("source_path") or previous.get("local_pdf_path") or previous.get("path")),
            len(str(previous.get("abstract") or previous.get("text_excerpt") or "")),
            bool(previous.get("discovery_source")),
        )
        if current_rank > previous_rank:
            replacement = {**previous, **record}
            replacement["alternate_records"] = [*(previous.get("alternate_records") or []), {
                "title": previous.get("title"), "source": previous.get("source"),
                "source_url": previous.get("source_url"), "doi": previous.get("doi"),
            }]
            # The same work can have both a URL key and an exact-work alias.
            # Keep every alias pointing to the newly selected canonical record.
            for alias, value in list(merged.items()):
                if value is previous:
                    merged[alias] = replacement
        else:
            for field in ("discovery_source", "discovery_query", "source_url", "raw_html_path", "acquisition_status"):
                if not previous.get(field) and record.get(field):
                    previous[field] = record[field]
            previous["alternate_records"] = [*(previous.get("alternate_records") or []), {
                "title": record.get("title"), "source": record.get("source"),
                "source_url": record.get("source_url"), "doi": record.get("doi"),
            }]
    # Exact work aliases point at the same dict; retain each record only once.
    return list({id(value): value for value in merged.values()}.values())


def _stem_for_record(record: dict[str, Any], index: int) -> str:
    source = record.get("source_path") or record.get("local_pdf_path") or record.get("pdf_path") or record.get("path")
    if source:
        return Path(str(source)).stem
    doi = str(record.get("doi") or "").strip()
    if doi:
        return classifier._norm(doi).replace("/", "_").replace(".", "_")[:100]
    discovery_key = str(record.get("discovery_key") or "").strip()
    if discovery_key:
        return harvest.sanitize_filename(discovery_key, max_length=100)
    return harvest.sanitize_filename(str(record.get("title") or f"document_{index}"), max_length=100)


def classify_catalog(
    output_dir: str | Path | None = None,
    *,
    use_local_ai: bool = True,
    embedding_server: str | None = None,
    embedding_model: str | None = None,
    llm_server: str | None = None,
    llm_model: str | None = None,
) -> dict[str, Any]:
    if output_dir is not None:
        harvest.set_output_dir(output_dir)
    root = harvest.OUTPUT_DIR
    catalog = harvest.load_catalog()
    classic = catalog.get("papers") or catalog.get("downloaded") or []
    if not isinstance(classic, list):
        raise ValueError("catalog papers must be a list")
    discovered = _read_jsonl(root / "discovery_catalog.jsonl")
    reconciliation: Counter[str] = Counter()
    initially_merged = _merge_records(classic, discovered, reconciliation)
    papers, canonical_reasons = bibliographic_dedup.canonicalize(initially_merged)
    for reason, count in canonical_reasons.items():
        reconciliation[f"canonical_{reason.lower()}_removed"] += count

    emb_client = selected_embedding_model = llm_client = selected_llm_model = None
    if use_local_ai:
        emb_client, selected_embedding_model, llm_client, selected_llm_model = hybrid.detect_local_clients(
            embedding_servers=[embedding_server] if embedding_server else None,
            embedding_model=embedding_model,
            llm_servers=[llm_server] if llm_server else None,
            llm_model=llm_model,
        )
        if selected_embedding_model:
            print(f"Local embedding classifier: {selected_embedding_model} @ {emb_client.base_url}", flush=True)
        else:
            print("Local embedding model not found; using deterministic rules only.")
        if selected_llm_model:
            print(f"Local LLM reviewer: {selected_llm_model} @ {llm_client.base_url}", flush=True)
        else:
            print("Local LLM reviewer not found; ambiguous cases remain for review.", flush=True)

    profile_vectors: dict[str, list[float]] = {}
    classifications_dir = _classification_dir(root)
    counters = {
        "status": Counter(), "type": Counter(), "source": Counter(), "tier": Counter(),
        "route": Counter(), "section": Counter(), "usable_section": Counter(), "topic": Counter(), "input": Counter(), "method": Counter(),
    }
    low_confidence: list[dict[str, Any]] = []
    missing_section: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []

    for index, record in enumerate(papers, 1):
        if not isinstance(record, dict):
            continue
        # An abstract is a publisher-supplied summary. Light PDF text and web
        # excerpts are provisional classifier input only: keep them out of the
        # `abstract` field and feed them to the classifier via a separate,
        # clearly-named text so downstream artifacts never conflate the two.
        real_abstract = str(record.get("abstract") or "")
        classification_text = real_abstract or str(
            record.get("light_pdf_text") or record.get("text_excerpt") or ""
        )
        class_record = {**record, "abstract": classification_text[:6000]} if classification_text else dict(record)
        relevance_decision = relevance.evaluate(class_record)
        class_record = {**class_record, **relevance_decision}
        record = {**record, **relevance_decision}
        result = hybrid.classify_hybrid(
            class_record,
            embedding_client=emb_client,
            embedding_model=selected_embedding_model,
            llm_client=llm_client,
            llm_model=selected_llm_model,
            profile_vectors=profile_vectors,
        ) if use_local_ai else classifier.classify_record(class_record).as_dict()

        source_path = record.get("source_path") or record.get("local_pdf_path") or record.get("pdf_path") or record.get("path")
        source_exists = bool(source_path and Path(str(source_path)).expanduser().exists())
        acquisition_ok = str(record.get("acquisition_status") or "").upper() in {"DOWNLOADED_PDF", "SNAPSHOTTED_WEB"}
        handoff_candidate = source_exists and (acquisition_ok or not record.get("discovery_source"))
        payload = {
            "schema_version": "2.2",
            "document_key": record.get("doi") or record.get("source_sha256") or record.get("discovery_key") or _stem_for_record(record, index),
            "canonical_id": record.get("canonical_id"),
            "duplicate_sources": record.get("duplicate_sources") or [],
            "duplicate_urls": record.get("duplicate_urls") or [],
            "duplicate_reason": record.get("duplicate_reason"),
            "title": record.get("title"),
            "authors": record.get("authors") or [],
            "year": record.get("year"),
            "publisher": record.get("publisher") or record.get("venue"),
            "doi": harvest.normalize_doi(record.get("doi")),
            "source_sha256": record.get("source_sha256"),
            "source_path": source_path,
            "source_url": record.get("source_url"),
            "landing_url": record.get("landing_url"),
            "pdf_url": record.get("pdf_url"),
            "discovery_source": record.get("discovery_source") or record.get("source"),
            "discovery_query": record.get("discovery_query") or record.get("query"),
            "acquisition_status": record.get("acquisition_status"),
            "raw_html_path": record.get("raw_html_path"),
            "raw_html_sha256": record.get("raw_html_sha256"),
            "metadata_only": bool(record.get("metadata_only", False)),
            "abstract": real_abstract[:12000],
            "light_pdf_text": (str(record.get("light_pdf_text") or "")[:12000] or None),
            "classification_input": record.get("classification_input"),
            "classification_text": classification_text[:12000],
            **relevance_decision,
            "handoff_candidate": handoff_candidate,
            **result,
        }
        payload["crawler_evidence_level"] = corpus_policy.crawler_evidence_level({**record, **payload})
        payload["evidence_level"] = payload["crawler_evidence_level"]  # migration alias
        payload["source_tier"] = corpus_policy.source_tier(payload)
        payload["normalized_document_type"] = corpus_policy.normalized_document_type(payload)
        handoff_status, handoff_reason = corpus_policy.handoff_decision(payload)
        payload["acceptance_decision"] = handoff_status
        payload["acceptance_reason"] = handoff_reason
        stem = _stem_for_record(record, index)
        dest = classifications_dir / f"{stem}.classification.json"
        dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["classification_path"] = str(dest)
        results.append(payload)

        counters["input"]["discovery"] += int(bool(record.get("discovery_source")))
        counters["input"]["classic"] += int(not bool(record.get("discovery_source")))
        counters["status"][str(result.get("classification_status"))] += 1
        if result.get("llm_review", {}).get("used"):
            counters["method"]["qwen_reviewed"] += 1
        elif result.get("classification_status") == "REJECT_IRRELEVANT":
            counters["method"]["irrelevant_rejected"] += 1
        else:
            counters["method"]["rules_embedding_only"] += 1
        counters["type"][str(result.get("document_type"))] += 1
        counters["source"][str(result.get("source_class"))] += 1
        counters["tier"][str(result.get("authority_tier"))] += 1
        counters["route"][str(result.get("route_path"))] += 1
        for section in result.get("book_sections") or []:
            sid = str(section.get("id"))
            counters["section"][sid] += 1
            if handoff_candidate:
                counters["usable_section"][sid] += 1
        for topic in result.get("topics") or []:
            counters["topic"][str(topic)] += 1
        confidence = float(result.get("classification_confidence") or 0.0)
        if confidence < 0.75:
            low_confidence.append({
                "title": record.get("title"), "confidence": confidence,
                "status": result.get("classification_status"),
                "document_type": result.get("document_type"), "route_path": result.get("route_path"),
                "discovery_source": record.get("discovery_source"),
            })
        if not result.get("primary_section"):
            missing_section.append({
                "title": record.get("title"), "document_type": result.get("document_type"),
                "source_class": result.get("source_class"), "discovery_source": record.get("discovery_source"),
            })
        if index % 25 == 0 or index == len(papers):
            print(
                f"[classify] {index}/{len(papers)} status={result.get('classification_status')} "
                f"primary_section={result.get('primary_section') or 'none'}",
                flush=True,
            )

    coverage = coverage_policy.calculate(results)
    raw_total = len(classic) + len(discovered)
    dedup_removed = raw_total - len(papers)
    reconciliation_report = {
        "classic_input": len(classic), "discovery_input": len(discovered),
        "raw_input_total": raw_total, **dict(reconciliation),
        "dedup_removed": dedup_removed, "classification_input": len(papers),
        "classification_output": len(results),
        "invariant_ok": raw_total - dedup_removed == len(papers) == len(results),
    }
    audit = {
        "schema_version": "2.2",
        "documents": len(results),
        "input_counts": dict(counters["input"]),
        "classic_catalog_rows": len(classic),
        "discovery_catalog_rows": len(discovered),
        "local_ai": {
            "embedding_model": selected_embedding_model,
            "llm_model": selected_llm_model,
            "loopback_only": True,
        },
        "classification_total": len(results),
        "deterministic_only": counters["method"]["irrelevant_rejected"],
        "rule_embedding_auto": counters["method"]["rules_embedding_only"],
        "rules_embedding_only_count": counters["method"]["rules_embedding_only"],
        "qwen_reviewed_count": counters["method"]["qwen_reviewed"],
        "qwen_reviewed": counters["method"]["qwen_reviewed"],
        "qwen_review_rate": round(counters["method"]["qwen_reviewed"] / len(results), 4) if results else 0.0,
        "manual_review_count": sum(counters["status"][key] for key in ("NEEDS_REVIEW", "LOCAL_LLM_REVIEW")),
        "manual_review": sum(counters["status"][key] for key in ("NEEDS_REVIEW", "LOCAL_LLM_REVIEW")),
        "irrelevant_rejected_count": counters["method"]["irrelevant_rejected"],
        "status_counts": dict(counters["status"]),
        "document_type_counts": dict(counters["type"]),
        "source_class_counts": dict(counters["source"]),
        "authority_tier_counts": dict(counters["tier"]),
        "route_counts": dict(counters["route"]),
        "section_coverage": dict(sorted(counters["section"].items())),
        "handoff_candidate_section_coverage": dict(sorted(counters["usable_section"].items())),
        "coverage": coverage,
        "reconciliation": reconciliation_report,
        "topic_counts": dict(counters["topic"].most_common()),
        "low_confidence_count": len(low_confidence),
        "missing_section_count": len(missing_section),
        "low_confidence": low_confidence,
        "missing_section": missing_section,
    }
    audit_path = _audit_dir(root) / "classification_audit.json"
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")

    index_path = root / "classification_index.jsonl"
    with index_path.open("w", encoding="utf-8") as handle:
        for row in results:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"Classified: {len(results)} (classic={len(classic)}, discovery={len(discovered)})")
    print(
        "[stage 4/7 classification] "
        f"input={raw_total} dedup={dedup_removed} classified={len(results)} "
        f"accepted={sum(counters['status'][key] for key in corpus_policy.AUTO_HANDOFF)} "
        f"review={sum(counters['status'][key] for key in corpus_policy.REVIEW_STATUSES)} "
        f"rejected={sum(counters['status'][key] for key in corpus_policy.REJECT_STATUSES)}",
        flush=True,
    )
    print(f"Classification sidecars: {classifications_dir}")
    print(f"Audit: {audit_path}")
    print(f"Index: {index_path}")
    print("Statuses:", dict(counters["status"]))
    return audit


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Classify PaperCrawler harvest + free-discovery catalogs.")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--rules-only", action="store_true", help="Disable local embedding and LLM review.")
    parser.add_argument("--embedding-server", default=None, help="Loopback OpenAI-compatible embedding server.")
    parser.add_argument("--embedding-model", default=None, help="Embedding model ID or unique model-name fragment.")
    parser.add_argument("--llm-server", default=None, help="Loopback OpenAI-compatible chat model server.")
    parser.add_argument("--llm-model", default=None, help="Chat model ID or unique model-name fragment.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    classify_catalog(
        args.output_dir,
        use_local_ai=not args.rules_only,
        embedding_server=args.embedding_server,
        embedding_model=args.embedding_model,
        llm_server=args.llm_server,
        llm_model=args.llm_model,
    )


if __name__ == "__main__":
    main()
