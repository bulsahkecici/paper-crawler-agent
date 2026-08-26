#!/usr/bin/env python3

from __future__ import annotations

import unittest

import classification_engine as classifier


class ClassificationTests(unittest.TestCase):
    def test_academic_cost_paper_routes_to_articles_and_lcc(self) -> None:
        record = {
            "title": "Life cycle cost analysis of road tunnels",
            "abstract": "This study evaluates construction cost, operation cost and maintenance cost for highway tunnels.",
            "source": "crossref",
            "doi": "10.1234/example",
            "landing_url": "https://doi.org/10.1234/example",
        }
        result = classifier.classify_record(record)
        self.assertEqual(result.document_type, "JOURNAL_ARTICLE")
        self.assertEqual(result.source_class, "ACADEMIC")
        self.assertEqual(result.authority_tier, "B2")
        self.assertEqual(result.route_path, "C_ACADEMIC/ARTICLES")
        self.assertIn("life_cycle_cost", result.topics)
        self.assertIn("4.3.5", [row["id"] for row in result.book_sections])

    def test_kgm_standard_gets_official_priority_and_route(self) -> None:
        record = {
            "title": "Karayolları Teknik Şartnamesi - Tünel İşleri",
            "source_url": "https://www.kgm.gov.tr/SiteCollectionDocuments/KGMdocuments/example.pdf",
            "publisher": "Karayolları Genel Müdürlüğü",
        }
        result = classifier.classify_record(record)
        self.assertEqual(result.document_type, "TECHNICAL_STANDARD")
        self.assertEqual(result.source_class, "TR_OFFICIAL")
        self.assertEqual(result.publisher_code, "KGM")
        self.assertEqual(result.authority_tier, "A1")
        self.assertEqual(result.evidence_priority, 100)
        self.assertEqual(result.route_path, "A_OFFICIAL/TR/KGM/TECHNICAL_STANDARDS")

    def test_kgm_news_is_news_but_official(self) -> None:
        record = {
            "title": "Zigana Tüneli'nde bakım çalışmaları tamamlandı",
            "source_url": "https://www.kgm.gov.tr/haber/example",
            "publisher": "KGM",
        }
        result = classifier.classify_record(record)
        self.assertEqual(result.document_type, "NEWS")
        self.assertEqual(result.source_class, "TR_OFFICIAL")
        self.assertEqual(result.authority_tier, "A1")
        self.assertEqual(result.route_path, "A_OFFICIAL/TR/KGM/NEWS")

    def test_arxiv_is_preprint(self) -> None:
        record = {
            "title": "Deep learning for tunnel lining defect detection",
            "abstract": "Automated inspection and structural health monitoring of road tunnels.",
            "source": "arxiv",
            "landing_url": "https://arxiv.org/abs/1234.5678",
        }
        result = classifier.classify_record(record)
        self.assertEqual(result.document_type, "PREPRINT")
        self.assertEqual(result.source_class, "ACADEMIC")
        self.assertEqual(result.authority_tier, "D1")
        self.assertEqual(result.route_path, "C_ACADEMIC/PREPRINTS")

    def test_thesis_metadata_beats_generic_rules(self) -> None:
        record = {
            "title": "Karayolu tünellerinde geoteknik inceleme ve NATM uygulaması",
            "degree": "Doktora",
            "landing_url": "https://tez.yok.gov.tr/example",
        }
        result = classifier.classify_record(record)
        self.assertEqual(result.document_type, "THESIS_PHD")
        self.assertEqual(result.authority_tier, "C1")
        self.assertEqual(result.route_path, "C_ACADEMIC/THESES/PHD")
        self.assertIn("geotechnics", result.topics)
        self.assertIn("NATM", result.topics)

    def test_unknown_document_is_not_auto_accepted(self) -> None:
        record = {"title": "A document with no useful tunnel metadata"}
        result = classifier.classify_record(record)
        self.assertEqual(result.document_type, "UNKNOWN")
        self.assertEqual(result.classification_status, "NEEDS_REVIEW")
        self.assertEqual(result.route_path, "90_STAGING/NEEDS_CLASSIFICATION")

    def test_energy_paper_maps_to_energy_section(self) -> None:
        record = {
            "title": "Tunnel ventilation energy optimization using jet fan control",
            "abstract": "Energy efficiency and electricity cost reduction in road tunnel ventilation systems.",
            "source": "openalex",
        }
        result = classifier.classify_record(record)
        ids = [row["id"] for row in result.book_sections]
        self.assertIn("5.7.2", ids)
        self.assertIn("energy", result.topics)
        self.assertIn("ventilation", result.topics)


if __name__ == "__main__":
    unittest.main()
