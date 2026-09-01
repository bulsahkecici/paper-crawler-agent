#!/usr/bin/env python3

from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

import bibliographic_dedup as bd
import corpus_quality_gate
import coverage_policy
import free_discovery as discovery
import gap_discovery
import pipeline_state
import relevance_engine
import source_health


def _record(section: str = "5.5.1", status: str = "LLM_ACCEPTED", **extra):
    return {
        "primary_section": section, "book_sections": [{"id": section, "score": 0.9}],
        "classification_status": status, "classification_confidence": 0.9,
        "abstract": "road tunnel maintenance", "handoff_candidate": True, **extra,
    }


class CoveragePolicyTests(unittest.TestCase):
    def test_subsection_rolls_up_to_all_parents(self):
        sections = coverage_policy.calculate([_record()])["sections"]
        self.assertEqual([sections[s]["corpus_eligible_count"] for s in ("5", "5.5", "5.5.1")], [1, 1, 1])

    def test_accepted_count(self):
        self.assertEqual(coverage_policy.calculate([_record()])["totals"]["accepted_count"], 1)

    def test_rejected_excluded(self):
        totals = coverage_policy.calculate([_record(status="REJECT_IRRELEVANT")])["totals"]
        self.assertEqual(totals.get("discovered_count", 0), 0)

    def test_review_inclusion_is_configurable(self):
        row = _record(status="NEEDS_REVIEW")
        self.assertEqual(coverage_policy.calculate([row])["totals"]["discovered_count"], 1)
        self.assertEqual(coverage_policy.calculate([row], include_review_in_discovered=False)["totals"].get("discovered_count", 0), 0)

    def test_discovered_and_eligible_are_distinct(self):
        totals = coverage_policy.calculate([_record(status="NEEDS_REVIEW")])["totals"]
        self.assertEqual(totals["discovered_count"], 1)
        self.assertEqual(totals.get("corpus_eligible_count", 0), 0)


class QueryAnchorTests(unittest.TestCase):
    def test_generic_gets_anchor(self):
        self.assertEqual(gap_discovery.ensure_domain_anchor("maintenance"), "road tunnel maintenance")

    def test_existing_anchor_unchanged(self):
        self.assertEqual(gap_discovery.ensure_domain_anchor("tunnel construction cost"), "tunnel construction cost")

    def test_turkish_anchor(self):
        self.assertEqual(gap_discovery.ensure_domain_anchor("bakım maliyeti"), "karayolu tüneli bakım maliyeti")

    def test_section_context_influences_query(self):
        self.assertEqual(gap_discovery.ensure_domain_anchor("staff cost", "Operation economics"), "road tunnel operation staff cost")


class PrefilterTests(unittest.TestCase):
    def _status(self, title, url):
        return relevance_engine.noncontent_decision({"title": title, "source_url": url})["noncontent_status"]

    def test_piarc_login_rejected(self): self.assertEqual(self._status("Login", "https://piarc.org/login"), "REJECT_NONCONTENT_PAGE")
    def test_subscribe_rejected(self): self.assertEqual(self._status("Subscribe", "https://x.org/subscribe"), "REJECT_NONCONTENT_PAGE")
    def test_privacy_rejected(self): self.assertEqual(self._status("Privacy Policy", "https://x.org/privacy-policy"), "REJECT_NONCONTENT_PAGE")
    def test_technical_manual_accepted(self): self.assertEqual(self._status("Tunnel operation manual", "https://x.org/manual"), "CONTENT_CANDIDATE")
    def test_operation_page_accepted(self): self.assertEqual(self._status("Road tunnel operation", "https://x.org/technical/operation"), "CONTENT_CANDIDATE")


