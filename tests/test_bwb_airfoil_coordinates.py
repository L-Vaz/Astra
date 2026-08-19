"""Exact airfoil-coordinate archive contract.

The polar table and geometry archive describe one section identity.  These tests are
pure stdlib so the same reader is usable by the FoamAgent and OpenVSP interpreters.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from domain_packs.bwb import airfoils


def _coordinates_module():
    try:
        from domain_packs.bwb import airfoil_coordinates
    except ImportError as exc:  # RED should be a focused failure, not collection abort.
        pytest.fail(f"coordinate archive reader is missing: {exc}")
    return airfoil_coordinates


def test_coordinate_archive_has_exact_polar_name_and_version_parity():
    coords = _coordinates_module()

    assert coords.list_sections() == airfoils.list_sections()
    assert coords.provenance()["aerosandbox"] == airfoils.provenance()["aerosandbox"]
    assert coords.provenance()["sections"] == len(airfoils.list_sections())


@pytest.mark.parametrize("name", ["e472", "e474", "s5010"])
def test_fixture_coordinate_hashes_verify_and_write_standard_dat(name, tmp_path):
    coords = _coordinates_module()

    section = coords.section(name)
    canonical = json.dumps(
        section["coordinates"], separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    assert hashlib.sha256(canonical).hexdigest() == section["sha256"]

    dat = tmp_path / f"{name}.dat"
    written = coords.write_dat(name, dat)
    assert written == dat
    lines = dat.read_text(encoding="ascii").splitlines()
    assert lines[0] == name
    assert len(lines) == len(section["coordinates"]) + 1


def test_reference_ledger_distinguishes_missing_experiment_from_missing_geometry():
    coords = _coordinates_module()

    assert coords.reference("e472")["experimental"]
    assert coords.reference("s5010")["experimental"]
    assert coords.reference("e474")["experimental"] is None
    assert coords.reference("e474")["coordinates"]


def test_coordinate_reader_rejects_a_tampered_section_hash(tmp_path):
    coords = _coordinates_module()
    raw = coords.archive_payload()
    raw["sections"]["e474"]["coordinates"][0][1] += 0.001
    altered = tmp_path / "tampered.json.gz"
    coords.write_archive_payload(raw, altered)

    with pytest.raises(ValueError, match="e474.*hash"):
        coords.load_archive(altered)

