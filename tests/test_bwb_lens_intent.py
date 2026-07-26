"""Do the concept cards say what their own numbers say? (S3-G1h..G6)

The five cards carry plain-language provenance, and before Stage 3 several sentences
asserted comparisons their own panel contradicted. Measured over the condition grid:

* `easy_launch` printed "Wing loading set to X (below baseline Y)" UNCONDITIONALLY —
  false in 56.0% of conditions, because a 5.5 kg/m^2 floor raises the loading whenever
  the baseline is lighter. Live output: "5.5 kg/m^2 (below baseline 2.6)", directly
  above the card's own "stall +2.0 m/s — harder launch".
* `max_range` asserted "Wing sized so cruise CL = CL*" as fact. It reaches CL* within
  2% in 16.2% of conditions; the median miss is 61%.
* `span_efficiency` printed PRE-repair wing loading, breaking "at the same wing
  loading" in 14.8% of conditions (50.0% at 10 kg), and promised "pay for it in span"
  where span is SMALLER than baseline in 33.8%.
* Four cards can be the same wing: 10.0% of conditions collapse to ONE distinct sizing.
* The hand-launch limit rendered as "the 10 m/s launch limit" — 10.5 through `:.0f`.

The general rule these tests enforce: **every comparison a provenance string asserts is
checked against the numbers on that card**, over a grid rather than one lucky fixture.

Pure `domain_packs`.
"""
import itertools
import re

import pytest

from domain_packs.bwb import variants as V
from domain_packs.bwb.aero_estimates import LAUNCH_VSTALL_MAX, estimate
from domain_packs.bwb.atmosphere import atmosphere_at
from domain_packs.bwb.inverse import solve_requirements
from domain_packs.bwb.requirements import DesignRequirements

# A coarse but genuinely varied grid: the defects above hide in the corners (heavy and
# slow, tight span cap, high altitude), not at the pleasant middle where a single
# fixture lands.
GRID = list(itertools.product(
    (0.8, 2.0, 10.0, 25.0),        # mtow kg
    (10.0, 18.0, 35.0),            # cruise m/s
    (0.0, 4000.0),                 # altitude m
    (1.2, 2.0, 3.0),               # span cap m
    ("hand", "catapult", "runway"),
))


def _batch(mtow, speed, altitude, span_cap, launch):
    requirements = DesignRequirements(
        mtow_kg=mtow, cruise_speed_ms=speed, cruise_altitude_m=altitude,
        launch=launch, span_max_m=span_cap)
    solved = solve_requirements(requirements)
    from domain_packs.bwb.schema import MissionDerived, MissionSpec

    atmosphere = atmosphere_at(altitude)
    derived = MissionDerived(
        mission=MissionSpec(payload_kg=min(max(mtow * 0.3, 0.01), 10.0),
                            cruise_speed_ms=speed, cruise_altitude_m=altitude,
                            mtow_kg=mtow, launch=launch),
        atmosphere=atmosphere, mtow_kg=mtow, air_density_kgm3=atmosphere.density_kgm3,
        weight_n=mtow * 9.81, wing_loading_kgm2=solved.panel.wing_loading_kgm2,
        s_ref_target_m2=solved.panel.s_ref_m2, required_cl=solved.panel.cl_cruise,
        reynolds_mac=solved.panel.reynolds_mac, params=solved.params,
        assumptions=list(solved.notes))
    return V.generate_candidates(derived, requirements)


@pytest.fixture(scope="module")
def batches():
    """One batch per grid point — the oracle every test below reads."""
    return [(point, _batch(*point)) for point in GRID]


def _card(batch, key):
    return next(c for c in batch if c.key == key)


# ===================================================================================
# S3-G1h — no card asserts a comparison its own panel contradicts
# ===================================================================================

def test_easy_launch_provenance_matches_the_numbers(batches):
    """"below the baseline's" must imply the loading IS below the baseline's.

    Only OUTCOME sentences make that claim: a line describing what the lens ASKED for
    says "under the baseline's" instead, because a request that the planform gates
    overrode is not a property of the design."""
    checked = 0
    for point, batch in batches:
        base, card = _card(batch, "baseline"), _card(batch, "easy_launch")
        for line in card.provenance:
            if "below the baseline" in line:
                checked += 1
                assert card.panel.wing_loading_kgm2 < base.panel.wing_loading_kgm2, (
                    f"{point}: card says {line!r} but its wing loading "
                    f"{card.panel.wing_loading_kgm2:.2f} >= baseline "
                    f"{base.panel.wing_loading_kgm2:.2f}")
    assert checked, "no card exercised the 'below baseline' branch — grid too narrow"


