#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import handoff_export
import presentation_sources
import tunnel_harvest as harvest


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (8, 6), (20, 80, 140)).save(output, format="PNG")
    return output.getvalue()


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.url = "https://zenodo.org/api/records?q=tunnel"

    def json(self) -> dict:
        return self._payload

    def close(self) -> None:
        pass


class PresentationDiscoveryTests(unittest.TestCase):
    def test_zenodo_adapter_preserves_platform_producer_and_download_metadata(self) -> None:
        payload = {"hits": {"hits": [{
            "metadata": {
                "title": "Road tunnel fire safety presentation",
                "upload_type": "presentation",
                "publication_date": "2025-04-02",
                "doi": "10.5281/zenodo.123",
                "creators": [{"name": "A. Engineer", "affiliation": "Example Road Authority"}],
            },
            "links": {"html": "https://zenodo.org/records/123"},
            "files": [{"key": "slides.pptx", "links": {"content": "https://zenodo.org/records/123/files/slides.pptx"}}],
        }]}}
        cfg = {
            "platforms": {"zenodo": {
                "enabled": True, "adapter": "zenodo_api", "domains": ["zenodo.org"],
                "search_url": "https://zenodo.org/api/records?q={query}&size={limit}",
            }},
            "policy": {"max_queries_per_platform": 1},
        }
        with patch.object(presentation_sources, "_config", return_value=cfg):
            rows, errors = presentation_sources.discover_public_presentations(
                ["road tunnel fire"], per_platform=5, safe_get=lambda *_a, **_k: _Response(payload),
            )
        self.assertFalse(errors)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["platform"], "ZENODO")
        self.assertEqual(rows[0]["organization"], "Example Road Authority")
        self.assertTrue(rows[0]["download_url"].endswith("slides.pptx"))


class PresentationAssetTests(unittest.TestCase):
    def test_pptx_images_have_slide_level_provenance_and_sha(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="presentation_assets_"))
        deck = root / "deck.pptx"
        relationships = """<?xml version='1.0' encoding='UTF-8'?>
        <Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>
          <Relationship Id='rId1' Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/image' Target='../media/image1.png'/>
        </Relationships>"""
        with zipfile.ZipFile(deck, "w") as archive:
            archive.writestr("ppt/presentation.xml", "<presentation/>")
            archive.writestr("ppt/slides/slide1.xml", "<slide/>")
            archive.writestr("ppt/slides/_rels/slide1.xml.rels", relationships)
            archive.writestr("ppt/media/image1.png", _png())
        deck_sha = hashlib.sha256(deck.read_bytes()).hexdigest()
        report = presentation_sources.extract_slide_assets(
            deck, root / "assets", source_url="https://example.org/deck.pptx", deck_sha256=deck_sha,
        )
        self.assertEqual(report["slide_count"], 1)
        self.assertEqual(len(report["assets"]), 1)
        asset = report["assets"][0]
        self.assertEqual(asset["slide_number"], 1)
        self.assertEqual(asset["deck_sha256"], deck_sha)
        self.assertEqual(hashlib.sha256(Path(asset["path"]).read_bytes()).hexdigest(), asset["sha256"])

    def test_handoff_packages_presentation_assets_and_checksums(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="presentation_handoff_"))
        harvest.set_output_dir(root)
        deck = root / "source.pptx"
        deck.write_bytes(b"PK presentation test")
        deck_sha = hashlib.sha256(deck.read_bytes()).hexdigest()
        image = root / "slide.png"
        image.write_bytes(_png())
        image_sha = hashlib.sha256(image.read_bytes()).hexdigest()
        row = {
            "title": "Road tunnel fire safety presentation",
            "source_path": str(deck), "source_sha256": deck_sha,
            "source_url": "https://example.org/deck.pptx",
            "resolved_url": "https://example.org/deck.pptx",
            "acquisition_status": "DOWNLOADED_PRESENTATION",
            "document_type": "TECHNICAL_PRESENTATION", "source_class": "RESEARCH_REPOSITORY",
            "authority_tier": "E2", "classification_status": "AUTO_ACCEPT",
            "classification_confidence": 0.95, "relevance_status": "STRONG",
            "tunnel_relevance_score": 0.95, "route_path": "E_PRESENTATIONS",
            "presentation": {"platform": "ZENODO", "slide_count": 1},
            "presentation_assets": [{
                "kind": "presentation_slide_image", "path": str(image), "sha256": image_sha,
                "slide_number": 1, "source_url": "https://example.org/deck.pptx",
                "deck_sha256": deck_sha, "extraction_method": "pptx_relationship",
                "media_type": "image/png", "width": 8, "height": 6,
            }],
        }
        (root / "classification_index.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
        report = handoff_export.export_handoff(root)
        self.assertEqual(report["ready_for_handoff"], 1)
        self.assertEqual(report["presentation_assets"], 1)
        package = Path(report["package_root"])
        manifest = json.loads((package / "00_registry" / "handoff_manifest.jsonl").read_text().strip())
        asset = manifest["presentation_assets"][0]
        self.assertEqual(asset["slide_number"], 1)
        self.assertTrue((package / asset["path"]).is_file())
        checksums = (package / "00_registry" / "checksums.sha256").read_text()
        self.assertIn(image_sha, checksums)


if __name__ == "__main__":
    unittest.main()