class DedupTests(unittest.TestCase):
    def test_same_doi(self): self.assertEqual(bd.duplicate_reason({"doi": "10.1234/x"}, {"doi": "https://doi.org/10.1234/x"}), "DOI")
    def test_same_sha(self): self.assertEqual(bd.duplicate_reason({"source_sha256": "aa"}, {"source_sha256": "aa"}), "SHA256")
    def test_normalized_title(self): self.assertEqual(bd.duplicate_reason({"title": "Road—Tunnel Cost", "year": 2020}, {"title": "road tunnel cost!", "year": 2020}), "TITLE_EXACT")
    def test_fuzzy_title_year_author(self): self.assertEqual(bd.duplicate_reason({"title": "Life cycle costs of road tunnels", "year": 2020, "authors": ["A Smith"]}, {"title": "Life-cycle cost of road tunnels", "year": 2020, "authors": ["A Smith"]}), "TITLE_FUZZY")
    def test_same_title_different_work_not_merged(self): self.assertIsNone(bd.duplicate_reason({"title": "Tunnel Safety", "year": 2019, "authors": ["A"]}, {"title": "Tunnel Safety", "year": 2024, "authors": ["B"]}))


class ResumeTests(unittest.TestCase):
    def test_legacy_artifacts_bootstrap_at_gap(self):
        root = Path(tempfile.mkdtemp())
        audit = root / "audit"; audit.mkdir()
        (root / "discovery_catalog.jsonl").write_text(json.dumps({"title": "Tunnel"}) + "\n")
        (audit / "discovery_audit.json").write_text("{}")
        (audit / "light_pdf_extract_audit.json").write_text("{}")
        (root / "classification_index.jsonl").write_text(json.dumps({"title": "Tunnel"}) + "\n")
        (audit / "classification_audit.json").write_text(json.dumps({"documents": 1}))
        state = pipeline_state.PipelineState(root)
        adopted = state.bootstrap_legacy()
        self.assertEqual(adopted["initial_classification"], "COMPLETED")
        self.assertEqual(adopted["gap_discovery"], "PARTIAL")

    def test_completed_stage_skipped(self):
        root = Path(tempfile.mkdtemp())
        state = pipeline_state.PipelineState(root)
        state.mark("free_discovery", "COMPLETED")
        called = []
        self.assertIsNone(state.run("free_discovery", lambda: called.append(1)))
        self.assertFalse(called)

    def test_partial_stage_resumes(self):
        root = Path(tempfile.mkdtemp())
        state = pipeline_state.PipelineState(root)
        state.mark("gap_discovery", "PARTIAL")
        self.assertEqual(state.run("gap_discovery", lambda: 7), 7)
        self.assertTrue(state.completed("gap_discovery"))

    def test_successful_pdf_is_not_downloaded_twice(self):
        root = Path(tempfile.mkdtemp())
        pdf = root / "pdfs" / "Road_tunnel_support.pdf"
        pdf.parent.mkdir()
        pdf.write_bytes(b"%PDF-1.4\n%%EOF")
        row = discovery.DiscoveryRecord(title="Road tunnel support", source="x", discovery_source="x", pdf_url="https://example.org/a.pdf")
        with patch.object(discovery, "secure_download_pdf") as download:
            result = discovery.acquire_record(row, root)
        download.assert_not_called()
        self.assertEqual(result["acquisition_status"], "DOWNLOADED_PDF")

    def test_interrupt_is_recoverable(self):
        root = Path(tempfile.mkdtemp())
        state = pipeline_state.PipelineState(root)
        with self.assertRaises(KeyboardInterrupt):
            state.run("gap_discovery", lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
        self.assertEqual(pipeline_state.PipelineState(root).status("gap_discovery"), "PARTIAL")


class SourceHealthTests(unittest.TestCase):
    def test_core_disabled_after_two_failures(self):
        registry = source_health.SourceHealthRegistry(Path(tempfile.mkdtemp()) / "health.json", cooldown_seconds=60)
        registry.failure("core", "timeout"); registry.failure("core", "timeout")
        self.assertFalse(registry.available("core"))

    def test_stage_change_keeps_breaker(self):
        path = Path(tempfile.mkdtemp()) / "health.json"
        registry = source_health.SourceHealthRegistry(path); registry.failure("core", "timeout"); registry.failure("core", "timeout")
        self.assertFalse(source_health.SourceHealthRegistry(path).available("core"))

    def test_openalex_429_backoff(self):
        calls = []
        response = SimpleNamespace(status_code=429, headers={"Retry-After": "0"})
        def provider(query, limit):
            calls.append(1)
            if len(calls) == 1:
                raise requests.HTTPError("429", response=response)
            return []
        with patch("time.sleep"), patch("random.uniform", return_value=0):
            self.assertEqual(discovery._provider_call("openalex", provider, "tunnel", 2), [])
        self.assertEqual(len(calls), 2)

    def test_success_resets_failure_count(self):
        registry = source_health.SourceHealthRegistry(Path(tempfile.mkdtemp()) / "health.json")
        registry.failure("core", "timeout"); registry.success("core")
        self.assertEqual(registry.data["core"]["consecutive_failures"], 0)

    def test_robots_cache_is_host_scoped(self):
        discovery._ROBOTS_CACHE.clear()
        response = SimpleNamespace(status_code=200, text="User-agent: *\nAllow: /", headers={})
        session = SimpleNamespace(calls=0)
        def get(*args, **kwargs): session.calls += 1; return response
        session.get = get
        with patch.object(discovery, "url_safety", return_value=discovery.URLSafetyResult(True, "PUBLIC")):
            self.assertTrue(discovery._robots_allowed("https://example.org/a", session))
            self.assertTrue(discovery._robots_allowed("https://example.org/b", session))
        self.assertEqual(session.calls, 1)


class QualityGateTests(unittest.TestCase):
    def fixture(self) -> Path:
        root = Path(tempfile.mkdtemp())
        audit = root / "audit"; audit.mkdir()
        stages = {stage: "COMPLETED" for stage in pipeline_state.STAGES}
        (audit / "pipeline_state.json").write_text(json.dumps({"stages": stages}))
        (audit / "classification_audit.json").write_text(json.dumps({"reconciliation": {"invariant_ok": True, "dedup_removed": 1}, "coverage": {"parent_aggregation": True}}))
        (root / "classification_index.jsonl").write_text(json.dumps({"evidence_level": "ABSTRACT"}) + "\n")
        package = root / "exports" / "TunnelBookAI_Source_Pack"; (package / "00_registry").mkdir(parents=True); (package / "99_audit").mkdir()
        source = package / "source.pdf"; source.write_bytes(b"pdf")
        manifest = {"canonical_id": "CAN_1", "sha256": hashlib.sha256(b"pdf").hexdigest(), "local_path": "source.pdf", "provenance": {"source_url": "https://x"}}
        (package / "00_registry" / "handoff_manifest.jsonl").write_text(json.dumps(manifest) + "\n")
        (package / "99_audit" / "review_queue.jsonl").write_text("")
        (package / "99_audit" / "rejected_manifest.jsonl").write_text("")
        return root

    def test_incomplete_pipeline_no_go(self):
        root = self.fixture(); (root / "audit" / "pipeline_state.json").write_text(json.dumps({"stages": {}}))
        self.assertEqual(corpus_quality_gate.evaluate(root)["decision"], "NO_GO")

    def test_reconciliation_error_no_go(self):
        root = self.fixture(); (root / "audit" / "classification_audit.json").write_text(json.dumps({"reconciliation": {"invariant_ok": False}, "coverage": {"parent_aggregation": True}}))
        self.assertEqual(corpus_quality_gate.evaluate(root)["decision"], "NO_GO")

    def test_duplicate_canonical_id_no_go(self):
        root = self.fixture(); path = root / "exports" / "TunnelBookAI_Source_Pack" / "00_registry" / "handoff_manifest.jsonl"
        row = json.loads(path.read_text()); row2 = {**row, "sha256": "def", "local_path": "source2.pdf"}; (path.parent.parent / "source2.pdf").write_bytes(b"pdf2")
        path.write_text(json.dumps(row) + "\n" + json.dumps(row2) + "\n")
        self.assertEqual(corpus_quality_gate.evaluate(root)["decision"], "NO_GO")

    def test_valid_complete_run_go(self):
        self.assertEqual(corpus_quality_gate.evaluate(self.fixture())["decision"], "GO")


if __name__ == "__main__":
    unittest.main()
