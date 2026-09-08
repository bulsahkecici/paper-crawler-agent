#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import handoff_export
import handoff_quality_gate
import pipeline_state
import tunnel_harvest as harvest


def _ready_root() -> Path:
    root = Path(tempfile.mkdtemp(prefix="handoff_release_"))
    harvest.set_output_dir(root)
    audit = root / "audit"
    audit.mkdir()
    stages = {stage: "COMPLETED" for stage in pipeline_state.STAGES}
    (audit / "pipeline_state.json").write_text(json.dumps({"stages": stages}))
    (audit / "classification_audit.json").write_text(json.dumps({
        "reconciliation": {"invariant_ok": True, "dedup_removed": 0},
        "coverage": {"basis": "book_agnostic_broad_topics", "informational_only": True, "topics": {}},
    }))
    return root


def _web_row(root: Path, *, route: str = "B_TECHNICAL/WEB") -> tuple[dict, bytes, bytes]:
    normalized = b"# Road tunnel ventilation\n\nNormalized crawler snapshot.\n"
    raw = b"<html><body><h1>Road tunnel ventilation</h1><p>Energy study.</p></body></html>"
    source_dir = root / "web_fixture"
    source_dir.mkdir()
    source = source_dir / "source.md"
    raw_source = source_dir / "source_raw.html"
    source.write_bytes(normalized)
    raw_source.write_bytes(raw)
    return ({
        "title": "Road tunnel life cycle cost",
        "abstract": "Road tunnel construction, operation and maintenance life cycle cost.",
        "source_path": str(source),
        "source_sha256": hashlib.sha256(normalized).hexdigest(),
        "raw_html_path": str(raw_source),
        "raw_html_sha256": hashlib.sha256(raw).hexdigest(),
        "source_url": "https://example.org/tunnel",
        "discovery_source": "web",
        "document_type": "TECHNICAL_REPORT",
        "source_class": "ROAD_AUTHORITY",
        "authority_tier": "B1",
        "classification_status": "AUTO_ACCEPT",
        "classification_confidence": 0.95,
        "relevance_status": "STRONG",
        "route_path": route,
    }, normalized, raw)


class HandoffReleaseTests(unittest.TestCase):
    def test_raw_html_is_authoritative_and_markdown_is_provisional(self) -> None:
        root = _ready_root()
        row, normalized, raw = _web_row(root)
        (root / "classification_index.jsonl").write_text(json.dumps(row) + "\n")

        report = handoff_export.export_handoff(root)
        package = Path(report["package_root"])
        manifest = json.loads((package / "00_registry" / "handoff_manifest.jsonl").read_text())
        representation = manifest["source_representation"]

        self.assertTrue(manifest["local_path"].endswith("source_raw.html"))
        self.assertEqual(manifest["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(representation["original_or_raw"], manifest["local_path"])
        self.assertEqual(representation["original_or_raw_sha256"], manifest["sha256"])
        self.assertTrue(representation["crawler_normalized"].endswith("source.md"))
        self.assertEqual(
            representation["crawler_normalized_sha256"], hashlib.sha256(normalized).hexdigest()
        )
        self.assertEqual(representation["crawler_normalized_status"], "PROVISIONAL")
        checksums = (package / "00_registry" / "checksums.sha256").read_text()
        self.assertIn(representation["original_or_raw"], checksums)
        self.assertIn(representation["crawler_normalized"], checksums)
        self.assertIn("99_audit/handoff_quality_gate.json", checksums)
        self.assertEqual(report["quality_gate"]["decision"], "GO")

    def test_needs_classification_cannot_be_ready(self) -> None:
        root = _ready_root()
        row, _, _ = _web_row(root, route="90_STAGING/NEEDS_CLASSIFICATION")
        row["source_class"] = "UNKNOWN"
        (root / "classification_index.jsonl").write_text(json.dumps(row) + "\n")

        report = handoff_export.export_handoff(root)
        self.assertEqual(report["ready_for_handoff"], 0)
        self.assertEqual(report["reclassify"], 1)
        queue = Path(report["package_root"]) / "99_audit" / "reclassify_queue.jsonl"
        self.assertEqual(json.loads(queue.read_text())["reason"], "source_classification_incomplete")

    def test_gate_blocks_ready_record_in_needs_classification(self) -> None:
        root = _ready_root()
        row, _, _ = _web_row(root)
        (root / "classification_index.jsonl").write_text(json.dumps(row) + "\n")
        report = handoff_export.export_handoff(root)
        package = Path(report["package_root"])
        manifest_path = package / "00_registry" / "handoff_manifest.jsonl"
        manifest = json.loads(manifest_path.read_text())
        manifest["route_path"] = "90_STAGING/NEEDS_CLASSIFICATION"
        manifest_path.write_text(json.dumps(manifest) + "\n")

        gate = handoff_quality_gate.evaluate_handoff(root, package_root=package)
        self.assertEqual(gate["decision"], "NO_GO")
        self.assertIn("needs_classification_marked_ready", gate["blocking_issues"])

    def test_release_is_copied_fingerprinted_and_not_overwritten(self) -> None:
        root = _ready_root()
        row, _, raw = _web_row(root)
        (root / "classification_index.jsonl").write_text(json.dumps(row) + "\n")
        releases = root / "releases"

        report = handoff_export.create_release(
            root, releases_root=releases, generated_at="2026-09-08T12:00:00+00:00"
        )
        package = Path(report["package_root"])
        metadata = json.loads((package / "00_registry" / "release_metadata.json").read_text())
        complete = json.loads((package / "00_registry" / "RELEASE_COMPLETE.json").read_text())
        manifest = json.loads((package / "00_registry" / "handoff_manifest.jsonl").read_text())
        exported = package / manifest["local_path"]

        self.assertEqual(package.name, metadata["release_id"])
        self.assertIn(metadata["manifest_sha256"][:12], metadata["release_id"])
        self.assertEqual(complete["status"], "COMPLETE")
        self.assertNotEqual(exported.stat().st_ino, Path(row["raw_html_path"]).stat().st_ino)
        Path(row["raw_html_path"]).write_bytes(b"changed after release")
        self.assertEqual(exported.read_bytes(), raw)
        Path(row["raw_html_path"]).write_bytes(raw)
        with self.assertRaises(FileExistsError):
            handoff_export.create_release(
                root, releases_root=releases, generated_at="2026-09-08T12:00:00+00:00"
            )


if __name__ == "__main__":
    unittest.main()
