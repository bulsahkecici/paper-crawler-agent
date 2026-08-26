#!/usr/bin/env python3
"""Focused staging / privacy tests. No live bibliographic API calls."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import paper_crawler_agent as agent
import tunnel_harvest as harvest

MINI_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _staging() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="tunnel_staging_"))
    harvest.set_output_dir(tmp)
    return tmp


class StagingLayoutTests(unittest.TestCase):
    def test_pdf_hash_and_size(self) -> None:
        tmp = _staging()
        pdf = harvest.PDF_DIR / "sample.pdf"
        pdf.write_bytes(MINI_PDF)
        sha, size = harvest.hash_pdf(pdf)
        self.assertEqual(size, len(MINI_PDF))
        self.assertEqual(sha, hashlib.sha256(MINI_PDF).hexdigest())
        self.assertEqual(len(sha), 64)

    def test_sidecar_staging_status_and_hash(self) -> None:
        tmp = _staging()
        pdf = harvest.PDF_DIR / "paper.pdf"
        pdf.write_bytes(MINI_PDF)
        sha, size = harvest.hash_pdf(pdf)
        paper = harvest.Paper(title="NATM tunnel support shotcrete", source="openalex", query="natm")
        paper.score = 8
        download = {
            "ok": True,
            "url": "http://example.test/a.pdf",
            "source_sha256": sha,
            "source_size_bytes": size,
            "corpus_status": harvest.CORPUS_STAGING,
        }
        meta_path = harvest._write_sidecar(paper, str(pdf), download)
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["source_sha256"], sha)
        self.assertEqual(payload["source_size_bytes"], size)
        self.assertEqual(payload["corpus_status"], "STAGING")
        self.assertNotEqual(payload["corpus_status"], "READY_FOR_INGEST")
        self.assertNotEqual(payload["corpus_status"], "INGESTED")
        self.assertFalse(payload["evidence_eligible"])
        self.assertEqual(payload["local_pdf_path"], str(pdf))
        self.assertTrue((tmp / "catalog.json").parent.exists())

    def test_rejected_status(self) -> None:
        _staging()
        pdf = harvest.PDF_DIR / "dns.pdf"
        pdf.write_bytes(MINI_PDF)
        row = {
            "title": "Combating Malicious DNS Tunnel",
            "abstract": "malware dns tunnel",
            "path": str(pdf),
            "pdf_path": str(pdf),
        }
        harvest.catalog_path().write_text(json.dumps({"papers": [row], "downloaded": [row]}), encoding="utf-8")
        kept, removed = harvest.purge_offtopic()
        self.assertGreaterEqual(removed, 1)
        self.assertEqual(len(kept), 0)
        self.assertTrue((harvest.REJECTED_DIR / "dns.pdf").exists())

    def test_literature_note_flags(self) -> None:
        _staging()
        pdf = harvest.PDF_DIR / "note.pdf"
        pdf.write_bytes(MINI_PDF)
        result = agent._save_summary(
            {
                "title": "Highway tunnel ventilation",
                "summary": "Abstract-only triage note about ventilation energy.",
                "authors": "A B",
                "year": "2024",
                "source": "openalex",
                "pdf_path": str(pdf),
            }
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["ai_generated_literature_note"])
        self.assertFalse(result["evidence_eligible"])
        text = Path(result["path"]).read_text(encoding="utf-8")
        self.assertIn("ai_generated_literature_note: true", text)
        self.assertIn("evidence_eligible: false", text)
        self.assertIn("did not read the PDF", text)
        meta, _ = harvest.sidecar_paths(pdf)
        payload = json.loads(meta.read_text(encoding="utf-8"))
        self.assertTrue(payload["ai_generated_literature_note"])
        self.assertFalse(payload["evidence_eligible"])
        self.assertEqual(payload["corpus_status"], "STAGING")

    def test_old_markdown_json_migrate(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="tunnel_migrate_"))
        old_md = tmp / "markdown"
        old_json = tmp / "json"
        old_md.mkdir()
        old_json.mkdir()
        (old_md / "Alpha.summary.md").write_text("# Alpha\n", encoding="utf-8")
        (old_json / "Alpha.meta.json").write_text('{"title": "Alpha"}', encoding="utf-8")
        (old_json / "catalog.json").write_text('{"papers": [{"title": "Alpha"}], "downloaded": []}', encoding="utf-8")
        (old_json / "index.jsonl").write_text("{}\n", encoding="utf-8")
        harvest.set_output_dir(tmp)
        self.assertTrue((tmp / "literature_notes" / "Alpha.summary.md").exists())
        self.assertTrue((tmp / "metadata" / "Alpha.meta.json").exists())
        self.assertTrue((tmp / "catalog.json").exists())
        self.assertTrue((tmp / "index.jsonl").exists())
        catalog = json.loads((tmp / "catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(catalog["papers"][0]["title"], "Alpha")

    def test_migrate_does_not_overwrite_larger_dest(self) -> None:
        tmp = _staging()
        dest = harvest.LITERATURE_NOTES_DIR / "Keep.summary.md"
        dest.write_text("LARGE" * 50, encoding="utf-8")
        old = (tmp / "markdown")
        old.mkdir(exist_ok=True)
        (old / "Keep.summary.md").write_text("tiny", encoding="utf-8")
        harvest.migrate_staging_layout()
        self.assertGreater(dest.stat().st_size, 10)
        self.assertIn("LARGE", dest.read_text(encoding="utf-8"))
        self.assertTrue((old / "Keep.summary.md").exists())


class LoopbackLlmTests(unittest.TestCase):
    def test_lm_studio_url_accepted(self) -> None:
        self.assertTrue(agent.is_loopback_model_server("http://127.0.0.1:1234/v1"))

    def test_localhost_accepted(self) -> None:
        self.assertTrue(agent.is_loopback_model_server("http://localhost:11434/v1"))

    def test_loopback_v6_accepted(self) -> None:
        self.assertTrue(agent.is_loopback_model_server("http://[::1]:8000/v1"))

    def test_remote_url_rejected(self) -> None:
        self.assertFalse(agent.is_loopback_model_server("https://api.example.com/v1"))
        self.assertFalse(agent.is_loopback_model_server("http://192.168.1.10:1234/v1"))
        self.assertFalse(agent.is_loopback_model_server("http://10.0.0.5:11434/v1"))
        with self.assertRaises(RuntimeError) as ctx:
            agent.detect_local_llm(model_server="https://api.example.com/v1", explicit_server=True)
        self.assertIn("Rejected non-loopback", str(ctx.exception))

    def test_no_dashscope_cli(self) -> None:
        with self.assertRaises(SystemExit):
            with patch("sys.argv", ["paper_crawler_agent.py", "--dashscope"]):
                agent.parse_args()

    def test_cloud_env_ignored_and_no_fallback(self) -> None:
        _staging()
        env = {
            "DASHSCOPE_API_KEY": "sk-cloud",
            "QWEN_BACKEND": "dashscope",
            "QWEN_MODEL_SERVER": "https://dashscope.aliyuncs.com/v1",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch.object(agent, "_list_openai_models", return_value=None):
                with self.assertRaises(RuntimeError) as ctx:
                    agent.detect_local_llm()
        msg = str(ctx.exception)
        self.assertIn("Cloud fallback is disabled by policy", msg)

    def test_harvest_only_skips_llm(self) -> None:
        _staging()
        called = {"llm": False, "harvest": False}

        def fake_harvest(**kwargs):
            called["harvest"] = True
            return {"papers": [], "downloaded": []}

        def fake_build_agent(**kwargs):
            called["llm"] = True
            raise AssertionError("LLM must not run with --harvest-only")

        with patch.object(harvest, "harvest", fake_harvest):
            with patch.object(agent, "build_agent", fake_build_agent):
                with patch("sys.argv", ["paper_crawler_agent.py", "--harvest-only", "--limit", "5"]):
                    agent.main()
        self.assertTrue(called["harvest"])
        self.assertFalse(called["llm"])

    def test_catalog_paths_after_migration(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="tunnel_cat_"))
        (tmp / "json").mkdir()
        (tmp / "json" / "catalog.json").write_text(
            json.dumps({"papers": [{"title": "TBM NATM tunnel", "path": "x.pdf"}]}),
            encoding="utf-8",
        )
        harvest.set_output_dir(tmp)
        self.assertEqual(harvest.catalog_path().resolve(), (tmp / "catalog.json").resolve())
        self.assertTrue(harvest.catalog_path().exists())
        data = harvest.load_catalog()
        self.assertEqual(len(data.get("papers") or []), 1)


class SourceHygieneTests(unittest.TestCase):
    def test_no_dashscope_in_agent_source(self) -> None:
        text = Path(agent.__file__).read_text(encoding="utf-8")
        self.assertNotIn("DASHSCOPE_API_KEY", text)
        self.assertNotIn("qwen_dashscope", text)
        self.assertNotIn("--dashscope", text)
        self.assertNotIn("model_type", text)


if __name__ == "__main__":
    unittest.main()
