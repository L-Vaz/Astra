"""Airfoil section properties, from a committed NeuralFoil screen of the AeroSandbox
library (S1-C).

WHAT THIS REPLACES
------------------
Before S1-C the section was a fiction. `asb_aero._symmetric_naca` snapped every
station to a symmetric NACA 00xx, so no aircraft here had camber or reflex, and
`aero_estimates.CL_MAX_2D = 1.30` was ONE constant for every geometry — cited to
"NACA 4-series at Re ~7e5", which is the wrong section family (conventionally
cambered, i.e. what a TAILED aircraft uses) at roughly 3.5x the Reynolds number
Astra actually flies. The `airfoil` intake row was inert and documented as such.

WHY A SECTION CHOICE IS A REAL DESIGN VARIABLE FOR THIS AIRCRAFT
----------------------------------------------------------------
A cambered aerofoil carries load on its aft portion, which pushes the centre of
pressure back and gives a nose-down pitching moment: Cm0 ~ -0.10. A conventional
aircraft cancels that with a tail. A BWB has no tail, so it must find that moment
somewhere else — from reflex (trailing edge bent up), washout, or sweep. Each of
those unloads the wing where it was carrying lift, so each costs CLmax.

That trade is measurable, and it is why the section cannot be a constant:

    naca4412  conventional camber   Cm0 -0.107   CLmax 1.454   (Re 2e5)
    eh1590    reflexed, flying wing Cm0 -0.019   CLmax 1.038

29% less maximum lift, bought with trimmability. Cm0 is therefore read here as a
TRIM BURDEN: the more negative it is, the more reflex/washout/sweep the airframe has
to spend to trim, and the more CLmax it loses doing so.

RUNTIME CONTRACT
----------------
Pure stdlib + the committed JSON. No aerosandbox, no neuralfoil, no numpy-only-in-
the-solver assumptions: `domain_packs` must stay importable from the OpenVSP py3.10
venv (CLAUDE.md), and NeuralFoil inference per call would make the fuzz suites
unusable. Regenerate with `verification/generate_airfoil_table.py`.
"""
from __future__ import annotations

import hashlib
import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

_DATA_DIR = Path(__file__).with_name("data")
_TABLE_PATH = _DATA_DIR / "airfoils_neuralfoil.json"


class OutOfRange(NamedTuple):
    """A lookup that left the table's domain. Reported, never silently clamped —
    a clamped value with no flag is indistinguishable from physics (cf. the Oswald
    clamp plateau, S6-F3)."""
    quantity: str
    requested: float
    clamped_to: float


class SectionAero(NamedTuple):
    """Section properties at one Reynolds number, interpolated from the table."""
    name: str
    reynolds: float
    tc: float
    clmax: float
    alpha_clmax_deg: float
    cm0: float
    cl0: float
    cl_alpha_per_deg: float
    confidence: float
    stalled: bool = True
    """False => CL was still rising at the top of the screened alpha sweep, so `clmax`
    is a LOWER BOUND set by where the data stopped, not a stall peak. Carried through
    rather than hidden: a truncated CLmax used as a stall peak makes the aircraft look
    slower-stalling and smaller-winged than it is."""
    out_of_range: Tuple[OutOfRange, ...] = ()


@lru_cache(maxsize=1)
def _table() -> Dict[str, Any]:
    """Load and structurally validate the committed screen.

    Validation is not ceremony: this file is generated, ~2 MB, and never hand-edited,
    so a shape error means it was regenerated wrongly and every stall speed
    downstream is quietly wrong."""
    if not _TABLE_PATH.exists():
        raise FileNotFoundError(
            f"{_TABLE_PATH} is missing. Generate it in the FoamAgent env with "
            f"`python verification/generate_airfoil_table.py`.")
    data = json.loads(_TABLE_PATH.read_text(encoding="utf-8"))
    prov, sections = data.get("provenance"), data.get("sections")
    if not isinstance(prov, dict) or not isinstance(sections, dict) or not sections:
        raise ValueError(f"{_TABLE_PATH.name}: expected 'provenance' and 'sections'")
    for key in ("re_grid", "cl_ladder", "neuralfoil", "aerosandbox"):
        if key not in prov:
            raise ValueError(f"{_TABLE_PATH.name}: provenance missing {key!r}")
    if len(sections) != prov.get("sections"):
        raise ValueError(
            f"{_TABLE_PATH.name}: {len(sections)} sections but provenance claims "
            f"{prov.get('sections')}")
    return data


