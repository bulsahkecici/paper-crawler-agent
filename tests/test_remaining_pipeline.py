#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import light_pdf_extract
import source_dedup_audit
import tunnel_harvest as harvest
import tunnelbookai_importer


class RemainingPipelineTests(unittest.TestCase):
    def test_light_pdf_extract_reports_invalid_pdf_without_crash(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="light_pdf_"))
        bad = root / "bad.pdf"
        bad.write_bytes(b"not-a-real-pdf")
        result = light_pdf_extract.extract_first_pages(bad)
        self.assertFalse(result["text_layer_available"])
        self.assertTrue(result["extraction_error"])

    def test_dedup_audit_identifies_same_doi(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="dedup_"))
        harvest.set_output_dir(root)
        rows = [
            {"title": "Tunnel Cost", "doi": "10.1000/example", "source_sha256": "a" * 64, "source_path": "/a"},
            {"title": "Tunnel Cost", "doi": "https://doi.org/10.1000/example", "source_sha256": "b" * 64},
        ]
        with (root / "classification_index.jsonl").open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        report = source_dedup_audit.audit(root)
        self.assertEqual(report["same_doi_groups"], 1)
        self.assertEqual(len(report["same_doi"][0]), 2)

    def test_importer_validates_checksum_and_builds_docling_plan(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="tbai_import_"))
        registry = root / "00_registry"
        source_dir = root / "01_originals" / "C_ACADEMIC" / "ARTICLES" / "DOC1"
        registry.mkdir(parents=True)
        source_dir.mkdir(parents=True)
        source = source_dir / "source.pdf"
        content = b"%PDF-1.4\nsource\n%%EOF\n"
        source.write_bytes(content)
        sha = hashlib.sha256(content).hexdigest()
        rel = source.relative_to(root).as_posix()
        manifest = {
            "document_id": "DOC1",
            "source_path": rel,
            "source_sha256": sha,
            "document_type": "JOURNAL_ARTICLE",
            "source_class": "ACADEMIC",
            "authority_tier": "B2",
            "primary_section": "4.3.5",
            "book_sections": [{"id": "4.3.5", "score": 0.95}],
            "topics": ["life_cycle_cost"],
            "paper_crawler_status": "READY_FOR_HANDOFF",
        }
        (registry / "manifest.jsonl").write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        (registry / "checksums.sha256").write_text(f"{sha}  {rel}\n", encoding="utf-8")
        (registry / "handoff_contract.json").write_text(json.dumps({"schema_version": "1.0"}), encoding="utf-8")

        report = tunnelbookai_importer.validate_package(root)
        self.assertEqual(report["ready_for_docling"], 1)
        self.assertFalse(report["canonical_corpus_modified"])
        plan = Path(report["ingest_plan"]).read_text(encoding="utf-8")
        self.assertIn("READY_FOR_DOCLING", plan)
        self.assertIn("DOCLING_FULLTEXT_AND_QUALITY_AUDIT", plan)


if __name__ == "__main__":
    unittest.main()