def test_easy_launch_admits_when_it_could_not_lower_the_loading(batches):
    """Where this lens still ends up no lighter than the baseline — now only when the
    planform gates move the area, never because of the old 5.5 kg/m^2 floor — the card
    must say so rather than claim a lighter wing (S3-G1h, S6-G1)."""
    for point, batch in batches:
        base, card = _card(batch, "baseline"), _card(batch, "easy_launch")
        if card.panel.wing_loading_kgm2 > base.panel.wing_loading_kgm2 + 1e-9:
            text = " ".join(card.provenance + card.constraint_notes)
            assert "did not end up lighter" in text or "minimum practical" in text, \
                f"{point}: {card.provenance}"


def test_easy_launch_never_stalls_worse_than_baseline(batches):
    """S6-G1: the floor was unconditional, so in 64.1% of conditions it RAISED the
    wing loading. Measured before the fix: stall worse than baseline in 24.0% of
    conditions, and the worst stall of all five in 21.6% — on the card titled
    "Easy launch / Lowest stall speed"."""
    worse = []
    for point, batch in batches:
        base, card = _card(batch, "baseline"), _card(batch, "easy_launch")
        if card.panel.v_stall_ms > base.panel.v_stall_ms + 0.01:
            worse.append((point, round(card.panel.v_stall_ms, 2),
                          round(base.panel.v_stall_ms, 2)))
    assert not worse, f"easy_launch stalls worse than the baseline in {worse}"


def test_easy_launch_is_never_the_highest_stall_of_the_five(batches):
    worst = [point for point, batch in batches
             if _card(batch, "easy_launch").panel.v_stall_ms
             == max(c.panel.v_stall_ms for c in batch)
             and len({round(c.panel.v_stall_ms, 6) for c in batch}) > 1]
    assert not worst, f"the easy-launch card has the worst stall at {worst}"


def test_no_card_claims_below_baseline_while_being_above_it(batches):
    """The general form, over every card and every line."""
    for point, batch in batches:
        base = _card(batch, "baseline")
        for card in batch:
            for line in card.provenance:
                if "below the baseline" in line and "wing loading" in line.lower():
                    assert card.panel.wing_loading_kgm2 < base.panel.wing_loading_kgm2, \
                        f"{point} {card.key}: {line}"


# ===================================================================================
# S3-G2h — max_range's CL* claim is qualified, and the retraction is mandatory
# ===================================================================================

def test_max_range_retraction_is_present_whenever_cl_star_is_missed(batches):
    missed = 0
    for point, batch in batches:
        card = _card(batch, "max_range")
        miss = abs(card.panel.cl_cruise - card.panel.cl_star) / card.panel.cl_star
        if miss > V.CL_STAR_TOLERANCE:
            missed += 1
            notes = " ".join(card.constraint_notes)
            assert "away from CL*" in notes, (
                f"{point}: cruise CL {card.panel.cl_cruise:.3f} misses CL* "
                f"{card.panel.cl_star:.3f} by {miss:.0%} with no retraction: {notes}")
            assert "does not cruise at its best-L/D point" in notes, point
    assert missed, "CL* was always reached on this grid — grid too narrow"


def test_max_range_provenance_reports_achieved_cl(batches):
    for point, batch in batches:
        card = _card(batch, "max_range")
        miss = abs(card.panel.cl_cruise - card.panel.cl_star) / card.panel.cl_star
        text = " ".join(card.provenance)
        if miss > V.CL_STAR_TOLERANCE:
            assert "achieved CL" in text, f"{point}: {text}"
            assert f"{card.panel.cl_cruise:.3f}" in text, point
        else:
            assert "cruise CL = CL*" in text, f"{point}: {text}"


def test_the_objective_string_is_qualified():
    lens = next(l for l in V.LENSES if l.key == "max_range")
    assert "as the stall and cruise-CL limits allow" in lens.objective


# ===================================================================================
# S3-G3h / G4h — provenance quotes post-repair numbers only
# ===================================================================================

# A card may legitimately quote the BASELINE's loading when it is comparing itself to
# it ("started from the baseline's 7.5; the design sits at 7.1"). These patterns pick
# out the numbers a card asserts about ITSELF.
_OWN_AREA = re.compile(r"S = (\d+\.\d+) m\^2")
_OWN_LOADING = re.compile(
    r"(?:sits at|\(wing loading|^Wing loading) (\d+\.\d+) kg/m\^2")


