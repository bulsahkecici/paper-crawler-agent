#!/usr/bin/env python3
"""PaperCrawler <-> TunnelBookAI boundary contract tests (offline)."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import classify_catalog
import corpus_policy
import decision_router
import handoff_export
import handoff_quality_gate
import light_pdf_extract
import tunnel_harvest as harvest


def _pdf(root: Path, name: str, body: bytes) -> tuple[Path, str]:
    path = root / "pdfs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path, hashlib.sha256(body).hexdigest()


class EvidenceSemanticsTests(unittest.TestCase):
    def test_crawler_evidence_level_never_full_text(self) -> None:
        for record in (
            {"full_text": "x" * 5000, "source_path": "/tmp/a.pdf"},
            {"text": "y" * 9000},
            {"classification_input": "LIGHT_PDF_FIRST_PAGES"},
            {"crawler_evidence_level": "PDF_EXTRACT"},
        ):
            level = corpus_policy.crawler_evidence_level(record)
            self.assertNotIn(level, corpus_policy.FORBIDDEN_CRAWLER_EVIDENCE_LEVELS)
            self.assertIn(level, corpus_policy.CRAWLER_EVIDENCE_LEVELS)

    def test_light_pdf_first_pages_maps_to_light_pdf_text(self) -> None:
        self.assertEqual(
            corpus_policy.crawler_evidence_level({"classification_input": "LIGHT_PDF_FIRST_PAGES"}),
            "LIGHT_PDF_TEXT",
        )

    def test_legacy_alias_is_accepted_but_downgraded(self) -> None:
        self.assertEqual(corpus_policy.crawler_evidence_level({"crawler_evidence_level": "WEBPAGE_TEXT"}), "WEB_SNAPSHOT_TEXT")
        self.assertEqual(corpus_policy.crawler_evidence_level({"crawler_evidence_level": "FULL_TEXT"}), "LIGHT_PDF_TEXT")

    def test_no_text_layer_is_title_metadata_only(self) -> None:
        self.assertEqual(corpus_policy.crawler_evidence_level({"title": "Tunnel"}), "TITLE_METADATA_ONLY")

    def test_light_pdf_text_never_becomes_an_abstract(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="light_pdf_sep_"))
        harvest.set_output_dir(root)
        (root / "catalog.json").write_text(json.dumps({"papers": []}), encoding="utf-8")
        pdf, sha = _pdf(root, "t.pdf", b"%PDF-1.4\n%%EOF\n")
        row = {  # shape produced by light_pdf_extract for a text-layer PDF, no real abstract
            "title": "Road tunnel ventilation energy optimization", "source": "crossref",
            "discovery_source": "crossref", "discovery_query": "road tunnel ventilation",
            "source_path": str(pdf), "source_sha256": sha, "acquisition_status": "DOWNLOADED_PDF",
            "document_type": "JOURNAL_ARTICLE", "source_url": "https://ex.org/a",
            "light_pdf_text": "Road tunnel ventilation and jet fan energy optimization for operating cost reduction.",
            "classification_input": "LIGHT_PDF_FIRST_PAGES", "light_pdf_text_extracted": True,
            "crawler_evidence_level": "LIGHT_PDF_TEXT",
        }
        (root / "discovery_catalog.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
        classify_catalog.classify_catalog(root, use_local_ai=False)
        payload = json.loads((root / "classification_index.jsonl").read_text().splitlines()[0])
        self.assertEqual(payload["abstract"], "")
        self.assertIn("ventilation", payload["classification_text"])
        self.assertEqual(payload["classification_input"], "LIGHT_PDF_FIRST_PAGES")
        self.assertEqual(payload["crawler_evidence_level"], "LIGHT_PDF_TEXT")
        self.assertIn("ventilation", payload["topics"])
        self.assertNotIn("primary_section", payload)

    def test_light_pdf_audit_has_no_full_text_metric(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="light_pdf_boundary_"))
        harvest.set_output_dir(root)
        pdf, _sha = _pdf(root, "scan.pdf", b"%PDF-1.4\n%%EOF\n")  # no text layer
        (root / "discovery_catalog.jsonl").write_text(
            json.dumps({"title": "Road tunnel", "source_path": str(pdf)}) + "\n", encoding="utf-8"
        )
        report = light_pdf_extract.enrich_catalog(root)
        self.assertNotIn("extracted_full_text", report)
        self.assertIn("light_pdf_text_extracted", report)
        self.assertEqual(report["semantic_boundary"], "Light PDF text is provisional classification input and is not full-text evidence.")


class MetadataReferenceTests(unittest.TestCase):
    def _record(self, **extra):
        return {
            "title": "Road Tunnel Engineering Handbook", "relevance_status": "STRONG",
            "classification_status": "AUTO_ACCEPT", "classification_confidence": 0.9,
            "document_type": "BOOK", "primary_section": "2.2", **extra,
        }

    def test_official_metadata_only_routes_to_metadata_reference(self) -> None:
        row = self._record(source_class="INT_OFFICIAL", source_url="https://piarc.org/x", metadata_only=True)
        self.assertEqual(decision_router.route(row, source_exists=False)["decision"], "METADATA_REFERENCE")

    def test_isbn_only_book_routes_to_metadata_reference(self) -> None:
        row = self._record(isbn="978-1-234-56789-0", metadata_only=True, source_url="https://pub.example/book")
        self.assertEqual(decision_router.route(row, source_exists=False)["decision"], "METADATA_REFERENCE")

    def test_plain_missing_source_still_retries(self) -> None:
        row = self._record(source_url="https://example.org/x")
        self.assertEqual(decision_router.route(row, source_exists=False)["decision"], "RETRY_ACQUISITION")

    def test_official_source_with_url_is_retried_before_reference_fallback(self) -> None:
        # An acquirable official page must be reacquired, not filed as metadata.
        row = self._record(source_class="INT_OFFICIAL", pdf_url="https://fhwa.dot.gov/tunnel.pdf")
        self.assertEqual(decision_router.route(row, source_exists=False)["decision"], "RETRY_ACQUISITION")

    def test_official_source_hard_failure_is_kept_as_reference(self) -> None:
        row = self._record(source_class="INT_OFFICIAL", source_url="https://piarc.org/x", acquisition_status="LOGIN_ONLY")
        self.assertEqual(decision_router.route(row, source_exists=False)["decision"], "METADATA_REFERENCE")

    def test_metadata_reference_excluded_from_ready_and_written_to_queue(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="metaref_export_"))
        harvest.set_output_dir(root)
        pdf, sha = _pdf(root, "real.pdf", b"%PDF-1.4\nroad tunnel lifecycle cost\n%%EOF\n")
        rows = [
            {  # physical -> READY_FOR_HANDOFF
                "title": "Road tunnel life cycle cost", "abstract": "Road tunnel construction and maintenance life cycle cost.",
                "source_path": str(pdf), "source_sha256": sha, "classification_status": "AUTO_ACCEPT",
                "classification_confidence": 0.95, "primary_section": "4.3.5", "relevance_status": "STRONG",
                "document_type": "JOURNAL_ARTICLE", "route_path": "C_ACADEMIC/ARTICLES",
            },
            {  # official, no file -> METADATA_REFERENCE
                "title": "PIARC Road Tunnels Manual entry", "abstract": "Road tunnel operation and safety guidance.",
                "source_class": "INT_OFFICIAL", "source_url": "https://piarc.org/manual/entry",
                "metadata_only": True, "classification_status": "AUTO_ACCEPT", "classification_confidence": 0.9,
                "primary_section": "5.4", "relevance_status": "STRONG", "document_type": "TECHNICAL_GUIDELINE",
                "route_path": "A_OFFICIAL/INTERNATIONAL/PIARC",
            },
        ]
        (root / "classification_index.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        report = handoff_export.export_handoff(root)
        self.assertEqual(report["ready_for_handoff"], 1)
        self.assertEqual(report["metadata_references"], 1)
        package = Path(report["package_root"])
        refs = [json.loads(l) for l in (package / "99_audit" / "metadata_references.jsonl").read_text().splitlines() if l.strip()]
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["decision"], "METADATA_REFERENCE")
        manifest = [json.loads(l) for l in (package / "00_registry" / "handoff_manifest.jsonl").read_text().splitlines() if l.strip()]
        self.assertEqual(len(manifest), 1)
        self.assertFalse(any(m.get("metadata_only_official_exception") for m in manifest))


class HandoffContractTests(unittest.TestCase):
    def _export(self) -> Path:
        root = Path(tempfile.mkdtemp(prefix="contract_export_"))
        harvest.set_output_dir(root)
        pdf, sha = _pdf(root, "p.pdf", b"%PDF-1.4\nroad tunnel ventilation energy\n%%EOF\n")
        row = {
            "title": "Road tunnel ventilation energy", "abstract": "Road tunnel ventilation energy optimization study.",
            "source_path": str(pdf), "source_sha256": sha, "classification_status": "AUTO_ACCEPT",
            "classification_confidence": 0.95, "primary_section": "5.7.2", "relevance_status": "STRONG",
            "document_type": "JOURNAL_ARTICLE", "route_path": "C_ACADEMIC/ARTICLES",
            "book_sections": [{"id": "5.7.2", "score": 0.95}, {"id": "5.7", "score": 0.6}],
        }
        (root / "classification_index.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
        handoff_export.export_handoff(root)
        return Path(root) / "exports" / "TunnelBookAI_Source_Pack"

    def test_contract_is_versioned_with_responsibilities_and_rules(self) -> None:
        contract = json.loads((self._export() / "00_registry" / "handoff_contract.json").read_text())
        self.assertEqual(contract["schema_version"].split(".")[0], "2")
        self.assertIn("full content conversion", contract["consumer_responsibilities"])
        self.assertIn("discovery", contract["producer_responsibilities"])
        self.assertIn("READY_FOR_HANDOFF", contract["semantic_rules"])
        self.assertIn("METADATA_REFERENCE", contract["semantic_rules"])

    def test_manifest_is_book_agnostic(self) -> None:
        manifest = [json.loads(l) for l in (self._export() / "00_registry" / "handoff_manifest.jsonl").read_text().splitlines() if l.strip()]
        row = manifest[0]
        self.assertNotIn("provisional_primary_section", row)
        self.assertNotIn("final_primary_section", row)
        self.assertIn("topics", row)
        self.assertEqual(row["source_representation"]["crawler_normalized_status"], "PROVISIONAL")
        self.assertNotIn(row["crawler_evidence_level"], {"FULL_TEXT", "PDF_EXTRACT"})

    def test_gate_never_blocks_on_boundary_violations_for_clean_package(self) -> None:
        package = self._export()
        result = handoff_quality_gate.evaluate_handoff(package.parent.parent, package_root=package)
        boundary_blockers = {
            "invalid_papercrawler_fulltext_claim", "metadata_reference_marked_ready",
            "invalid_provisional_section", "handoff_contract_schema_invalid",
            "source_representation_missing",
        }
        self.assertEqual(boundary_blockers & set(result["blocking_issues"]), set())


if __name__ == "__main__":
    unittest.main()
