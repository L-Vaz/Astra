"""S1-C — the committed NeuralFoil section screen.

These tests pin the CONTRACT of the table and its reader, not the numbers themselves.
Section values move whenever the table is regenerated with a different NeuralFoil
version or grid; what must not move is that the reader interpolates in log-Reynolds,
flags rather than silently clamps an out-of-range lookup, refuses to extrapolate drag
past stall, and separates reflexed sections from conventional ones.

The physics assertions below are bands, not point values, and every band is anchored
to a measurement recorded in the test that uses it.
"""
import math

import pytest

from domain_packs.bwb import airfoils as AF

pytestmark = pytest.mark.skipif(
    not AF._TABLE_PATH.exists(),
    reason="airfoil table not generated — run verification/generate_airfoil_table.py "
           "in the FoamAgent env")


# ===================================================================================
# table integrity
# ===================================================================================

def test_table_loads_and_declares_its_provenance():
    prov = AF.provenance()
    for key in ("generator", "aerosandbox", "neuralfoil", "model_size",
                "re_grid", "cl_ladder", "sections"):
        assert key in prov, key
    assert prov["sections"] == len(AF.list_sections())
    assert len(AF.list_sections()) > 1500, "the library screen looks truncated"


def test_provenance_publishes_its_measured_interpolation_error():
    """The table stores CD on a CL ladder instead of fitting a polar, because a
    quadratic was measured at median RMS 20.7-26.0%. A table that replaces a fit on
    accuracy grounds must publish its own error, or the claim is unfalsifiable."""
    err = AF.provenance()["cl_ladder_interpolation_error_pct"]
    assert err["measured_on"] >= 10, "too few samples to characterise the ladder"
    assert err["median"] is not None and err["median"] < 20.0, err
    assert err["worst"] is not None


def test_reynolds_grid_brackets_the_mission_envelope():
    """Measured across 35 generated candidates spanning payload 0.1-4 kg, 8-60 m/s,
    0-4000 m: Re(MAC) ran 1.15e5 to 1.09e6. The grid must contain that, or every
    lookup in normal use is a clamped extrapolation."""
    grid = AF.provenance()["re_grid"]
    assert min(grid) <= 1.15e5, grid
    assert max(grid) >= 1.09e6, grid


def test_clmax_is_a_stall_peak_not_a_post_stall_artifact():
    """The bug this pins, found during S1-C and fixed before the table shipped.

    `argmax(CL)` over a full alpha sweep returns the GLOBAL maximum. For a thin
    section at Re 1e5-1e6, CL collapses at stall and then climbs again as the
    separated wake reorganises — measured on naca64a010 at Re 4e5, CL recovered to
    0.831 at alpha 19.5 having peaked at 0.823 and stalled to 0.689. So the global
    max sat in the post-stall regime, and every derived quantity inherited it:
    stored CLmax was up to 11.6% too high (s1010 0.9217 -> 0.8151), and because
    CD(CL) is multi-valued across a stall the drag ladder was wrong by up to 862%.

    A CLmax reported at the very top of the swept alpha range means the section
    never stalled inside the sweep, so the value is a lower bound rather than a
    stall peak. That is allowed — some high-camber sections genuinely do not stall
    below 30 deg — but it must be DECLARED.

    Only ONE direction is a lie worth asserting: `stalled=True` at the sweep
    boundary, i.e. a truncation passed off as a stall peak. The converse does NOT
    hold and must not be asserted — a section with no entry at the requested
    Reynolds gets an interpolated `alpha_clmax_deg` that lands mid-sweep while
    `stalled` is the conservative AND of the bracketing entries. goe570 at Re 2e5
    is exactly that case, and its False is correct, not a misreport."""
    lo_a, hi_a, _step = AF.provenance()["alpha_sweep_deg"]
    lying = []
    for name in AF.list_sections():
        for re in AF.provenance()["re_grid"]:
            s = AF.section_at(name, re)
            at_edge = s.alpha_clmax_deg >= hi_a - 1e-9 or s.alpha_clmax_deg <= lo_a + 1e-9
            if at_edge and s.stalled:
                lying.append((name, re, s.alpha_clmax_deg))
    assert not lying, (
        f"{len(lying)} entries report a stall peak at the edge of the swept range "
        f"[{lo_a}, {hi_a}] — a truncation passed off as CLmax: {lying[:5]}")