def test_provenance_numbers_match_the_final_panel(batches):
    """Every area and wing loading a card asserts about itself must be its own.

    The failure this catches: the number was computed before `_feasible_planform`
    repaired the planform, so the sentence described a wing that was never built."""
    for point, batch in batches:
        for card in batch:
            if card.is_baseline:
                continue
            for line in card.provenance:
                for match in _OWN_AREA.finditer(line):
                    assert float(match.group(1)) == pytest.approx(
                        card.panel.s_ref_m2, abs=5e-4), f"{point} {card.key}: {line}"
                for match in _OWN_LOADING.finditer(line):
                    assert float(match.group(1)) == pytest.approx(
                        card.panel.wing_loading_kgm2, abs=0.05), \
                        f"{point} {card.key}: {line}"


def test_a_card_that_quotes_the_baseline_says_so(batches):
    """Guard the guard: the exclusion above must not become a loophole. A wing-loading
    number that is NOT this card's own may appear only in a sentence that names the
    baseline."""
    for point, batch in batches:
        base = _card(batch, "baseline")
        for card in batch:
            if card.is_baseline:
                continue
            for line in card.provenance:
                for match in re.finditer(r"(\d+\.\d+) kg/m\^2", line):
                    value = float(match.group(1))
                    own = value == pytest.approx(card.panel.wing_loading_kgm2,
                                                 abs=0.05)
                    quoted = value == pytest.approx(base.panel.wing_loading_kgm2,
                                                    abs=0.05)
                    # a REQUESTED loading may appear only in a sentence that says it
                    # was a request, never as a statement of what the design is
                    requested = any(marker in line for marker in
                                    ("Asked for", "minimum practical value"))
                    assert own or requested \
                        or (quoted and "baseline" in line.lower()), \
                        f"{point} {card.key}: unattributed number in {line!r}"


def test_span_efficiency_span_claim(batches):
    """"pay for it in span" is false in 33.8% of conditions; where span did not grow,
    the card must say so."""
    smaller = 0
    for point, batch in batches:
        base, card = _card(batch, "baseline"), _card(batch, "span_efficiency")
        if card.panel.span_m <= base.panel.span_m + 1e-9:
            smaller += 1
            assert any("Span did not grow" in n for n in card.constraint_notes), \
                f"{point}: span {card.panel.span_m:.2f} vs base {base.panel.span_m:.2f}"
    assert smaller, "span always grew on this grid — grid too narrow"


def test_control_margin_concedes_stall():
    lens = next(l for l in V.LENSES if l.key == "control_margin")
    assert "stall" in lens.objective.lower()


def test_control_margin_really_does_have_the_highest_stall_often(batches):
    """The conceded cost is measured, not asserted: if this lens stopped being the
    stall-worst, the objective string would need rewording."""
    worst = 0
    for _, batch in batches:
        stalls = {c.key: c.panel.v_stall_ms for c in batch}
        if stalls["control_margin"] == max(stalls.values()):
            worst += 1
    assert worst / len(batches) > 0.3, (
        f"control_margin is stall-worst in only {worst / len(batches):.0%} of "
        f"conditions; its objective string claims 65.9%")


# ===================================================================================
# S3-G5 — degeneracy is reported
# ===================================================================================

def test_degeneracy_is_reported_when_sizings_collapse(batches):
    collapsed = 0
    for point, batch in batches:
        distinct = {c.distinct_sizings for c in batch}
        assert len(distinct) == 1, f"{point}: candidates disagree on distinct_sizings"
        count = distinct.pop()
        duplicates = [c for c in batch if c.duplicate_of]
        assert count == len(batch) - len(duplicates)
        if duplicates:
            collapsed += 1
            for card in duplicates:
                twin = _card(batch, card.duplicate_of)
                assert card.panel.span_m == pytest.approx(twin.panel.span_m, abs=1e-4)
                assert any("Same wing as" in n for n in card.constraint_notes), point
    assert collapsed, "no collapse on this grid — grid too narrow"


def test_gallery_never_shows_two_identical_sizings_as_two_choices(batches):
    """S6-G5a: report the collapse (S3-G5) AND stop offering the duplicates as
    choices. Making them genuinely distinct is deferred design work."""
    for point, batch in batches:
        shown = V.distinct_candidates(batch)
        sizings = [(round(c.panel.span_m, 4), round(c.panel.s_ref_m2, 4))
                   for c in shown]
        assert len(sizings) == len(set(sizings)), f"{point}: duplicates offered {sizings}"
        assert shown, "every batch must still offer at least one design"


