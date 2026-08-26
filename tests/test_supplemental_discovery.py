from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import supplemental_discovery as sd
import free_discovery


class SupplementalDiscoveryTests(unittest.TestCase):
    def test_parse_rss_news_item(self) -> None:
        xml = b'''<?xml version="1.0"?><rss><channel><item>
        <title>Road tunnel fire safety investigation published</title>
        <link>https://example.gov/news/tunnel-fire</link>
        <description>Official road tunnel fire safety and emergency report.</description>
        <pubDate>Wed, 26 Aug 2026 10:00:00 GMT</pubDate>
        </item></channel></rss>'''
        rows = sd.parse_feed(xml, "https://example.gov/rss.xml", "Example Road Authority", "INT_OFFICIAL")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].document_type, "NEWS")
        self.assertEqual(rows[0].source_class, "INT_OFFICIAL")
        self.assertEqual(rows[0].year, "2026")
        self.assertIn("tunnel-fire", rows[0].landing_url or "")

    def test_parse_atom_news_item(self) -> None:
        xml = b'''<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry>
        <title>Tunnel maintenance programme update</title>
        <link href="https://roads.example.gov/news/tunnel-maintenance" />
        <summary>Road tunnel maintenance and operation programme.</summary>
        <updated>2026-08-26T10:00:00Z</updated>
        </entry></feed>'''
        rows = sd.parse_feed(xml, "https://roads.example.gov/atom.xml", "Roads", "INT_OFFICIAL")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].document_type, "NEWS")
        self.assertEqual(rows[0].year, "2026")

    def test_crossref_book_and_chapter_types(self) -> None:
        class FakeResponse:
            def json(self):
                return {"message": {"items": [
                    {"type": "book", "title": ["Road Tunnel Engineering"], "subject": ["tunnel"], "ISBN": ["9781234567890"], "URL": "https://doi.org/10.1/book", "DOI": "10.1/book", "issued": {"date-parts": [[2024]]}},
                    {"type": "book-chapter", "title": ["Tunnel Ventilation"], "subject": ["road tunnel ventilation"], "URL": "https://doi.org/10.1/chapter", "DOI": "10.1/chapter", "issued": {"date-parts": [[2025]]}},
                ]}}
        with patch.object(free_discovery, "safe_get", return_value=FakeResponse()):
            rows = sd.search_crossref_books("road tunnel", 10)
        self.assertEqual([r.document_type for r in rows], ["BOOK", "BOOK_CHAPTER"])
        self.assertEqual(rows[0].extra.get("isbn"), "9781234567890")

    def test_catalog_merge_deduplicates_same_isbn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [
                {"title": "Road Tunnel Engineering", "authors": ["A"], "year": "2020", "extra": {"isbn": "978-1-234-56789-0"}, "metadata_only": True},
                {"title": "Road Tunnel Engineering Second Record", "authors": ["A"], "year": "2020", "extra": {"isbn": "9781234567890"}, "metadata_only": True, "abstract": "richer metadata"},
            ]
            report = sd._append_catalog(root, rows)
            self.assertEqual(report["total"], 1)
            catalog = root / "discovery_catalog.jsonl"
            loaded = [json.loads(line) for line in catalog.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].get("abstract"), "richer metadata")


if __name__ == "__main__":
    unittest.main()
