#!/usr/bin/env python3
"""Two-record, no-network smoke test through classification and coverage."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import classify_catalog
import tunnel_harvest as harvest


class ControlledPipelineSmokeTest(unittest.TestCase):
    def test_two_tunnel_domains_flow_through_real_classifier(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="paper_crawler_smoke_"))
        harvest.set_output_dir(root)
        (root / "catalog.json").write_text(json.dumps({"papers": []}), encoding="utf-8")
        rows = []
        for index, (title, abstract, query) in enumerate((
            ("NATM tunnel support design", "Road tunnel shotcrete and rock bolt support design.", "road tunnel NATM support"),
            ("Road tunnel maintenance operating cost", "Tunnel operation, maintenance, energy and staff cost.", "road tunnel maintenance operating cost"),
        ), 1):
            pdf = root / "pdfs" / f"smoke_{index}.pdf"
            pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
            rows.append({
                "title": title, "abstract": abstract, "source": "crossref", "discovery_source": "crossref",
                "discovery_query": query, "source_path": str(pdf), "source_sha256": f"{index:064x}",
                "acquisition_status": "DOWNLOADED_PDF", "document_type": "JOURNAL_ARTICLE",
            })
        (root / "discovery_catalog.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        report = classify_catalog.classify_catalog(root, use_local_ai=False)
        self.assertEqual(report["documents"], 2)
        self.assertTrue(report["reconciliation"]["invariant_ok"])
        self.assertEqual(report["coverage"]["basis"], "book_agnostic_broad_topics")
        self.assertTrue(report["coverage"]["informational_only"])


if __name__ == "__main__":
    unittest.main()