def test_ranking_puts_infeasible_designs_last(batches):
    """S6-F1: `ld_cruise` is arithmetic for a point below stall, and ranking on it let
    those points compete with real designs as equals."""
    for point, batch in batches:
        ranked = V.rank_candidates(batch)
        flags = [bool(c.panel.cruise_feasible and not c.violations) for c in ranked]
        assert flags == sorted(flags, reverse=True), point


@pytest.mark.xfail(strict=True,
                   reason="S6-G2: max_range's fixed AR 6.5 loses to span_efficiency's "
                          "AR 8.5 in 39.1% of conditions, so the lens NAMED for range "
                          "is not the one with the best cruise L/D. Fixing it means "
                          "letting max_range optimise AR as well as area — a bounded "
                          "experiment, deliberately not attempted here. Stage 3 "
                          "qualified the claim instead.")
def test_max_range_has_the_best_cruise_ld_among_the_lenses(batches):
    for point, batch in batches:
        lenses = [c for c in batch if not c.is_baseline and c.panel.cruise_feasible]
        if len(lenses) < 2:
            continue
        best = max(lenses, key=lambda c: c.panel.ld_cruise)
        assert best.key == "max_range", (point, best.key)


@pytest.mark.xfail(strict=True,
                   reason="S6-G3: easy_launch has lower induced drag than "
                          "span_efficiency in 57.3% of conditions, so the lens named "
                          "for span efficiency does not own it. Same bounded "
                          "experiment as S6-G2.")
def test_span_efficiency_has_the_lowest_induced_drag(batches):
    import math as _math

    for point, batch in batches:
        lenses = [c for c in batch if not c.is_baseline]
        def k(c):
            return 1.0 / (_math.pi * c.panel.oswald_e * c.panel.aspect_ratio)
        best = min(lenses, key=k)
        assert best.key == "span_efficiency", (point, best.key)


def test_dihedral_is_the_same_in_every_concept(batches):
    """S7-CAP-2, documented rather than fixed: every _Lens inherits the 2.0 degree
    default, so dihedral is identical across all five cards and is not a dimension the
    gallery explores. Recorded here so the backlog item is measured — if a lens ever
    starts varying it, this fails and the capability note is out of date."""
    for point, batch in batches:
        dihedrals = {round(c.params.dihedral_deg, 6) for c in batch
                     if not c.is_baseline}
        assert dihedrals == {2.0}, (point, dihedrals)


def test_the_probed_collapse_condition_reports_four_cards_as_one_wing():
    """The concrete case from the probe: mtow 0.8, alt 4000, v 10, span cap 1.2."""
    batch = _batch(0.8, 10.0, 4000.0, 1.2, "hand")
    assert batch[0].distinct_sizings < len(batch)
    duplicates = [c.key for c in batch if c.duplicate_of]
    assert duplicates, [c.panel.s_ref_m2 for c in batch]


def test_full_parameter_distance_cannot_detect_the_collapse():
    """A documenting test for why the cluster is on SIZING, not on the parameter
    vector: sweep, thickness and twist always differ between lenses, so a
    12-parameter distance never collapses even when the wing is identical."""
    batch = _batch(0.8, 10.0, 4000.0, 1.2, "hand")
    duplicates = [c for c in batch if c.duplicate_of]
    assert duplicates, "premise: this condition collapses"
    card = duplicates[0]
    twin = _card(batch, card.duplicate_of)
    distance = max(abs(getattr(card.params, f) - getattr(twin.params, f))
                   for f in type(card.params).model_fields)
    assert distance > 0.15, (
        f"parameter distance {distance:.3f} — if this ever drops, the parameter "
        f"vector could detect degeneracy after all and the note above is stale")


# ===================================================================================
# S3-G6 — the tradeoff sentences
# ===================================================================================

@pytest.mark.parametrize("launch", ["hand", "catapult", "runway"])
def test_launch_limit_renders_one_decimal(launch):
    """10.5 through `:.0f` printed "the 10 m/s launch limit" next to a "stall 10.50
    m/s" sentence on the same card. Catapult and runway are integers, so a test using
    only those would have missed it — hence the parametrization."""
    limit = LAUNCH_VSTALL_MAX[launch]
    base = _panel(span_m=1.6, v=18.0)
    breaching = _panel(span_m=0.9, v=18.0, launch=launch, mass=12.0)
    if breaching.launch_ok is not False:
        pytest.skip(f"{launch}: this fixture does not breach the limit")
    sentences = " ".join(V.tradeoff_sentences(breaching, base))
    assert f"{limit:.1f} m/s launch limit" in sentences, sentences