def test_sections_that_never_stall_are_a_small_declared_minority():
    """If a large fraction never stalls, the sweep is too short and CLmax is
    systematically a lower bound — which would quietly undersize every wing."""
    prov = AF.provenance()
    never = prov.get("entries_that_never_stalled")
    assert never is not None, "provenance must declare how many entries never stalled"
    total = prov["sections"] * len(prov["re_grid"])
    assert never / total < 0.02, (
        f"{never}/{total} entries never stalled inside the sweep — extend "
        f"ALPHA_SWEEP rather than shipping CLmax lower bounds as stall peaks")


def test_stall_incidence_is_physically_plausible():
    """A section that stalls at 2 degrees or at 19 has not been read correctly.
    Band is deliberately wide — this catches gross misreads, not modelling error."""
    bad = []
    for name in AF.list_sections():
        s = AF.section_at(name, 4e5)
        if not (2.0 <= s.alpha_clmax_deg <= 19.0):
            bad.append((name, s.alpha_clmax_deg))
    # A handful of unusual sections may legitimately sit outside; a large fraction
    # means the branch detection is broken again.
    assert len(bad) < 0.05 * len(AF.list_sections()), (
        f"{len(bad)}/{len(AF.list_sections())} sections stall outside 2-19 deg: "
        f"{bad[:8]}")


# ===================================================================================
# the reader's contract
# ===================================================================================

def test_unknown_section_raises_rather_than_guessing():
    with pytest.raises(KeyError, match="not-an-airfoil"):
        AF.section_at("not-an-airfoil", 4e5)


def test_reynolds_below_and_above_the_grid_is_flagged_not_silently_clamped():
    """A clamped value with no flag is indistinguishable from physics — the exact
    failure the Oswald clamp plateau produced (S6-F3)."""
    grid = AF.provenance()["re_grid"]
    name = AF.list_sections()[0]

    low = AF.section_at(name, min(grid) / 10.0)
    assert low.out_of_range, "under-range Reynolds returned no flag"
    assert low.out_of_range[0].quantity == "reynolds"
    assert low.out_of_range[0].clamped_to == pytest.approx(min(grid))

    high = AF.section_at(name, max(grid) * 10.0)
    assert high.out_of_range and high.out_of_range[0].clamped_to == pytest.approx(max(grid))

    inside = AF.section_at(name, math.sqrt(min(grid) * max(grid)))
    assert not inside.out_of_range


def test_interpolation_is_in_log_reynolds():
    """Between 1e5 and 1.2e6 a LINEAR blend sits almost entirely at the top of the
    range, so the midpoint of the two must land near the geometric mean, not the
    arithmetic one. Verified by checking the interpolated value at the geometric
    midpoint is the average of the two endpoints."""
    grid = AF.provenance()["re_grid"]
    lo, hi = grid[0], grid[1]
    name = AF.list_sections()[0]
    a = AF.section_at(name, lo).clmax
    b = AF.section_at(name, hi).clmax
    mid = AF.section_at(name, math.sqrt(lo * hi)).clmax
    assert mid == pytest.approx((a + b) / 2.0, abs=1e-6), (a, mid, b)


def test_drag_is_not_extrapolated_past_stall(any_section_with_low_clmax):
    """CD(CL) is not single-valued past CLmax: the same CL occurs pre- and post-stall
    with wildly different drag. Asking above the ladder must flag, not extrapolate."""
    name, aero = any_section_with_low_clmax
    cd, flags = AF.section_cd(name, aero.clmax + 2.0, aero.reynolds)
    assert flags, "drag above CLmax returned no out-of-range flag"
    assert any(f.quantity == "cl" for f in flags)
    assert cd > 0


def test_drag_rises_away_from_the_low_drag_point():
    """A section's polar has a minimum; drag at a high lift coefficient must exceed
    drag near it. This is the weakest true statement about a drag bucket — the shape
    between those points is exactly what is NOT quadratic."""
    name = "naca0012"
    if not AF.has_section(name):
        pytest.skip("naca0012 absent from the screen")
    lo, _ = AF.section_cd(name, 0.15, 4e5)
    hi, _ = AF.section_cd(name, 0.75, 4e5)
    assert hi > lo, (lo, hi)


# ===================================================================================
# S1-C physics — the section is a real variable
# ===================================================================================

REFLEXED = ("mh60", "eh1590", "s5010")
CONVENTIONAL = ("naca4412", "clarky")


