#!/usr/bin/env python3
import unittest
import gap_discovery

class TopicDiscoveryTests(unittest.TestCase):
    def test_chapter_gaps_are_deprecated(self): self.assertEqual(gap_discovery.section_gaps({"section_coverage":{"5.5":2}}), [])
    def test_chapter_queries_are_not_generated(self): self.assertEqual(gap_discovery._queries_for_section("5.7.2",3), [])
    def test_queries_keep_tunnel_anchor(self): self.assertEqual(gap_discovery.ensure_domain_anchor("maintenance cost"), "road tunnel maintenance cost")
if __name__ == "__main__": unittest.main()
