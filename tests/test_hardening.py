#!/usr/bin/env python3
"""Offline tests for production hardening gates."""

from __future__ import annotations

import hashlib
import json
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import free_discovery as discovery
import handoff_export
import decision_router
import relevance_engine as relevance
import tunnel_harvest as harvest


class RelevanceTests(unittest.TestCase):
    def test_tunnel_engineering_examples_are_strong(self) -> None:
        for title in (
            "Assessment of NATM tunnel lining thickness",
            "TBM excavation method in squeezing rock",
            "tunnel construction settlement prediction",
            "tunnel lining crack detection",
            "tunnel structural defects", "road tunnel fire", "tunnel ventilation",
            "tunnel lighting energy savings", "tunnel maintenance cost",
            "tunnel construction cost", "tunnel lifecycle cost",
            "road tunnel operational cost", "tunnel geological investigation",
            "shield tunnel construction",
        ):
            self.assertEqual(relevance.evaluate({"title": title})["relevance_status"], "STRONG")

    def test_irrelevant_examples_are_rejected(self) -> None:
        for title in (
            "Antiferromagnetic tunnel junctions",
            "Quantum tunneling in materials",
            "Carpal tunnel syndrome",
            "Cubital tunnel syndrome", "Tarsal tunnel syndrome", "FET tunnel device",
            "VPN tunnel configuration", "SSH tunnel guide", "Animal tunnels",
            "Agricultural high tunnel production", "Wind tunnel aerodynamics",
            "Social media marketing", "Natural gas price bubble",
            "Energy reporting standard", "Underwater image restoration", "6G survey",
        ):
            self.assertEqual(relevance.evaluate({"title": title})["relevance_status"], "IRRELEVANT")

    def test_generic_queries_are_tunnel_qualified(self) -> None:
        for query in ("unit cost", "length", "rehabilitation cost"):
            self.assertIn("tunnel", discovery.qualify_tunnel_query(query).lower())


class NetworkAndAcquisitionTests(unittest.TestCase):
    def test_dns_is_not_security_block(self) -> None:
        with patch("socket.getaddrinfo", side_effect=socket.gaierror):
            result = discovery.url_safety("https://api.openaire.eu/graph")
        self.assertFalse(result.safe)
        self.assertEqual(result.reason, "DNS_RESOLUTION_FAILED")

    def test_private_and_loopback_remain_security_blocked(self) -> None:
        self.assertEqual(discovery.url_safety("http://127.0.0.1/a").reason, "LOOPBACK")
        self.assertEqual(discovery.url_safety("http://10.0.0.1/a").reason, "PRIVATE_IP")
        self.assertEqual(discovery.url_safety("http://169.254.1.1/a").reason, "LINK_LOCAL")

    def test_acquisition_failure_statuses(self) -> None:
        record = discovery.DiscoveryRecord(title="Road tunnel", source="test", discovery_source="test")
        self.assertEqual(discovery._acquisition_failure_status(discovery.DNSResolutionError("x"), record), "DNS_FAILURE")
        self.assertEqual(discovery._acquisition_failure_status(discovery.SecurityBlockedError("x"), record), "SECURITY_BLOCKED")
        self.assertEqual(discovery._acquisition_failure_status(PermissionError("x"), record), "ROBOTS_BLOCKED")

    def test_oai_records_are_filtered_before_acquisition(self) -> None:
        rows = [
            discovery.DiscoveryRecord(title="Quantum tunneling in materials", source="oai", discovery_source="oai"),
            discovery.DiscoveryRecord(title="Road tunnel ventilation design", source="oai", discovery_source="oai"),
        ]
        accepted, rejected = discovery.filter_relevant_records(rows)
        self.assertEqual(rejected, 1)
        self.assertEqual([row.title for row in accepted], ["Road tunnel ventilation design"])

    def test_redirect_to_private_network_is_blocked(self) -> None:
        response = SimpleNamespace(status_code=302, headers={"Location": "http://127.0.0.1/private"}, close=lambda: None)
        session = SimpleNamespace(get=lambda *args, **kwargs: response)
        with patch.object(discovery, "url_safety", side_effect=[
            discovery.URLSafetyResult(True, "PUBLIC"),
            discovery.URLSafetyResult(False, "LOOPBACK"),
        ]):
            with self.assertRaises(discovery.SecurityBlockedError):
                discovery.safe_get("https://public.example/start", session=session, respect_robots=False)


class HandoffQueueTests(unittest.TestCase):
    def test_irrelevant_and_review_rows_get_audit_manifests(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="handoff_hardening_"))
        harvest.set_output_dir(root)
        pdf = harvest.PDF_DIR / "source.pdf"
        content = b"%PDF-1.4\nroad tunnel\n%%EOF\n"
        pdf.write_bytes(content)
        sha = hashlib.sha256(content).hexdigest()
        rows = [
            {"title": "Quantum tunneling", "source_path": str(pdf), "source_sha256": sha, "classification_status": "AUTO_ACCEPT", "primary_section": "2.2", "route_path": "C_ACADEMIC/ARTICLES"},
            {"title": "Road tunnel maintenance cost", "source_path": str(pdf), "source_sha256": sha, "classification_status": "NEEDS_REVIEW", "primary_section": "4.3.5", "route_path": "C_ACADEMIC/ARTICLES"},
        ]
        (root / "classification_index.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        report = handoff_export.export_handoff(root)
        audit = Path(report["package_root"]) / "99_audit"
        rejected = [json.loads(line) for line in (audit / "rejected_manifest.jsonl").read_text().splitlines()]
        review = [json.loads(line) for line in (audit / "review_queue.jsonl").read_text().splitlines()]
        self.assertEqual(rejected[0]["reason"], "explicit_irrelevant_or_noncontent")
        self.assertEqual(review, [])
        reclassify = [json.loads(line) for line in (audit / "reclassify_queue.jsonl").read_text().splitlines()]
        self.assertEqual(reclassify[0]["decision"], "RECLASSIFY")


class DecisionRouterTests(unittest.TestCase):
    def record(self, **extra):
        return {
            "title": "Road tunnel maintenance", "relevance_status": "STRONG",
            "classification_status": "AUTO_ACCEPT", "classification_confidence": 0.9,
            "document_type": "TECHNICAL_REPORT", "primary_section": "5.5",
            "source_path": "/tmp/source.pdf", **extra,
        }

    def test_strong_valid_source_auto_handoff(self):
        self.assertEqual(decision_router.route(self.record(), source_exists=True)["decision"], "AUTO_HANDOFF")

    def test_strong_missing_source_retries(self):
        self.assertEqual(decision_router.route(self.record(), source_exists=False)["decision"], "RETRY_ACQUISITION")

    def test_irrelevant_auto_rejects(self):
        row = self.record(relevance_status="IRRELEVANT")
        self.assertEqual(decision_router.route(row, source_exists=True)["decision"], "AUTO_REJECT")

    def test_machine_disagreement_reclassifies(self):
        row = self.record(rule_embedding_disagreement=True)
        self.assertEqual(decision_router.route(row, source_exists=True)["decision"], "AUTO_HANDOFF")

    def test_true_probable_ambiguity_is_manual(self):
        row = self.record(relevance_status="PROBABLE", classification_status="NEEDS_REVIEW")
        self.assertEqual(decision_router.route(row, source_exists=True)["decision"], "RECLASSIFY")

    def test_evidence_level_does_not_block_handoff(self):
        row = self.record(evidence_level="TITLE_METADATA_ONLY")
        self.assertEqual(decision_router.route(row, source_exists=True)["decision"], "AUTO_HANDOFF")


if __name__ == "__main__":
    unittest.main()
