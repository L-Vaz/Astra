from __future__ import annotations

import json
import unittest
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
PORTFOLIO = ROOT / "portfolio"
MANIFEST = PORTFOLIO / "source-manifest.json"

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
