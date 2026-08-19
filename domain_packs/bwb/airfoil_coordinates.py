"""Exact airfoil coordinates paired with the committed NeuralFoil polar table.

The reader is deliberately standard-library only. The OpenVSP Python 3.10 process
uses it to write selected section files, while the FoamAgent Python 3.12 process uses
the same hashes in recommendations and handoff evidence.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import gzip
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Mapping

from . import airfoils


_ARCHIVE_PATH = Path(__file__).with_name("data") / "airfoil_coordinates.json.gz"


def _canonical_coordinates(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _section_hash(coordinates: Any) -> str:
    return hashlib.sha256(_canonical_coordinates(coordinates)).hexdigest()


def write_archive_payload(payload: Mapping[str, Any], path: Path) -> Path:
    """Write a byte-deterministic gzip archive (fixed timestamp and no filename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True,
                     ensure_ascii=True).encode("ascii")
    with path.open("wb") as stream:
        with gzip.GzipFile(filename="", mode="wb", fileobj=stream, mtime=0) as zipped:
            zipped.write(raw)
    return path

def load_archive(path: Path = _ARCHIVE_PATH) -> Dict[str, Any]:
    """Load and validate provenance, polar parity, and every coordinate hash."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} is missing; generate it with verification/generate_airfoil_table.py "
            "--coordinates-only"
        )
    with gzip.open(path, "rt", encoding="ascii") as stream:
        payload = json.load(stream)
    provenance = payload.get("provenance")
    sections = payload.get("sections")
    if not isinstance(provenance, dict) or not isinstance(sections, dict) or not sections:
        raise ValueError(f"{path.name}: expected provenance and non-empty sections")
    if provenance.get("sections") != len(sections):
        raise ValueError(
            f"{path.name}: {len(sections)} sections but provenance claims "
            f"{provenance.get('sections')}"
        )
    for name, record in sections.items():
        coordinates = record.get("coordinates") if isinstance(record, dict) else None
        if not isinstance(coordinates, list) or len(coordinates) < 3:
            raise ValueError(f"{name}: coordinates are missing or malformed")
        if _section_hash(coordinates) != record.get("sha256"):
            raise ValueError(f"{name}: coordinate hash does not match archive record")

    coordinate_names = tuple(sorted(sections))
    polar_names = airfoils.list_sections()
    if coordinate_names != polar_names:
        missing = sorted(set(polar_names) - set(coordinate_names))[:5]
        extra = sorted(set(coordinate_names) - set(polar_names))[:5]
        raise ValueError(
            f"coordinate/polar section drift (missing={missing}, extra={extra})"
        )
    if provenance.get("aerosandbox") != airfoils.provenance().get("aerosandbox"):
        raise ValueError(
            "coordinate/polar AeroSandbox version drift: "
            f"{provenance.get('aerosandbox')} != "
            f"{airfoils.provenance().get('aerosandbox')}"
        )
    return payload


@lru_cache(maxsize=1)
def _archive() -> Dict[str, Any]:
    return load_archive(_ARCHIVE_PATH)


def archive_payload() -> Dict[str, Any]:
    """Return a defensive copy suitable for audit or mutation tests."""
    return deepcopy(_archive())


def provenance() -> Dict[str, Any]:
    return deepcopy(_archive()["provenance"])


def archive_sha256() -> str:
    return hashlib.sha256(_ARCHIVE_PATH.read_bytes()).hexdigest()


def list_sections() -> tuple[str, ...]:
    return tuple(sorted(_archive()["sections"]))


def section(name: str) -> Dict[str, Any]:
    try:
        return deepcopy(_archive()["sections"][name])
    except KeyError:
        raise KeyError(f"unknown coordinate section {name!r}") from None


def coordinate_sha256(name: str) -> str:
    return str(section(name)["sha256"])


def reference(name: str) -> Dict[str, Any]:
    if name not in _archive()["sections"]:
        raise KeyError(f"unknown coordinate section {name!r}")
    generic = {
        "coordinates": _archive()["provenance"]["source_url"],
        "experimental": None,
        "evidence_grade": "D",
    }
    generic.update(deepcopy(_archive().get("references", {}).get(name, {})))
    return generic


def write_dat(name: str, path: Path) -> Path:
    """Write an OpenVSP/Selig-format coordinate file for exactly one section."""
    record = section(name)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [name]
    lines.extend(f"{float(x):.8f} {float(y):.8f}" for x, y in record["coordinates"])
    path.write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")
    return path
