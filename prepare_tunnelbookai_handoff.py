#!/usr/bin/env python3
"""One-command PaperCrawler → TunnelBookAI handoff preparation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import classify_catalog
import free_discovery
import gap_discovery
import handoff_export
import light_pdf_extract
import source_dedup_audit
import supplemental_discovery
import run_summary
import run_manifest
import handoff_quality_gate
import pipeline_state
import tunnel_harvest as harvest


def _log(message: str) -> None:
    print(f"[pipeline] {message}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discover, enrich, classify, gap-fill, audit and export TunnelBookAI source handoff.")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--destination", default=None)
    parser.add_argument("--max-queries", type=int, default=60)
    parser.add_argument("--per-source", type=int, default=10)
    parser.add_argument("--max-book-queries", type=int, default=18)
    parser.add_argument("--light-pdf-pages", type=int, default=3)
    parser.add_argument("--rules-only", action="store_true", help="Disable local embedding/LLM classification.")
    parser.add_argument("--embedding-server", default=None, help="Loopback OpenAI-compatible embedding server.")
    parser.add_argument("--embedding-model", default=None, help="Embedding model ID or unique model-name fragment.")
    parser.add_argument("--llm-server", default=None, help="Loopback OpenAI-compatible chat model server.")
    parser.add_argument("--llm-model", default=None, help="Chat model ID or unique model-name fragment.")
    parser.add_argument("--skip-gap-pass", action="store_true", help="Skip coverage-driven second discovery pass.")
    parser.add_argument("--skip-news-books", action="store_true", help="Skip RSS/Atom institutional news and book metadata discovery.")
    parser.add_argument("--no-dynamic-expansion", action="store_true")
    parser.add_argument("--resume", action="store_true", default=True, help="Resume completed stages (default).")
    parser.add_argument("--fresh-run", action="store_true", help="Create a new checkpoint state; existing documents are preserved.")
    parser.add_argument("--bootstrap-legacy-checkpoint", action="store_true", help="Adopt validated pre-checkpoint artifacts and resume at the interrupted gap stage.")
    parser.add_argument("--checkpoint-only", action="store_true", help="Write/inspect checkpoint state without running pipeline stages.")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--classify-only", action="store_true")
    modes.add_argument("--handoff-only", action="store_true")
    modes.add_argument("--review-export-only", action="store_true")
    modes.add_argument("--retry-acquisition-only", action="store_true")
    modes.add_argument("--gap-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Print the selected bounded workflow without writing files or calling providers.")
    return parser.parse_args()


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _read_jsonl(path: Path) -> list[dict]:
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError):
        return []


def _retry_acquisition(root: Path) -> dict:
    """Retry only records already routed to acquisition; never expand discovery."""
    queue_path = root / "exports" / "TunnelBookAI_Source_Pack" / "99_audit" / "retry_acquisition.jsonl"
    queue = _read_jsonl(queue_path)
    catalog_path = root / "discovery_catalog.jsonl"
    catalog = _read_jsonl(catalog_path)
    outcomes: list[dict] = []
    for item in queue:
        source = next((row for row in catalog if (
            (item.get("document_id") and item.get("document_id") in {row.get("discovery_key"), row.get("doi"), row.get("source_sha256")})
            or (item.get("source_url") and item.get("source_url") == row.get("source_url"))
            or (item.get("title") and item.get("title") == row.get("title"))
        )), item)
        allowed = free_discovery.DiscoveryRecord.__dataclass_fields__
        payload = {key: value for key, value in source.items() if key in allowed}
        payload.setdefault("title", str(item.get("title") or "Untitled tunnel source"))
        payload.setdefault("source", str(item.get("discovery_source") or "retry"))
        payload.setdefault("discovery_source", str(item.get("discovery_source") or "retry"))
        result = free_discovery.acquire_record(free_discovery.DiscoveryRecord(**payload), root)
        source.update(result)
        outcomes.append({"document_id": item.get("document_id"), "title": item.get("title"), "acquisition_status": result.get("acquisition_status")})
    if queue:
        with catalog_path.open("w", encoding="utf-8") as handle:
            for row in catalog:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    report = {"attempted": len(queue), "outcomes": outcomes}
    (root / "audit" / "retry_acquisition_audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    args = parse_args()
    selected_mode = next((name for name in ("classify_only", "handoff_only", "review_export_only", "retry_acquisition_only", "gap_only") if getattr(args, name)), "full")
    if args.dry_run:
        print(json.dumps({
            "mode": selected_mode, "output_dir": args.output_dir or str(harvest.OUTPUT_DIR),
            "rules_only": args.rules_only, "resume": args.resume and not args.fresh_run,
            "network_required": selected_mode in {"full", "gap_only", "retry_acquisition_only"},
            "writes": False,
        }, ensure_ascii=False, indent=2))
        return
    if args.output_dir is not None:
        harvest.set_output_dir(args.output_dir)
    root = harvest.OUTPUT_DIR
    if args.classify_only:
        print(json.dumps(classify_catalog.classify_catalog(
            root, use_local_ai=not args.rules_only,
            embedding_server=args.embedding_server, embedding_model=args.embedding_model,
            llm_server=args.llm_server, llm_model=args.llm_model,
        ), ensure_ascii=False, indent=2))
        return
    if args.handoff_only or args.review_export_only:
        print(json.dumps(handoff_export.export_handoff(root, destination=args.destination), ensure_ascii=False, indent=2))
        return
    if args.gap_only:
        print(json.dumps(gap_discovery.run_gap_discovery(
            root, embedding_server=args.embedding_server, embedding_model=args.embedding_model,
            use_local_embedding=not args.rules_only,
        ), ensure_ascii=False, indent=2))
        return
    if args.retry_acquisition_only:
        print(json.dumps(_retry_acquisition(root), ensure_ascii=False, indent=2))
        return

    manifest_context = run_manifest.start(root, models={
        "embedding_model": args.embedding_model, "embedding_endpoint": args.embedding_server,
        "llm_model": args.llm_model, "llm_endpoint": args.llm_server,
        "local_only": True,
    }, query_count=max(1, args.max_queries))
    state = pipeline_state.PipelineState(root, fresh=args.fresh_run)
    if args.bootstrap_legacy_checkpoint:
        adopted = state.bootstrap_legacy()
        _log(f"legacy checkpoint adopted: {adopted}")
    if args.checkpoint_only:
        print(json.dumps(state.data, ensure_ascii=False, indent=2), flush=True)
        return
    _log("1/7 free discovery started")
    discovery_report = state.run("free_discovery", lambda: free_discovery.discover_all(
        output_dir=args.output_dir, max_queries=max(1, args.max_queries), per_source=max(1, args.per_source),
        acquire=True, expand_dynamic=not args.no_dynamic_expansion,
        embedding_server=args.embedding_server, embedding_model=args.embedding_model,
        use_local_embedding=not args.rules_only,
    )) or _read_json(root / "audit" / "discovery_audit.json")
    supplemental_report = None
    if not args.skip_news_books:
        _log("2/7 institutional news and book discovery started")
        supplemental_report = state.run("institutional_discovery", lambda: supplemental_discovery.run_supplemental_discovery(
            output_dir=args.output_dir,
            max_book_queries=max(1, args.max_book_queries),
            per_source=max(1, args.per_source),
            acquire_news=True,
        )) or _read_json(root / "audit" / "supplemental_discovery_audit.json")
    elif not state.completed("institutional_discovery"):
        state.mark("institutional_discovery", "COMPLETED", skipped=True)
    _log("3/7 lightweight PDF text extraction started")
    light_extract_report = state.run("pdf_enrichment", lambda: light_pdf_extract.enrich_catalog(
        args.output_dir, max_pages=max(1, args.light_pdf_pages),
    )) or _read_json(root / "audit" / "light_pdf_extract_audit.json")
    _log("4/7 initial classification started")
    first_classification = state.run("initial_classification", lambda: classify_catalog.classify_catalog(
        args.output_dir,
        use_local_ai=not args.rules_only,
        embedding_server=args.embedding_server,
        embedding_model=args.embedding_model,
        llm_server=args.llm_server,
        llm_model=args.llm_model,
    )) or _read_json(root / "audit" / "classification_audit.json")
    gap_report = None
    final_classification = first_classification
    if not args.skip_gap_pass:
        _log("5/7 coverage-gap discovery started")
        gap_report = state.run("gap_discovery", lambda: gap_discovery.run_gap_discovery(
            args.output_dir, embedding_server=args.embedding_server, embedding_model=args.embedding_model,
            use_local_embedding=not args.rules_only,
        )) or _read_json(root / "audit" / "gap_discovery_audit.json")
        if int(gap_report.get("catalog_additions") or 0) > 0:
            light_pdf_extract.enrich_catalog(args.output_dir, max_pages=max(1, args.light_pdf_pages))
            final_classification = state.run("reclassification", lambda: classify_catalog.classify_catalog(
                args.output_dir,
                use_local_ai=not args.rules_only,
                embedding_server=args.embedding_server,
                embedding_model=args.embedding_model,
                llm_server=args.llm_server,
                llm_model=args.llm_model,
            )) or _read_json(root / "audit" / "classification_audit.json")
        elif not state.completed("reclassification"):
            state.mark("reclassification", "COMPLETED", skipped=True)
    else:
        if not state.completed("gap_discovery"):
            state.mark("gap_discovery", "COMPLETED", skipped=True)
        if not state.completed("reclassification"):
            state.mark("reclassification", "COMPLETED", skipped=True)
    _log("6/7 source duplicate/version audit started")
    source_audit = state.run("source_audit", lambda: source_dedup_audit.audit(args.output_dir)) or _read_json(root / "audit" / "source_dedup_version_audit.json")
    _log("7/7 TunnelBookAI handoff export started")
    handoff = state.run("handoff", lambda: handoff_export.export_handoff(args.output_dir, destination=args.destination)) or _read_json(root / "exports" / "TunnelBookAI_Source_Pack" / "99_audit" / "handoff_audit.json")
    _log("run summary started")
    summary = run_summary.write(args.output_dir)
    _log("handoff quality gate started")
    quality_gate = handoff_quality_gate.evaluate_handoff(root, package_root=args.destination)
    run_manifest.finish(manifest_context, root, summary=summary, decision=quality_gate)
    report = {
        "discovery": discovery_report,
        "news_and_books": supplemental_report,
        "light_pdf_extract": light_extract_report,
        "initial_classification": {
            "documents": first_classification.get("documents"),
            "status_counts": first_classification.get("status_counts"),
        },
        "gap_pass": gap_report,
        "final_classification": {
            "documents": final_classification.get("documents"),
            "status_counts": final_classification.get("status_counts"),
            "topic_coverage": final_classification.get("topic_coverage"),
        },
        "source_dedup_version_audit": {
            "documents": source_audit.get("documents"),
            "exact_duplicate_groups": source_audit.get("exact_duplicate_groups"),
            "same_doi_groups": source_audit.get("same_doi_groups"),
            "fuzzy_review_pairs": source_audit.get("fuzzy_review_pairs"),
        },
        "handoff": handoff,
        "run_summary": summary,
        "handoff_quality_gate": quality_gate,
    }
    _log("pipeline finished")
    hq = handoff or {}
    print("\n".join([
        "",
        "PAPERCRAWLER RUN COMPLETE",
        "=========================",
        "",
        f"Discovery: queries={summary.get('discovery', {}).get('queries', 0)} discovered={summary.get('discovery', {}).get('discovered', 0)}",
        f"Acquired originals: {sum(int(v) for k, v in (summary.get('acquisition') or {}).items() if 'DOWNLOAD' in str(k) or 'SNAPSHOT' in str(k))}",
        f"Provisional classifications: {(final_classification or {}).get('documents', 0)}",
        "",
        f"READY_FOR_HANDOFF:   {hq.get('ready_for_handoff', 0)}",
        f"METADATA_REFERENCE:  {hq.get('metadata_references', 0)}",
        f"MANUAL_REVIEW:       {hq.get('manual_review', 0)}",
        f"RETRY_ACQUISITION:   {hq.get('retry_acquisition', 0)}",
        f"AUTO_REJECT:         {hq.get('rejected', 0)}",
        "",
        f"Handoff quality gate: {quality_gate.get('decision', 'NO_GO')}",
        "",
    ]), flush=True)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
