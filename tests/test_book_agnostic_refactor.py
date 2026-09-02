#!/usr/bin/env python3
from __future__ import annotations
import unittest
import classification_engine as classifier
import decision_router
import presentation_sources
import relevance_engine

class RelevanceRegressionTests(unittest.TestCase):
    def test_accept_examples(self):
        values=["NATM tunnel support using shotcrete","TBM excavation performance in road tunnel","road tunnel maintenance","road tunnel ventilation","tunnel lining inspection","road tunnel fire safety","immersed tube tunnel","tunnel construction cost","tunnel life cycle cost","metro tunnel deformation","shield tunnel settlement","road tunnel lighting energy"]
        for title in values:
            with self.subTest(title=title): self.assertIn(relevance_engine.evaluate({"title":title})["relevance_status"],{"STRONG","PROBABLE"})
    def test_reject_examples(self):
        values=["quantum tunneling","tunnel junction transistor","tunnel FET","carpal tunnel","cubital tunnel","VPN tunnel","SSH tunnel","animal tunnels","agricultural high tunnels","generic wind tunnel"]
        for title in values:
            with self.subTest(title=title): self.assertEqual(relevance_engine.evaluate({"title":title})["relevance_status"],"IRRELEVANT")

class InstitutionTests(unittest.TestCase):
    def check(self,url,source_class,code):
        result=classifier.classify_record({"title":"Road tunnel technical manual","source_url":url,"relevance_status":"STRONG","tunnel_relevance_score":.95})
        self.assertEqual(result.source_class,source_class); self.assertEqual(result.publisher_code,code)
    def test_registry(self):
        for url,kind,code in [("https://www.kgm.gov.tr/x","TR_OFFICIAL","KGM"),("https://www.fhwa.dot.gov/x","ROAD_AUTHORITY","FHWA"),("https://www.vegvesen.no/x","ROAD_AUTHORITY","STATENS_VEGVESEN"),("https://www.rijkswaterstaat.nl/x","ROAD_AUTHORITY","RIJKSWATERSTAAT"),("https://www.asfinag.at/x","ROAD_AUTHORITY","ASFINAG"),("https://austroads.com.au/x","PROFESSIONAL_ORGANIZATION","AUSTROADS"),("https://www.tii.ie/x","TRANSPORT_AUTHORITY","TII"),("https://www.piarc.org/x","PROFESSIONAL_ORGANIZATION","PIARC")]:
            with self.subTest(code=code): self.check(url,kind,code)

class PresentationTests(unittest.TestCase):
    def test_slideshare_kgm_uses_kgm_producer(self):
        x=presentation_sources.resolve_presentation({"title":"Road tunnel maintenance slides","source_url":"https://slideshare.net/x","actual_producer":"KGM","organization":"KGM"})
        self.assertEqual(x["producer"]["name"],"KGM"); self.assertNotEqual(x["producer"]["name"],"SLIDESHARE")
    def test_anonymous_platform_is_not_official(self):
        x=presentation_sources.resolve_presentation({"title":"Road tunnel slides","source_url":"https://slideserve.com/x"})
        self.assertEqual(x["source_class"],"PRESENTATION_PLATFORM"); self.assertEqual(x["authority_tier"],"G")
    def test_piarc_and_zenodo_attribution(self):
        piarc=presentation_sources.resolve_presentation({"title":"Road tunnel conference presentation","source_url":"https://piarc.org/x","organization":"PIARC","conference":"PIARC Congress"})
        self.assertEqual(piarc["document_type"],"CONFERENCE_PRESENTATION"); self.assertEqual(piarc["source_class"],"PROFESSIONAL_ORGANIZATION")
        zen=presentation_sources.resolve_presentation({"title":"Tunnel conference slides","source_url":"https://zenodo.org/records/1","organization":"Example University","university":True})
        self.assertEqual(zen["source_class"],"RESEARCH_REPOSITORY"); self.assertEqual(zen["document_type"],"ACADEMIC_PRESENTATION")

class ArchitectureTests(unittest.TestCase):
    def test_chapter_fields_are_legacy_only(self):
        migrated=classifier.migrate_legacy_fields({"title":"x","primary_section":"5.5","book_sections":["5.5"]})
        self.assertNotIn("primary_section",migrated); self.assertEqual(migrated["legacy"]["primary_section"],"5.5")
    def test_section_ambiguity_does_not_reclassify(self):
        row={"title":"Road tunnel maintenance","relevance_status":"STRONG","classification_status":"AUTO_ACCEPT","classification_confidence":.9,"document_type":"TECHNICAL_REPORT","source_class":"ACADEMIC","authority_tier":"D3","primary_section":None}
        self.assertEqual(decision_router.route(row,source_exists=True)["decision"],"AUTO_HANDOFF")

if __name__=="__main__": unittest.main()
