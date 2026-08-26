#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import classify_catalog
import free_discovery as discovery
import tunnel_harvest as harvest


class _FakeResponse:
    def __init__(self, *, text: str = "", content: bytes | None = None, url: str = "https://example.org/") -> None:
        self.text = text
        self.content = content if content is not None else text.encode("utf-8")
        self.url = url
        self.headers = {"Content-Type": "text/html; charset=utf-8"}
        self.encoding = "utf-8"
        self.status_code = 200

    def json(self):
        return json.loads(self.text)

    def close(self) -> None:
        pass


class FreeDiscoveryTests(unittest.TestCase):
    def test_private_and_loopback_urls_are_rejected(self) -> None:
        self.assertFalse(discovery.is_public_web_url("http://127.0.0.1:1234/v1"))
        self.assertFalse(discovery.is_public_web_url("http://10.0.0.5/test"))
        self.assertFalse(discovery.is_public_web_url("http://192.168.1.20/test"))

    def test_public_dns_result_is_accepted(self) -> None:
        fake = [(2, 1, 6, "", ("93.184.216.34", 443))]
        with patch("socket.getaddrinfo", return_value=fake):
            self.assertTrue(discovery.is_public_web_url("https://example.org/a"))

    def test_dergipark_oai_endpoint_is_derived_from_journal_url(self) -> None:
        url = "https://dergipark.org.tr/tr/pub/examplejournal/article/12345"
        self.assertEqual(
            discovery.dergipark_oai_endpoint(url),
            "https://dergipark.org.tr/api/public/oai/examplejournal/",
        )

    def test_taxonomy_queries_are_book_aware(self) -> None:
        queries = [q.lower() for q in discovery.taxonomy_queries(max_queries=300)]
        self.assertTrue(any("life cycle cost" in q or "lifecycle cost" in q for q in queries))
        self.assertTrue(any("ventilation" in q and "energy" in q for q in queries))
        self.assertTrue(any("geotechnical" in q for q in queries))

    def test_dedup_prefers_direct_pdf_and_richer_record_for_same_doi(self) -> None:
        base = discovery.DiscoveryRecord(
            title="Tunnel maintenance cost",
            source="x",
            discovery_source="x",
            landing_url="https://example.org/item",
            abstract="short",
            doi="10.1234/tunnel.1",
        )
        richer = discovery.DiscoveryRecord(
            title="Tunnel maintenance cost",
            source="y",
            discovery_source="y",
            landing_url="https://example.org/item",
            pdf_url="https://example.org/item.pdf",
            abstract="much richer abstract about tunnel operation and maintenance cost",
            doi="https://doi.org/10.1234/tunnel.1",
        )
        rows = discovery.deduplicate([base, richer])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].pdf_url, "https://example.org/item.pdf")

    def test_html_parser_finds_relevant_links_and_document_type(self) -> None:
        parsed = discovery.parse_html(
            "<html><head><title>KGM Tunnel Manual</title></head><body>"
            "<a href='/docs/tunnel-spec.pdf'>Tunnel technical specification</a>"
            "</body></html>"
        )
        self.assertIn(("/docs/tunnel-spec.pdf", "Tunnel technical specification"), parsed.links)
        self.assertEqual(
            discovery._guess_web_document_type(parsed.title, "https://example.org/manual", parsed.text),
            "TECHNICAL_STANDARD",
        )

    def test_oai_parser_extracts_relevant_record(self) -> None:
        xml = """<?xml version='1.0' encoding='UTF-8'?>
        <OAI-PMH xmlns='http://www.openarchives.org/OAI/2.0/'
          xmlns:oai_dc='http://www.openarchives.org/OAI/2.0/oai_dc/'
          xmlns:dc='http://purl.org/dc/elements/1.1/'>
          <ListRecords><record><metadata><oai_dc:dc>
            <dc:title>Road tunnel ventilation energy optimization</dc:title>
            <dc:creator>A. Author</dc:creator>
            <dc:description>Energy efficiency in tunnel ventilation systems.</dc:description>
            <dc:identifier>https://example.org/article/1</dc:identifier>
            <dc:date>2025</dc:date>
          </oai_dc:dc></metadata></record></ListRecords>
        </OAI-PMH>"""
        with patch.object(discovery, "is_public_web_url", return_value=True), patch.object(
            discovery, "safe_get", return_value=_FakeResponse(text=xml)
        ):
            rows = discovery.harvest_oai("https://example.org/oai", limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].title, "Road tunnel ventilation energy optimization")
        self.assertEqual(rows[0].year, "2025")

    def test_classify_catalog_includes_discovery_catalog(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="paper_discovery_classify_"))
        harvest.set_output_dir(tmp)
        source = tmp / "discovery_sources" / "web" / "energy" / "source.md"
        source.parent.mkdir(parents=True)
        source.write_text("# Tunnel ventilation energy optimization\nEnergy efficiency for jet fans.", encoding="utf-8")
        sha, size = harvest.hash_pdf(source)
        row = {
            "title": "Tunnel ventilation energy optimization",
            "abstract": "Energy efficiency and jet fan control in road tunnels.",
            "source": "institutional",
            "discovery_source": "institutional:TEST",
            "source_url": "https://example.org/tunnel-energy",
            "source_path": str(source),
            "source_sha256": sha,
            "source_size_bytes": size,
            "document_type": "TECHNICAL_REPORT",
            "source_class": "INT_OFFICIAL",
            "acquisition_status": "SNAPSHOTTED_WEB",
        }
        (tmp / "discovery_catalog.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
        audit = classify_catalog.classify_catalog(tmp, use_local_ai=False)
        self.assertEqual(audit["documents"], 1)
        self.assertEqual(audit["discovery_catalog_rows"], 1)
        index = (tmp / "classification_index.jsonl").read_text(encoding="utf-8")
        self.assertIn("Tunnel ventilation energy optimization", index)
        self.assertIn(str(source), index)


if __name__ == "__main__":
    unittest.main()