def test_reflexed_and_conventional_sections_separate_on_trim_burden():
    """The screen has no use-case metadata to curate from — a .dat file is a name
    line and coordinates. Suitability is derived from Cm0, which is only defensible
    if it actually separates the two families.

    Measured directly through NeuralFoil at Re 2e5 before this table existed:
    mh60 -0.0240, eh1590 -0.0187, s5010 -0.0141 against clarky -0.0960 and
    naca4412 -0.1073 — a 4-5x gap."""
    present_r = [n for n in REFLEXED if AF.has_section(n)]
    present_c = [n for n in CONVENTIONAL if AF.has_section(n)]
    if not present_r or not present_c:
        pytest.skip("named reference sections absent from the screen")

    worst_reflexed = max(abs(AF.section_at(n, 2e5).cm0) for n in present_r)
    best_conventional = min(abs(AF.section_at(n, 2e5).cm0) for n in present_c)
    assert worst_reflexed < best_conventional, (
        f"reflexed {worst_reflexed:.4f} vs conventional {best_conventional:.4f} — "
        f"Cm0 no longer separates the families, so the suitability screen is invalid")


def test_a_symmetric_section_has_no_pitching_moment():
    """First principles, and a check that the table is not scrambled: a symmetric
    aerofoil at zero incidence produces no lift and no moment about the quarter
    chord. If naca0012 shows camber-like Cm0, the screen mapped names to the wrong
    coordinates."""
    if not AF.has_section("naca0012"):
        pytest.skip("naca0012 absent from the screen")
    s = AF.section_at("naca0012", 4e5)
    assert abs(s.cm0) < 0.02, s.cm0
    assert abs(s.cl0) < 0.05, s.cl0


def test_screen_ranks_by_trim_burden_and_explains_itself():
    """Ranking is on QUANTISED trim burden, not raw |Cm0|. Sorting on the raw value
    ranked symmetric sections (Cm0 exactly 0.0000) above every reflexed one, which is
    backwards — see TRIM_RANK_RESOLUTION. Within a bucket the tie-break is CLmax."""
    ranked = AF.screen(2e5, tc_range=(0.08, 0.16))
    assert ranked, "nothing survived the screen"
    buckets = [round(s.trim_burden / AF.TRIM_RANK_RESOLUTION) for s in ranked]
    assert buckets == sorted(buckets), "screen() must rank by quantised trim burden"
    for a, b in zip(ranked, ranked[1:]):
        if round(a.trim_burden / AF.TRIM_RANK_RESOLUTION) == \
           round(b.trim_burden / AF.TRIM_RANK_RESOLUTION):
            assert a.aero.clmax >= b.aero.clmax - 1e-9, (
                f"within a trim bucket, {a.name} must outrank {b.name} on CLmax")
    assert all(s.notes for s in ranked), "every screened section must carry a reason"
    assert any(s.tailless_suitable for s in ranked)
    assert any(not s.tailless_suitable for s in ranked), (
        "screen() must return unsuitable sections too, so the trade is visible")


def test_reflexed_sections_outrank_symmetric_ones_at_equal_trim_burden():
    """The flaw the quantised ranking exists to fix. A reflexed section carries the
    same near-zero trim burden as a symmetric one AND still makes lift at zero
    incidence (measured Re 4e5: mh60 cl0 +0.095, s5010 +0.120, against naca0012
    +0.000). Ranking must not put the section that makes no lift on top."""
    ranked = AF.screen(4e5, tc_range=(0.08, 0.14))
    top = ranked[:25]
    cambered = [s for s in top if abs(s.aero.cl0) >= 0.02]
    assert cambered, (
        "the top of the screen is all symmetric sections — the ranking has "
        "regressed to raw |Cm0|")
    assert len(cambered) > len(top) / 2, (
        f"only {len(cambered)}/{len(top)} of the top sections make lift at zero "
        f"incidence")


def test_confidence_does_not_filter_out_the_flying_wing_sections():
    """A confidence floor of 0.5 removed mh60 (0.36), eh1590 (0.31), s5010 (0.27),
    mh45 (0.35) and mh64 (0.29) while keeping naca0012 (0.91) — it selected against
    exactly the sections a tailless BWB wants, because NeuralFoil confidence tracks
    how common a shape is in its training set, not how good it is."""
    names = [n for n in ("mh60", "eh1590", "s5010") if AF.has_section(n)]
    if not names:
        pytest.skip("flying-wing reference sections absent")
    screened = {s.name for s in AF.screen(4e5, tc_range=(0.08, 0.14))}
    missing = [n for n in names if n not in screened]
    assert not missing, f"default screen excluded flying-wing sections: {missing}"


def test_screen_respects_the_thickness_window():
    ranked = AF.screen(2e5, tc_range=(0.10, 0.12))
    assert ranked
    assert all(0.10 <= s.aero.tc <= 0.12 for s in ranked)


@pytest.fixture
def any_section_with_low_clmax():
    """A real section and its properties, for the past-stall drag check."""
    name = AF.list_sections()[0]
    return name, AF.section_at(name, 4e5)