def provenance() -> Dict[str, Any]:
    """Generator, library versions, grids, and the MEASURED interpolation error.

    Surfaced so a card can say where a number came from rather than asserting it."""
    return dict(_table()["provenance"])


@lru_cache(maxsize=1)
def table_sha256() -> str:
    return hashlib.sha256(
        _TABLE_PATH.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def list_sections() -> Tuple[str, ...]:
    return tuple(sorted(_table()["sections"]))


def has_section(name: str) -> bool:
    return name in _table()["sections"]


def cl_ladder() -> Tuple[float, ...]:
    return tuple(_table()["provenance"]["cl_ladder"])


@lru_cache(maxsize=64)
def nearest_by_tc(tc_fraction: float) -> str:
    """The section closest to a bare thickness, for reading PRE-S1-C data.

    Sessions written before the section existed stored `root_thickness_pct=14.0` and
    no aerofoil at all. That aircraft is not reproducible — no section is exactly
    14.00% — so loading it means choosing the nearest one. Ties break on the smallest
    |Cm0| (most trim-neutral, which is what a BWB wants) and then alphabetically, so
    the same file always resolves to the same aircraft.

    This is a MIGRATION path, not a design path: new work names its sections."""
    sections = _table()["sections"]
    best_key = None
    best_name = ""
    for name, rec in sections.items():
        entries = rec["re"]
        mid = entries[len(entries) // 2]
        key = (round(abs(float(rec["tc"]) - tc_fraction), 6),
               round(abs(float(mid["cm0"])), 4), name)
        if best_key is None or key < best_key:
            best_key, best_name = key, name
    return best_name


def tc_of(name: str) -> float:
    """Thickness/chord of a section, as a fraction. Geometry, so no Reynolds needed.

    This is what makes the section the OWNER of thickness (S1-C). `BWBParams` no
    longer takes `root_thickness_pct`/`tip_thickness_pct` as inputs; choosing a
    section fixes the thickness at that station, because thickness is a property of
    the shape and cannot be dialled independently of it. Measured cost: 0.00012 ms
    per lookup after a 68 ms one-time table load, so a hot construction loop can call
    it freely."""
    sections = _table()["sections"]
    if name not in sections:
        raise KeyError(
            f"unknown airfoil {name!r} — not in the committed screen of "
            f"{len(sections)} sections")
    return float(sections[name]["tc"])


def _lerp(x: float, x0: float, x1: float, y0: float, y1: float) -> float:
    if x1 == x0:
        return y0
    t = (x - x0) / (x1 - x0)
    return y0 + t * (y1 - y0)


def _bracket_re(entries: List[Dict[str, Any]], re: float):
    """(lo, hi, out_of_range) bracketing `re`.

    Interpolation is in log(Re) because the underlying boundary-layer behaviour
    scales with Reynolds logarithmically, not linearly — a linear blend between
    1e5 and 1.2e6 would sit almost entirely at the top of the range."""
    # Exact grid hit: use that entry for BOTH ends. Blending a bracket here made
    # `stalled` the AND of two neighbours, so a section that genuinely stalled at
    # 2e5 inherited doubt from the 1e5 entry and reported a mid-sweep alpha with
    # stalled=False — a contradiction the reader was manufacturing, not reading.
    for entry in entries:
        if entry["re"] == re:
            return entry, entry, ()

    lo = entries[0]
    hi = entries[-1]
    flags: Tuple[OutOfRange, ...] = ()
    if re <= lo["re"]:
        if re < lo["re"]:
            flags = (OutOfRange("reynolds", re, lo["re"]),)
        return lo, lo, flags
    if re >= hi["re"]:
        if re > hi["re"]:
            flags = (OutOfRange("reynolds", re, hi["re"]),)
        return hi, hi, flags
    for a, b in zip(entries, entries[1:]):
        if a["re"] <= re <= b["re"]:
            return a, b, flags
    return hi, hi, flags


def section_at(name: str, reynolds: float) -> SectionAero:
    """Section properties at `reynolds`, log-Re interpolated between grid points."""
    sections = _table()["sections"]
    if name not in sections:
        raise KeyError(
            f"unknown airfoil {name!r} — not in the committed screen of "
            f"{len(sections)} sections")
    rec = sections[name]
    entries = rec["re"]
    lo, hi, flags = _bracket_re(entries, reynolds)

    if lo is hi:
        pick = lo
        def blend(key: str) -> float:
            return float(pick[key])
    else:
        lx, hx, x = math.log(lo["re"]), math.log(hi["re"]), math.log(reynolds)
        def blend(key: str) -> float:
            return _lerp(x, lx, hx, float(lo[key]), float(hi[key]))

    return SectionAero(
        name=name, reynolds=float(reynolds), tc=float(rec["tc"]),
        clmax=blend("clmax"), alpha_clmax_deg=blend("alpha_clmax_deg"),
        cm0=blend("cm0"), cl0=blend("cl0"),
        cl_alpha_per_deg=blend("cl_alpha_per_deg"),
        confidence=blend("confidence"),
        # Conservative across a Reynolds bracket: if either bounding entry never
        # stalled, the interpolated CLmax inherits that doubt.
        stalled=bool(lo.get("stalled", True) and hi.get("stalled", True)),
        out_of_range=flags)


def section_cd(name: str, cl: float, reynolds: float) -> Tuple[float, Tuple[OutOfRange, ...]]:
    """Section profile drag at a lift coefficient and Reynolds number.

    Sampled from the stored CL ladder, NOT from a fitted polar. A quadratic
    CD = a + b*CL + c*CL^2 was measured against NeuralFoil at median RMS 20.7-26.0%
    and worst 271% on the attached branch, because a laminar drag bucket at
    Re 1e5-1e6 kinks the polar where transition jumps. The ladder's own
    interpolation error is in `provenance()["cl_ladder_interpolation_error_pct"]`.

    Above the section's CLmax there is no attached-flow drag to report: CD(CL) stops
    being single-valued past stall. That returns the CLmax value with an
    out-of-range flag rather than an extrapolation."""
    sections = _table()["sections"]
    if name not in sections:
        raise KeyError(f"unknown airfoil {name!r}")
    entries = sections[name]["re"]
    lo, hi, flags = _bracket_re(entries, reynolds)
    ladder = _table()["provenance"]["cl_ladder"]

    def cd_on(entry: Dict[str, Any]) -> Tuple[float, Tuple[OutOfRange, ...]]:
        pairs = [(c, d) for c, d in zip(ladder, entry["cd_at_cl"]) if d is not None]
        if not pairs:
            raise ValueError(f"{name}: no drag stored at Re {entry['re']:g}")
        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        local: Tuple[OutOfRange, ...] = ()
        q = cl
        if q < xs[0]:
            local = (OutOfRange("cl", cl, xs[0]),)
            q = xs[0]
        elif q > xs[-1]:
            local = (OutOfRange("cl", cl, xs[-1]),)
            q = xs[-1]
        for a, b, ya, yb in zip(xs, xs[1:], ys, ys[1:]):
            if a <= q <= b:
                return _lerp(q, a, b, ya, yb), local
        return ys[-1], local

    if lo is hi:
        value, local = cd_on(lo)
        return value, flags + local
    v_lo, f_lo = cd_on(lo)
    v_hi, f_hi = cd_on(hi)
    lx, hx, x = math.log(lo["re"]), math.log(hi["re"]), math.log(reynolds)
    return _lerp(x, lx, hx, v_lo, v_hi), flags + f_lo + f_hi


# ---------------------------------------------------------------------------
# suitability — computed, because the library carries no metadata to curate from
# ---------------------------------------------------------------------------
#
# A .dat file in the AeroSandbox database is one name line followed by coordinates.
# There is no application tag, no designer intent, nothing that says what a section
# is FOR. So "suitable for a tailless BWB" has to be derived, and it can be: the
# reflexed/flying-wing sections separate cleanly from conventional cambered ones on
# Cm0 alone. Measured over a 61-section sample of the library at Re 2e5:
#
#     |Cm0| percentiles   p10 0.0000   p25 0.0292   p50 0.0617   p75 0.1030   p90 0.1477
#
# and on named sections: mh60 -0.0240, eh1590 -0.0187, s5010 -0.0141 (all designed
# for tailless use) against clarky -0.0960 and naca4412 -0.1073 (conventional).
#
# ENGINEERING JUDGMENT [D], not a sourced criterion: the cut below sits at roughly
# the 25th percentile of the library. It is a screening aid, not a verdict, and it
# is why `screen()` returns a ranked list with reasons rather than a boolean.
# Falsified if a section inside the band turns out to need more trim authority than
# the airframe has — which only the S1-D trim solve can answer.
TRIM_BURDEN_BAND = 0.03

# Trim-burden differences below this do not rank. Measured basis: four sections all
# designed for the same job — mh60 -0.0035, eh1590 +0.0019, s5010 -0.0010, mh45
# -0.0013 at Re 4e5 — spread across ~0.005, so discriminating at that scale sorts
# noise. Without it, `sorted(|Cm0|)` put SYMMETRIC sections (Cm0 exactly 0.0000) above
# every reflexed one, which is backwards: a reflexed section carries the same trim
# burden AND still makes lift at zero incidence (mh60 cl0 +0.095, s5010 +0.120)
# whereas a symmetric section makes none (naca0012 cl0 0.0000) and must buy its lift
# with geometric incidence. [D] — falsified if NeuralFoil's own CM error turns out to
# be much smaller than 0.005, which would make finer ranking meaningful.
TRIM_RANK_RESOLUTION = 0.005


class SectionScreen(NamedTuple):
    """One section judged against a design point, with the reasons attached."""
    name: str
    aero: SectionAero
    trim_burden: float          # |Cm0| — reflex/washout the airframe must spend
    tailless_suitable: bool
    notes: Tuple[str, ...]


def screen(reynolds: float,
           tc_range: Optional[Tuple[float, float]] = None,
           min_confidence: float = 0.0,
           names: Optional[Tuple[str, ...]] = None) -> Tuple[SectionScreen, ...]:
    """Rank sections for a tailless BWB at one design point.

    Ordered by trim burden (quantised — see TRIM_RANK_RESOLUTION), then CLmax
    descending. Returns EVERY section passing the mechanical filters, suitable or
    not, so the caller can show the trade rather than hide it.

    `min_confidence` defaults to 0.0 — REPORT, DO NOT FILTER. NeuralFoil's
    analysis_confidence measures how well a shape is represented in its training
    set, not how good the aerofoil is, and reflexed geometries are rare. Measured at
    Re 4e5: mh60 0.36, eh1590 0.31, s5010 0.27, mh45 0.35, mh64 0.29 against
    naca0012 0.91. A 0.5 floor therefore removed EVERY flying-wing section from a
    tailless-BWB screen while keeping the symmetric ones — precisely backwards. Low
    confidence is surfaced as a note instead, the same way an out-of-range Reynolds
    is flagged rather than silently clamped."""
    out: List[SectionScreen] = []
    for name in (names if names is not None else list_sections()):
        try:
            aero = section_at(name, reynolds)
        except KeyError:
            continue
        if tc_range is not None and not (tc_range[0] <= aero.tc <= tc_range[1]):
            continue
        if aero.confidence < min_confidence:
            continue
        burden = abs(aero.cm0)
        notes: List[str] = []
        if burden <= TRIM_BURDEN_BAND:
            if abs(aero.cl0) < 0.02:
                notes.append(
                    f"symmetric — trims for free (Cm0 {aero.cm0:+.3f}) but makes no "
                    f"lift at zero incidence, so the wing must buy it with incidence")
            else:
                notes.append(
                    f"reflexed and cambered — near-zero trim burden (Cm0 "
                    f"{aero.cm0:+.3f}) AND lift at zero incidence (cl0 "
                    f"{aero.cl0:+.3f}). This is what a tailless wing wants")
        else:
            notes.append(f"Cm0 {aero.cm0:+.3f} is a conventional cambered section: it "
                         f"needs a tail, or washout and sweep that cost CLmax")
        if aero.confidence < 0.4:
            notes.append(f"NeuralFoil confidence {aero.confidence:.2f} — this shape is "
                         f"sparsely represented in its training set, so treat the "
                         f"numbers as indicative")
        if not aero.stalled:
            notes.append(f"did not stall by {aero.alpha_clmax_deg:.0f}° in the screen — "
                         f"CLmax {aero.clmax:.2f} is a lower bound, not a stall peak")
        if aero.out_of_range:
            notes.append("Reynolds outside the screened grid — value clamped, "
                         "treat as indicative")
        out.append(SectionScreen(name=name, aero=aero, trim_burden=burden,
                                 tailless_suitable=burden <= TRIM_BURDEN_BAND,
                                 notes=tuple(notes)))
    out.sort(key=lambda s: (round(s.trim_burden / TRIM_RANK_RESOLUTION),
                            -s.aero.clmax))
    return tuple(out)
