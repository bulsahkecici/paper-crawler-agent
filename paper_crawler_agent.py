#!/usr/bin/env python3
"""Tunnel-engineering staging harvest + local literature notes.

Search/download (internet allowed for bibliographic APIs and OA PDFs only):
  OpenAlex, Europe PMC, DOAJ, Crossref, Unpaywall, PMC.

LLM literature notes: LOCAL LOOPBACK MODEL ONLY (127.0.0.1 / localhost / ::1).
No remote model provider, no cloud LLM, no upload of abstracts or PDFs.
Harvested papers remain corpus_status=STAGING and are not ingested into
the TunnelBookAI corpus from this script.

literature_notes/ files are AI-generated triage notes, NOT evidence and NOT
canonical source Markdown.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from qwen_agent.agents import Assistant
from qwen_agent.tools import BaseTool
from qwen_agent.tools.base import register_tool

import tunnel_harvest as harvest

OUTPUT_DIR = harvest.OUTPUT_DIR
PDF_DIR = harvest.PDF_DIR
METADATA_DIR = harvest.METADATA_DIR
LITERATURE_NOTES_DIR = harvest.LITERATURE_NOTES_DIR

SYSTEM_INSTRUCTION = """You are a local literature-triage assistant for a Turkish
highway-tunnel book covering: tunnel types and history; construction methods
(rock, soft ground, underwater; NATM/TBM/support); geotechnical investigations
and alignment; construction cost and life-cycle cost; operation, structural and
electromechanical maintenance, energy use; highway-tunnel accidents and safety.

You do NOT have the PDF text. Do not claim to have read the PDF.
You receive only bibliographic metadata: title, authors, year, abstract, and path.
Write an AI-generated literature note based on that metadata and abstract.

For each paper in the user message, call save_paper_summary exactly once.
Pass pdf_path unchanged. Put one JSON object per call — never concatenate two
JSON objects. Note likely methods, findings, and which book topic it may support
(construction method, cost/LCC, O&M, energy, highway safety, geotechnics).
Use only the provided title, authors, year, abstract, and path. Do not invent
citations or results that are not in the abstract.