def _panel(span_m=1.6, v=18.0, launch="hand", mass=3.5):
    from domain_packs.bwb.schema import BWBParams

    params = BWBParams(span_m=span_m, root_chord_m=0.55, centre_body_fraction=0.28,
                       mid_chord_frac=0.6, taper_outer=0.4, sweep_inner_deg=35,
                       sweep_outer_deg=18, root_thickness_pct=14, tip_thickness_pct=10,
                       root_twist_deg=1.0, tip_twist_deg=-3.0, dihedral_deg=3.0)
    return estimate(params, atmosphere_at(0.0), v, mass * 9.81, launch)


@pytest.mark.parametrize("sign", [1, -1])
def test_threshold_symmetry_neutral_point(sign):
    """A -10 mm shift was dropped while +10 mm was reported: the comparison was
    float-exact and asymmetric."""
    base = _panel()
    moved = base.model_copy(update={"x_np_m": base.x_np_m + sign * 0.01})
    sentences = " ".join(V.tradeoff_sentences(moved, base))
    assert "neutral point" in sentences, (sign, sentences)


@pytest.mark.parametrize("sign", [1, -1])
def test_threshold_symmetry_span(sign):
    base = _panel()
    span = base.span_m + sign * 0.10
    moved = base.model_copy(update={
        "span_m": span,
        "root_bending_index_nm": base.weight_n * span / 8.0})
    sentences = " ".join(V.tradeoff_sentences(moved, base))
    assert "span" in sentences, (sign, sentences)


def test_tip_re_sentence_is_comparative():
    """Absolute firing made the "within a few percent" fallback unreachable for every
    low-tip-Re design, which is nearly every small UAV."""
    base = _panel()
    low = base.model_copy(update={"tip_re": 1.0e5})
    both_low = base.model_copy(update={"tip_re": 1.0e5})
    base_low = base.model_copy(update={"tip_re": 0.9e5})

    assert any("tip Reynolds" in s for s in V.tradeoff_sentences(
        low, base.model_copy(update={"tip_re": 3.0e5})))
    assert not any("tip Reynolds" in s
                   for s in V.tradeoff_sentences(both_low, base_low)), \
        "fired even though the baseline is just as bad"


def test_no_baseline_only_fallback_when_a_metric_moved():
    base = _panel()
    identical = base.model_copy()
    assert V.tradeoff_sentences(identical, base) == [
        "within a few percent of the baseline on every tracked metric"]


def test_the_fallback_is_reachable_for_a_low_tip_reynolds_design():
    """It was not: the absolute tip-Re sentence always fired first."""
    base = _panel().model_copy(update={"tip_re": 1.0e5})
    identical = base.model_copy()
    assert V.tradeoff_sentences(identical, base) == [
        "within a few percent of the baseline on every tracked metric"]


def test_tip_re_note_does_not_print_a_value_equal_to_its_own_threshold():
    """`:.2g` rounded 1.49e5 up to 1.5e5 and printed "1.5e+05 below 1.5e+05"."""
    panel = _panel().model_copy(update={"tip_re": 1.49e5})
    note = next(n for n in V.absolute_notes(panel) if "Tip Reynolds" in n)
    printed = re.search(r"Tip Reynolds ([\d.e+]+) is below ([\d.e+]+)", note)
    assert printed, note
    assert float(printed.group(1)) < float(printed.group(2)), note


def test_bending_and_span_are_one_sentence():
    """They were two, saying the same thing: bending %-change IS span %-change,
    exactly, because weight is identical across candidates."""
    base = _panel()
    span = base.span_m * 1.2
    moved = base.model_copy(update={
        "span_m": span, "root_bending_index_nm": base.weight_n * span / 8.0})
    sentences = V.tradeoff_sentences(moved, base)
    mentions = [s for s in sentences if "root bending" in s or "span " in s]
    assert len(mentions) == 1, mentions
    assert "span" in mentions[0] and "root bending" in mentions[0]


def test_baseline_launch_breach_is_flagged(batches):
    """The baseline's tradeoffs are a fixed placeholder, so a baseline that exceeds its
    own launch limit was flagged nowhere at all."""
    flagged = 0
    for point, batch in batches:
        base = _card(batch, "baseline")
        if base.panel.launch_ok is False:
            flagged += 1
            assert any("exceeds the" in n for n in base.constraint_notes), \
                f"{point}: baseline stall {base.panel.v_stall_ms:.1f} unflagged"
    assert flagged, "no baseline breached its launch limit on this grid"
