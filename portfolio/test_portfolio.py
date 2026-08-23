from __future__ import annotations

import json
import unittest
import zipfile
from pathlib import Path

from PIL import Image
from pypdf import PdfReader


ROOT = Path(__file__).resolve().parents[1]
PORTFOLIO = ROOT / "portfolio"
MANIFEST = PORTFOLIO / "source-manifest.json"
CONTENT = PORTFOLIO / "content.json"
CLAIMS = PORTFOLIO / "claim-ledger.json"
PPTX = PORTFOLIO / "astra-mechanical-engineering-portfolio.pptx"
PDF = PORTFOLIO / "astra-mechanical-engineering-portfolio.pdf"

REQUIRED_SOURCE_IDS = {
    "geometry_iso",
    "geometry_top",
    "mesh_surface",
    "mesh_slice_y",
    "mesh_slice_z",
    "surface_pressure",
    "velocity_slice",
    "residual_history",
    "force_history",
    "cockpit_overview",
    "cockpit_analysis",
}


class PortfolioContractTests(unittest.TestCase):
    def test_source_manifest_is_complete_and_portable(self) -> None:
        self.assertTrue(MANIFEST.is_file(), "source-manifest.json is missing")
        payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
        entries = payload["assets"]
        by_id = {entry["id"]: entry for entry in entries}

        self.assertEqual(len(by_id), len(entries), "source IDs must be unique")
        self.assertTrue(REQUIRED_SOURCE_IDS <= set(by_id))

        for entry in entries:
            source = (ROOT / entry["source"]).resolve()
            output = (ROOT / entry["output"]).resolve()
            self.assertTrue(source.is_relative_to(ROOT), entry["source"])
            self.assertTrue(output.is_relative_to(PORTFOLIO / "assets"), entry["output"])
            self.assertTrue(source.is_file(), f"missing source: {source}")
            self.assertTrue(output.is_file(), f"missing output: {output}")
            self.assertIn(entry["evidence_grade"], {"A", "B", "C", "D"})
            self.assertTrue(entry["case_label"].strip())
            self.assertTrue(entry["caption"].strip())
            with Image.open(output) as image:
                image.verify()

            if entry["kind"] == "ui_capture":
                self.assertEqual(entry["session_id"], "44d2e8b0")
                self.assertEqual(entry["viewport"], "1280x720")
                self.assertTrue(entry["capture_url"].startswith("http://127.0.0.1:8765/"))
                self.assertTrue(entry["captured_at"].endswith("Z"))

    def test_content_and_claims_are_complete(self) -> None:
        self.assertTrue(CONTENT.is_file(), "content.json is missing")
        self.assertTrue(CLAIMS.is_file(), "claim-ledger.json is missing")

        content = json.loads(CONTENT.read_text(encoding="utf-8"))
        claims = json.loads(CLAIMS.read_text(encoding="utf-8"))["claims"]
        sources = json.loads(MANIFEST.read_text(encoding="utf-8"))["assets"]
        claims_by_id = {claim["id"]: claim for claim in claims}
        sources_by_id = {source["id"]: source for source in sources}

        self.assertEqual(content["author"]["name"], "Lindon Vaz")
        self.assertEqual(
            content["author"]["linkedin"],
            "https://www.linkedin.com/in/lindon-vaz-",
        )
        self.assertEqual(content["author"]["github"], "https://github.com/L-Vaz/Astra")
        self.assertNotIn("email", content["author"])
        self.assertEqual(len(content["pages"]), 10)
        self.assertEqual([page["number"] for page in content["pages"]], list(range(1, 11)))

        serialized = json.dumps(content, ensure_ascii=False)
        for token in ("YOUR NAME", "EMAIL", "LINKEDIN", "PROJECT LINK", "lorem"):
            self.assertNotIn(token, serialized)
        for dash in ("\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2015"):
            self.assertNotIn(dash, serialized, "use ASCII hyphens in audience-facing copy")

        for page in content["pages"]:
            self.assertTrue(page["title"].strip())
            self.assertTrue(page["message"].strip())
            self.assertTrue(page["sources"])
            for claim_id in page.get("claim_ids", []):
                self.assertIn(claim_id, claims_by_id, f"page {page['number']}: {claim_id}")
            for asset_id in page.get("asset_ids", []):
                self.assertIn(asset_id, sources_by_id, f"page {page['number']}: {asset_id}")

        self.assertEqual(len(claims_by_id), len(claims), "claim IDs must be unique")
        for claim in claims:
            self.assertIn(claim["grade"], {"A", "B", "C", "D"})
            self.assertTrue(claim["source"].strip())
            self.assertTrue(claim["use_on_pages"])

    def test_powerpoint_is_editable_and_complete(self) -> None:
        self.assertTrue(PPTX.is_file(), "PowerPoint deliverable is missing")
        with zipfile.ZipFile(PPTX) as archive:
            names = archive.namelist()
            self.assertIn("ppt/presentation.xml", names)
            slides = [
                name
                for name in names
                if name.startswith("ppt/slides/slide")
                and name.endswith(".xml")
                and "/_rels/" not in name
            ]
            self.assertEqual(len(slides), 10)
            self.assertTrue(any(name.startswith("ppt/media/") for name in names))
            notes = [
                name
                for name in names
                if name.startswith("ppt/notesSlides/notesSlide")
                and name.endswith(".xml")
                and "/_rels/" not in name
            ]
            self.assertEqual(len(notes), 10)
            for note in notes:
                self.assertIn("[Sources]", archive.read(note).decode("utf-8", errors="ignore"))
            slide_text = " ".join(
                archive.read(name).decode("utf-8", errors="ignore") for name in slides
            )
            for expected in ("Lindon Vaz", "ASTRA", "Foam-Agent"):
                self.assertIn(expected, slide_text)

    def test_pdf_is_application_ready(self) -> None:
        self.assertTrue(PDF.is_file(), "PDF deliverable is missing")
        reader = PdfReader(PDF)
        self.assertEqual(len(reader.pages), 10)
        for page in reader.pages:
            self.assertGreater(float(page.mediabox.width), float(page.mediabox.height))
        extracted = " ".join(page.extract_text() or "" for page in reader.pages)
        for expected in (
            "Lindon Vaz",
            "ASTRA",
            "Foam-Agent",
            "linkedin.com/in/lindon-vaz-",
            "github.com/L-Vaz/Astra",
        ):
            self.assertIn(expected, extracted)
        for placeholder in ("your@email", "example.com", "TODO", "PLACEHOLDER"):
            self.assertNotIn(placeholder, extracted)
        link_targets = {
            annotation.get_object().get("/A", {}).get("/URI")
            for page in reader.pages
            for annotation in (page.get("/Annots") or [])
            if annotation.get_object().get("/Subtype") == "/Link"
        }
        self.assertIn("https://www.linkedin.com/in/lindon-vaz-", link_targets)
        self.assertIn("https://github.com/L-Vaz/Astra", link_targets)


if __name__ == "__main__":
    unittest.main(verbosity=2)
