#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import handoff_export
import hybrid_classifier as hybrid
import tunnel_harvest as harvest


class FakeEmbeddingClient:
    base_url = "http://127.0.0.1:1234/v1"

    def embedding(self, model: str, text: str) -> list[float]:
        lowered = text.lower()
        if "life cycle" in lowered or "lifecycle" in lowered or "yaşam döngüsü" in lowered:
            return [1.0, 0.0, 0.0]
        if "maintenance" in lowered or "bakım" in lowered:
            return [0.7, 0.3, 0.0]
        return [0.0, 1.0, 0.0]


class FakeLlmClient:
    base_url = "http://127.0.0.1:1234/v1"

    def chat_json(self, model: str, system: str, user: str) -> dict:
        payload = json.loads(user)
        ids = [row["id"] for row in payload["candidate_sections"]]
        selected = "4.3.5" if "4.3.5" in ids else ids[0]
        return {"sections": [{"id": selected, "confidence": 0.95}], "reason": "Best allowed section."}


class CountingLlmClient(FakeLlmClient):
    def __init__(self) -> None:
        self.calls = 0

    def chat_json(self, model: str, system: str, user: str) -> dict:
        self.calls += 1
        return super().chat_json(model, system, user)


class HybridClassificationTests(unittest.TestCase):
    def test_non_loopback_model_server_rejected(self) -> None:
        self.assertFalse(hybrid.is_loopback_url("https://api.example.com/v1"))
        with self.assertRaises(ValueError):
            hybrid.LocalOpenAIClient("https://api.example.com/v1")

    def test_embedding_fusion_keeps_lcc_section(self) -> None:
        record = {
            "title": "Life cycle cost analysis of road tunnels",
            "abstract": "Construction, operation and maintenance costs over the tunnel life cycle.",
            "source": "crossref",
        }
        result = hybrid.classify_hybrid(
            record,
            embedding_client=FakeEmbeddingClient(),
            embedding_model="text-embedding-nomic-embed-text-v1.5",
        )
        self.assertEqual(result["primary_section"], "4.3.5")
        self.assertTrue(result["embedding_review"]["sections"])
        self.assertIn("section_fusion", result["methods"])

    def test_qwen_can_only_select_candidate_section(self) -> None:
        record = {"title": "Ambiguous tunnel life cycle maintenance study", "source": "crossref"}
        result = hybrid.classify_hybrid(
            record,
            embedding_client=FakeEmbeddingClient(),
            embedding_model="text-embedding-nomic-embed-text-v1.5",
            llm_client=FakeLlmClient(),
            llm_model="qwen3.6-35b-a3b-mlx",
        )
        valid_ids = {row["id"] for row in result["book_sections"]}
        self.assertTrue(valid_ids)
        self.assertIn(result["primary_section"], valid_ids)
        if result["llm_review"].get("used"):
            self.assertEqual(result["classification_status"], "LLM_ACCEPTED")

    def test_strong_rule_embedding_agreement_skips_qwen(self) -> None:
        llm = CountingLlmClient()
        result = hybrid.classify_hybrid(
            {
                "title": "Life cycle cost analysis of road tunnels",
                "abstract": "Construction, operation and maintenance costs over the tunnel life cycle.",
                "source": "crossref",
                "relevance_status": "STRONG",
            },
            embedding_client=FakeEmbeddingClient(),
            embedding_model="text-embedding-nomic-embed-text-v1.5",
            llm_client=llm,
            llm_model="qwen3.6-35b-a3b-mlx",
        )
        self.assertFalse(result["llm_review"]["used"])
        self.assertEqual(llm.calls, 0)


class HandoffTests(unittest.TestCase):
    def test_export_only_accepts_gated_source_and_preserves_sha(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="paper_crawler_handoff_"))
        harvest.set_output_dir(root)
        pdf = harvest.PDF_DIR / "paper.pdf"
        content = b"%PDF-1.4\nTunnel source\n%%EOF\n"
        pdf.write_bytes(content)
        sha = hashlib.sha256(content).hexdigest()
        accepted = {
            "title": "Road tunnel life cycle cost",
            "abstract": "Road tunnel construction, operation and maintenance life cycle cost.",
            "source_path": str(pdf),
            "source_sha256": sha,
            "document_type": "JOURNAL_ARTICLE",
            "source_class": "ACADEMIC",
            "authority_tier": "B2",
            "evidence_priority": 84,
            "primary_section": "4.3.5",
            "book_sections": [{"id": "4.3.5", "score": 0.95}],
            "topics": ["life_cycle_cost"],
            "classification_confidence": 0.95,
            "classification_status": "AUTO_ACCEPT",
            "route_path": "C_ACADEMIC/ARTICLES",
        }
        rejected = {
            **accepted,
            "title": "Unreviewed paper",
            "classification_status": "NEEDS_REVIEW",
        }
        with (root / "classification_index.jsonl").open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(accepted) + "\n")
            handle.write(json.dumps(rejected) + "\n")

        report = handoff_export.export_handoff(root)
        self.assertEqual(report["ready_for_handoff"], 1)
        self.assertEqual(report["rejected"], 0)
        self.assertEqual(report["decision_counts"]["MANUAL_REVIEW"], 1)
        package = Path(report["package_root"])
        manifest_rows = [json.loads(line) for line in (package / "00_registry" / "manifest.jsonl").read_text().splitlines()]
        self.assertEqual(len(manifest_rows), 1)
        exported = package / manifest_rows[0]["source_path"]
        self.assertTrue(exported.exists())
        self.assertEqual(hashlib.sha256(exported.read_bytes()).hexdigest(), sha)
        self.assertEqual(manifest_rows[0]["paper_crawler_status"], "READY_FOR_HANDOFF")
        self.assertEqual(manifest_rows[0]["tunnelbookai_status"], "NOT_INGESTED")

    def test_sha_mismatch_is_rejected(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="paper_crawler_sha_"))
        harvest.set_output_dir(root)
        pdf = harvest.PDF_DIR / "bad.pdf"
        pdf.write_bytes(b"%PDF-bad")
        row = {
            "title": "Bad checksum",
            "source_path": str(pdf),
            "source_sha256": "0" * 64,
            "primary_section": "2.2",
            "classification_status": "AUTO_ACCEPT",
            "route_path": "C_ACADEMIC/ARTICLES",
        }
        (root / "classification_index.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
        report = handoff_export.export_handoff(root)
        self.assertEqual(report["ready_for_handoff"], 0)
        self.assertEqual(report["rejections"][0]["reason"], "sha256_mismatch")


if __name__ == "__main__":
    unittest.main()
