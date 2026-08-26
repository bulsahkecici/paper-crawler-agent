#!/usr/bin/env python3
from __future__ import annotations

import unittest
from unittest.mock import patch

import gap_discovery


class GapDiscoveryTests(unittest.TestCase):
    def test_gap_uses_handoff_candidate_coverage(self) -> None:
        audit = {
            "section_coverage": {"4.3.5": 100},
            "handoff_candidate_section_coverage": {"4.3.5": 2},
        }
        with patch.object(gap_discovery, "_yaml", return_value={"sections": {"4.3.5": 40}}):
            gaps = gap_discovery.section_gaps(audit)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["section_id"], "4.3.5")
        self.assertEqual(gaps[0]["current"], 2)
        self.assertEqual(gaps[0]["deficit"], 38)

    def test_gap_is_absent_when_usable_target_met(self) -> None:
        audit = {"handoff_candidate_section_coverage": {"5.7.2": 35}}
        with patch.object(gap_discovery, "_yaml", return_value={"sections": {"5.7.2": 35}}):
            self.assertEqual(gap_discovery.section_gaps(audit), [])

    def test_section_queries_prefer_taxonomy_terms(self) -> None:
        fake = {
            "sections": {
                "5.7.2": {
                    "strong_terms": ["tunnel ventilation energy optimization", "road tunnel energy saving"],
                    "medium_terms": ["jet fan energy", "LED tunnel lighting"],
                }
            }
        }
        with patch.object(gap_discovery, "_yaml", return_value=fake):
            queries = gap_discovery._queries_for_section("5.7.2", 3)
        self.assertEqual(len(queries), 3)
        self.assertEqual(queries[0], "tunnel ventilation energy optimization")
        self.assertIn("jet fan energy", queries)


if __name__ == "__main__":
    unittest.main()
