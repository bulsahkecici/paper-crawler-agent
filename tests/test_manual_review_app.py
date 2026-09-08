import json
import tempfile
import unittest
from pathlib import Path

import manual_review_app
from manual_acquisition import AcquisitionStore
from pypdf import PdfWriter


class ManualReviewStoreTests(unittest.TestCase):
    def fixture(self):
        root = Path(tempfile.mkdtemp())
        queue = root / "exports" / "TunnelBookAI_Source_Pack" / "99_audit"
        queue.mkdir(parents=True)
        row = {
            "document_key": "10.1/test", "doi": "10.1/test", "title": "Road tunnel ventilation",
            "classification_status": "NEEDS_REVIEW", "classification_confidence": .6,
            "relevance_status": "WEAK", "document_type": "UNKNOWN",
        }
        (root / "classification_index.jsonl").write_text(json.dumps(row) + "\n")
        (queue / "reclassify_queue.jsonl").write_text(json.dumps({"document_id":"10.1/test","title":row["title"]}) + "\n")
        return root

    def test_decision_is_durable_and_manual_accept_is_auditable(self):
        root = self.fixture(); store = manual_review_app.ReviewStore(root)
        saved = store.save({"document_id":"10.1/test","decision":"ACCEPT","relevance_status":"STRONG","document_type":"TECHNICAL_REPORT","notes":"content checked"})
        self.assertEqual(saved["decision"], "ACCEPT")
        report = store.apply(); self.assertEqual(report["applied"], 1)
        row = json.loads((root / "classification_index.jsonl").read_text().strip())
        self.assertEqual(row["classification_status"], "MANUAL_ACCEPTED")
        self.assertEqual(row["manual_relevance_status"], "STRONG")
        self.assertEqual(row["manual_review"]["notes"], "content checked")
        self.assertTrue(Path(report["backup"]).is_file())
        store.close()

    def test_invalid_decision_is_rejected(self):
        store = manual_review_app.ReviewStore(self.fixture())
        with self.assertRaises(ValueError):
            store.save({"document_id":"10.1/test","decision":"DELETE"})
        store.close()

    def test_downloaded_pdf_can_be_matched_and_imported_safely(self):
        root = self.fixture()
        retry = root / "exports" / "TunnelBookAI_Source_Pack" / "99_audit" / "retry_acquisition.jsonl"
        retry.write_text(json.dumps({"document_id":"10.1/test","title":"Road tunnel ventilation"}) + "\n")
        downloads = root / "downloads"; downloads.mkdir()
        source = downloads / "Road tunnel ventilation.pdf"
        writer = PdfWriter(); writer.add_blank_page(width=200, height=200)
        writer.add_metadata({"/Title":"Road tunnel ventilation"})
        with source.open("wb") as handle: writer.write(handle)
        store = AcquisitionStore(root, downloads)
        scan = store.suggestions()
        self.assertEqual(scan["suggestions"]["10.1/test"]["file"], source.name)
        result = store.import_pdf("10.1/test", source.name)
        self.assertEqual(result["status"], "IMPORTED")
        self.assertTrue(Path(result["source_path"]).is_file())
        self.assertTrue(Path(result["backup"]).is_file())
        row = json.loads((root / "classification_index.jsonl").read_text().strip())
        self.assertEqual(row["acquisition_status"], "DOWNLOADED_PDF")
        self.assertEqual(row["source_sha256"], result["sha256"])
        self.assertTrue(source.is_file())

    def test_short_generic_title_does_not_match_longer_unrelated_filename(self):
        score, _ = AcquisitionStore._match(
            {"title":"Tunnel excavation", "doi":None},
            {"name":"Optimal selection of a metro tunnel excavation method using AHP and TOPSIS.pdf", "pdf_title":"", "text_sample":""},
        )
        self.assertLess(score, 0.72)

    def test_manual_acquisition_exclusion_is_reversible(self):
        root = self.fixture()
        retry = root / "exports" / "TunnelBookAI_Source_Pack" / "99_audit" / "retry_acquisition.jsonl"
        retry.write_text(json.dumps({"document_id":"10.1/test","title":"Road tunnel ventilation","doi":"10.1/test"}) + "\n")
        store = AcquisitionStore(root, root / "downloads")
        store.set_excluded("10.1/test", True)
        self.assertIn("MANUAL_EXCLUDED", store.records()[0]["manual_download_exclusion_reasons"])
        store.set_excluded("10.1/test", False)
        self.assertNotIn("MANUAL_EXCLUDED", store.records()[0]["manual_download_exclusion_reasons"])


if __name__ == "__main__": unittest.main()