The note is for topic triage and identifying papers worth later full-text
ingestion. It is NOT valid evidence and is not canonical source Markdown.
After the batch, write a short structured report grouped by those book topics.
"""

SUMMARIZE_BATCH = 8

LOCAL_ONLY_ERROR = (
    "No local loopback LLM server is available. Cloud fallback is disabled by policy."
)


def _dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _parse_tool_objects(params: str | dict[str, Any] | None) -> list[dict[str, Any]]:
    """Parse tool JSON, including concatenated objects the model sometimes emits."""
    if isinstance(params, dict):
        return [params]
    text = str(params or "").strip()
    if not text:
        return [{}]
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    objects: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    idx = 0
    while idx < len(text):
        while idx < len(text) and text[idx].isspace():
            idx += 1
        if idx >= len(text):
            break
        try:
            obj, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            break
        if isinstance(obj, dict):
            objects.append(obj)
        idx = end
    return objects or [{}]


@register_tool("harvest_tunnel_papers")
class HarvestTunnelPapers(BaseTool):
    description = (
        "Run a multi-source harvest of open-access tunnel papers "
        "(OpenAlex, Europe PMC, DOAJ, Crossref). Downloads ranked OA PDFs "
        "into the staging area (corpus_status=STAGING). Call this at most once."
    )
    parameters = [
        {
            "name": "max_results",
            "type": "number",
            "description": "Maximum PDFs to download (default 800, max 1000).",
            "required": False,
        }
    ]

    def call(self, params: str, **kwargs) -> str:
        args = self._verify_json_format_args(params) if params else {}
        limit = int(args.get("max_results") or harvest.DEFAULT_LIMIT)
        limit = max(5, min(limit, harvest.MAX_LIMIT))
        report = harvest.harvest(limit=limit)
        compact = dict(report)
        compact["papers"] = [
            {
                k: p.get(k)
                for k in (
                    "title",
                    "year",
                    "authors",
                    "source",
                    "doi",
                    "path",
                    "score",
                    "corpus_status",
                    "source_sha256",
                )
            }
            for p in (report.get("papers") or [])
        ]
        return _dumps(compact)


@register_tool("save_paper_summary")
class SavePaperSummary(BaseTool):
    description = (
        "Save an AI-generated literature note (not evidence) under "
        "tunel_makaleleri/literature_notes/ for a staged PDF."
    )
    parameters = [
        {"name": "title", "type": "string", "description": "Paper title.", "required": True},
        {
            "name": "summary",
            "type": "string",
            "description": "Literature note from metadata/abstract only.",
            "required": True,
        },
        {"name": "authors", "type": "string", "description": "Author list.", "required": False},
        {"name": "year", "type": "string", "description": "Publication year.", "required": False},
        {"name": "source", "type": "string", "description": "Source API.", "required": False},
        {"name": "pdf_path", "type": "string", "description": "Local PDF path.", "required": False},
    ]

    def call(self, params: str, **kwargs) -> str:
        payloads = _parse_tool_objects(params)
        saved = [_save_summary(item) for item in payloads]
        if len(saved) == 1:
            return _dumps(saved[0])
        return _dumps({"ok": all(item.get("ok") for item in saved), "saved": saved})


def _summary_dest(title: str, pdf_path: str | None) -> Path:
    harvest.ensure_output_dir()
    if pdf_path:
        candidate = Path(str(pdf_path)).expanduser()
        if candidate.suffix.lower() == ".pdf":
            _, dest = harvest.sidecar_paths(candidate)
            dest.parent.mkdir(parents=True, exist_ok=True)
            return dest
    return harvest.LITERATURE_NOTES_DIR / f"{harvest.sanitize_filename(title)}.summary.md"


def _save_summary(args: dict[str, Any]) -> dict[str, Any]:
    title = str(args.get("title") or "untitled").strip()
    summary = str(args.get("summary") or "").strip()
    if not summary:
        return {"ok": False, "error": "summary is required", "title": title}
    pdf_path = str(args.get("pdf_path") or "")
    dest = _summary_dest(title, args.get("pdf_path"))
    sha = "n/a"
    if pdf_path and Path(pdf_path).exists():
        sha, _ = harvest.hash_pdf(pdf_path)
    body = [
        "---",
        "ai_generated_literature_note: true",
        "evidence_eligible: false",
        f"source_pdf: {pdf_path or 'n/a'}",
        f"source_sha256: {sha}",
        "---",
        "",
        f"# {title}",
        "",
        f"- Authors: {args.get('authors') or 'n/a'}",
        f"- Year: {args.get('year') or 'n/a'}",
        f"- Source: {args.get('source') or 'n/a'}",
        f"- PDF: {pdf_path or 'n/a'}",
        "",
        "*AI-generated literature note based on available bibliographic metadata "
        "and abstract. The model did not read the PDF. Not evidence. "
        "Not canonical source Markdown.*",
        "",
        "## Summary",
        "",
        summary,
        "",
    ]
    dest.write_text("\n".join(body), encoding="utf-8")
    if pdf_path:
        harvest.update_literature_note_flags(pdf_path, None if sha == "n/a" else sha)
    return {"ok": True, "path": str(dest), "ai_generated_literature_note": True, "evidence_eligible": False}


LOCAL_LLM_CANDIDATES = (
    "http://127.0.0.1:11434/v1",
    "http://127.0.0.1:1234/v1",
    "http://127.0.0.1:8000/v1",
)


def is_loopback_model_server(url: str) -> bool:
    """True only if the URL host is loopback (127.0.0.1, localhost, ::1, or alias)."""
    parsed = urlparse(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, parsed.port or 80, type=socket.SOCK_STREAM)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        addr = info[4][0]
        try:
            if not ipaddress.ip_address(addr).is_loopback:
                return False
        except ValueError:
            return False
    return True


def _list_openai_models(base_url: str, api_key: str = "EMPTY") -> list[str] | None:
    if not is_loopback_model_server(base_url):
        return None
    url = base_url.rstrip("/") + "/models"
    try:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {api_key or 'EMPTY'}"},
            timeout=2.5,
        )
        if not response.ok:
            return None
        data = response.json().get("data") or []
        return [item["id"] for item in data if item.get("id")]
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None


def _pick_qwen_model(model_ids: list[str], requested: str | None) -> str:
    ids = [mid for mid in model_ids if mid and "embed" not in mid.lower()]
    if requested:
        if requested in model_ids:
            return requested
        lowered = requested.lower()
        for mid in ids:
            if lowered in mid.lower():
                return mid
    for needle in ("qwen3.6", "qwen3-6", "qwen3.5", "qwen3"):
        for mid in ids:
            if needle in mid.lower():
                return mid
    return ids[0] if ids else (requested or "qwen3.6")


def detect_local_llm(
    model: str | None = None,
    model_server: str | None = None,
    api_key: str = "EMPTY",
    *,
    explicit_server: bool = False,
) -> tuple[str, str]:
    if model_server and explicit_server and not is_loopback_model_server(model_server):
        raise RuntimeError(
            f"Rejected non-loopback model-server: {model_server}. "
            "LOCAL MODEL ONLY. LOOPBACK ONLY."
        )
    candidates: list[str] = []
    for url in (model_server, os.getenv("QWEN_MODEL_SERVER"), *LOCAL_LLM_CANDIDATES):
        if not url:
            continue
        normalized = url.rstrip("/")
        if normalized in candidates:
            continue
        if not is_loopback_model_server(normalized):
            continue
        candidates.append(normalized)
    last_error = "no OpenAI-compatible loopback server responded"
    for base_url in candidates:
        model_ids = _list_openai_models(base_url, api_key)
        if model_ids is None:
            last_error = f"no response from {base_url}"
            continue
        chosen = _pick_qwen_model(model_ids, model or os.getenv("QWEN_MODEL"))
        print(f"Using local LLM: {chosen} @ {base_url}")
        return chosen, base_url
    raise RuntimeError(
        f"{LOCAL_ONLY_ERROR}\n"
        "Start LM Studio (or Ollama/vLLM) on 127.0.0.1, or run --harvest-only.\n"
        f"Last probe: {last_error}"
    )


def build_llm_cfg(
    model: str | None = None,
    model_server: str | None = None,
    api_key: str | None = None,
    *,
    explicit_server: bool = False,
) -> dict[str, Any]:
    generate_cfg = {"top_p": 0.8, "thought_in_content": False}
    key = api_key or os.getenv("QWEN_API_KEY", "EMPTY")
    chosen_model, chosen_server = detect_local_llm(
        model, model_server, key, explicit_server=explicit_server
    )
    return {
        "model": chosen_model,
        "model_server": chosen_server,
        "api_key": key,
        "generate_cfg": generate_cfg,
    }


def build_agent(
    model: str | None = None,
    model_server: str | None = None,
    api_key: str | None = None,
    *,
    explicit_server: bool = False,
) -> Assistant:
    harvest.ensure_output_dir()
    return Assistant(
        llm=build_llm_cfg(
            model, model_server, api_key, explicit_server=explicit_server
        ),
        name="Tunnel Paper Crawler",
        description="Writes local AI literature notes for staged OA tunnel papers.",
        system_message=SYSTEM_INSTRUCTION,
        function_list=["save_paper_summary"],
    )


def _summary_path(paper: dict[str, Any]) -> Path | None:
    pdf = paper.get("path") or paper.get("pdf_path") or paper.get("local_pdf_path")
    if not pdf:
        return None
    _, dest = harvest.sidecar_paths(pdf)
    return dest


def _already_summarized(paper: dict[str, Any]) -> bool:
    path = _summary_path(paper)
    if not path or not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return "## Summary" in text
    except OSError:
        return False


def _summarize_prompt(papers: list[dict[str, Any]]) -> str:
    lines = [
        "Write AI-generated literature notes from bibliographic metadata and abstracts only.",
        "You do not have PDF text. Do not claim to have read the PDF.",
        "Call save_paper_summary exactly once per paper, with pdf_path copied verbatim.",
        "Do not merge two papers into one JSON object.",
        "These notes are for triage, not evidence.",
        "",
    ]
    for index, paper in enumerate(papers, 1):
        authors = paper.get("authors") or []
        if isinstance(authors, list):
            authors = ", ".join(str(a) for a in authors[:8])
        abstract = ""
        pdf = paper.get("path") or paper.get("pdf_path") or paper.get("local_pdf_path")
        if pdf:
            meta, _ = harvest.sidecar_paths(pdf)
            if meta.exists():
                try:
                    abstract = (json.loads(meta.read_text(encoding="utf-8")).get("abstract") or "")[:900]
                except (OSError, ValueError):
                    abstract = ""
        lines.append(f"{index}. {paper.get('title')}")
        lines.append(f"   authors: {authors or 'n/a'}")
        lines.append(f"   year: {paper.get('year') or 'n/a'}  source: {paper.get('source')}  doi: {paper.get('doi') or 'n/a'}")
        lines.append(f"   pdf_path: {pdf}")
        if abstract:
            lines.append(f"   abstract: {abstract}")
        lines.append("")
    return "\n".join(lines)


def run_agent(bot: Assistant, query: str) -> None:
    messages = [{"role": "user", "content": query}]
    print("\n=== Agent (literature notes) ===")
    for last in bot.run(messages=messages):
        if not last:
            continue
        piece = last[-1]
        content = piece.get("content") if isinstance(piece, dict) else piece
        if content:
            print(content)
    print("\n=== Done ===")
    print(f"PDFs: {harvest.PDF_DIR}")
    print(f"Literature notes: {harvest.LITERATURE_NOTES_DIR}")
    print(f"Metadata: {harvest.METADATA_DIR}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Harvest OA tunnel papers into a staging area, then optionally write "
            "local-only AI literature notes. No cloud LLM. Not corpus ingest."
        )
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=harvest.DEFAULT_LIMIT,
        help="Target PDF count (default 800, max 1000).",
    )
    parser.add_argument("--harvest-only", action="store_true", help="Download only; skip the local LLM.")
    parser.add_argument("--skip-harvest", action="store_true", help="Note existing PDFs; do not search again.")
    parser.add_argument("--model", default=os.getenv("QWEN_MODEL"))
    parser.add_argument(
        "--model-server",
        default=None,
        help="Loopback OpenAI-compatible base URL (e.g. http://127.0.0.1:1234/v1).",
    )
    parser.add_argument("--api-key", default=os.getenv("QWEN_API_KEY", "EMPTY"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    limit = max(5, min(args.limit, harvest.MAX_LIMIT))
    report: dict[str, Any]
    if args.skip_harvest:
        report = harvest.load_catalog()
    else:
        report = harvest.harvest(limit=limit)

    if args.harvest_only:
        print(
            "Harvest finished (--harvest-only). "
            "PDFs in pdfs/; provenance in metadata/; abstracts in literature_notes/. "
            "corpus_status=STAGING. Not ingested into TunnelBookAI."
        )
        return

    papers = report.get("papers") or report.get("downloaded") or []
    if not papers and args.skip_harvest:
        papers = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(harvest.METADATA_DIR.glob("*.meta.json"))
        ]
        for paper in papers:
            paper.setdefault("path", paper.get("local_pdf_path") or paper.get("pdf_path"))
    print(f"Using existing catalog: {len(papers)} papers" if args.skip_harvest else f"Harvested {len(papers)} papers")
    if not papers:
        raise SystemExit("No papers downloaded; nothing to summarize.")

    pending = [paper for paper in papers if not _already_summarized(paper)]
    skipped = len(papers) - len(pending)
    if skipped:
        print(f"Skipping {skipped} papers that already have AI literature notes.")
    if not pending:
        print("All papers already have AI literature notes.")
        return

    try:
        bot = build_agent(
            model=args.model,
            model_server=args.model_server,
            api_key=args.api_key,
            explicit_server=args.model_server is not None,
        )
        batches = [
            pending[i : i + SUMMARIZE_BATCH] for i in range(0, len(pending), SUMMARIZE_BATCH)
        ]
        for index, batch in enumerate(batches, 1):
            print(f"\n=== Literature-note batch {index}/{len(batches)} ({len(batch)} papers) ===")
            run_agent(bot, _summarize_prompt(batch))
    except RuntimeError as exc:
        raise SystemExit(f"\n{exc}\n") from exc
    except Exception as exc:
        if "connection" in str(exc).lower() or "Connection" in type(exc).__name__:
            raise SystemExit(
                f"\n{LOCAL_ONLY_ERROR}\n"
                "PDFs remain in ./tunel_makaleleri/pdfs/. Start a loopback server and run:\n"
                "  python paper_crawler_agent.py --skip-harvest\n"
                f"Details: {exc}\n"
            ) from exc
        raise


if __name__ == "__main__":
    main()
