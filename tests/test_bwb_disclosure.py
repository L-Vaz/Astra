"""What the app SAYS, checked against what it DOES (S3-D2, S3-E1..E4, S3-F7, S3-F8).

Stage 3 changes no computed number. Every item is a string, a flag or a conditional —
and that is exactly why it needs tests: a disclosure that is hand-maintained goes stale
the moment the computation moves, and a stale disclosure is worse than none, because it
reads as verified.

So none of the assertions here compare prose to prose. Each one re-measures the
behaviour and asserts the CLAIM matches it:

* the substitution warnings must name the value `to_requirements` actually returns
* the `drives_computation` flags must equal the set of rows that measurably move a
  derived number, re-measured by perturbation on every run
* the driver chips must name only rows that move that specific number
* the bending-index label must match the formula the code evaluates

Pure `domain_packs`: runs on the Windows python and in WSL.
"""
import inspect
import math
import re

import pytest

from domain_packs.bwb import aero_estimates as A
from domain_packs.bwb import intake as I
from domain_packs.bwb.atmosphere import atmosphere_at

# The ten probed display-vs-used divergences, every one of which the app used to make
# silently while still reporting "feasible".
PROBED_SUBSTITUTIONS = [
    ("span_max", "100 m", 3.0),
    ("span_max", "0.1 m", 0.8),
    ("mtow_cap", "500 kg", 25.0),
    ("altitude", "99999 m", 4000.0),
    ("v_stall_max", "0.5 m/s", 5.0),
    ("v_stall_max", "99 m/s", 20.0),
    ("payload", "99 kg", 10.0),
    ("payload", "0 kg", 0.01),
    ("endurance", "900 min", 600.0),
]


def _row(intake: I.Intake, rid: str):
    return next(r for r in intake.rows if r.id == rid)


def _edited(rid: str, value: str) -> I.Intake:
    return I.derive(I.apply_edits(I.default_intake(), [{"id": rid, "value": value}]))


# ===================================================================================
# S3-D2 — every substitution is disclosed, with the typed AND the used value
# ===================================================================================

@pytest.mark.parametrize("rid,typed,used", PROBED_SUBSTITUTIONS,
                         ids=[f"{r}-{t}" for r, t, _ in PROBED_SUBSTITUTIONS])
def test_every_clamp_emits_a_warning(rid, typed, used):
    intake = _edited(rid, typed)
    warnings = [w for w in intake.warnings if w.row_id == rid]
    assert warnings, f"{rid}={typed!r} was replaced with {used} and nothing said so"

    warning = warnings[0]
    assert warning.kind in ("clamped", "rejected")
    assert typed.split()[0] in warning.typed, warning
    assert warning.used, "a warning must state the value actually used"
    assert warning.limit, "a warning must name the bound that forced it"
    assert I.usable_number(intake, rid) == pytest.approx(used)


@pytest.mark.parametrize("rid,typed,used", PROBED_SUBSTITUTIONS,
                         ids=[f"{r}-{t}" for r, t, _ in PROBED_SUBSTITUTIONS])
def test_the_row_shows_the_value_in_force(rid, typed, used):
    """`used_value` is what the screen renders next to the typed text."""
    row = _row(_edited(rid, typed), rid)
    assert row.value == typed, "the typed text must survive"
    assert row.used_value, f"{rid} shows {typed!r} with no indication it is not in use"


def test_a_used_value_differing_from_the_typed_one_always_implies_a_warning():
    """The general contract, over every numeric row and a range of magnitudes: if the
    solver used something other than what was typed, a warning exists. This is the test
    that catches a NEW clamp added without a disclosure."""
    for rid in I.ROW_LIMITS:
        for magnitude in (-500.0, 0.0, 0.05, 1.0, 12.0, 250.0, 99999.0):
            intake = _edited(rid, f"{magnitude:g}")
            typed = _row(intake, rid).number
            used = I.usable_number(intake, rid)
            disclosed = [w for w in intake.warnings if w.row_id == rid]
            if used is None or typed is None or used != pytest.approx(typed):
                assert disclosed, (
                    f"{rid}: typed {typed}, used {used}, nothing disclosed")


def test_no_silent_substitution_reports_plain_feasible():
    """A green "feasible" over a replaced requirement is the dishonest state."""
    clean = I.derive(I.default_intake())
    assert clean.feasibility.verdict in ("feasible", "binding")
    assert clean.feasibility.substitutions == 0

    substituted = _edited("span_max", "100 m")
    assert substituted.feasibility.verdict != "feasible"
    assert substituted.feasibility.substitutions >= 1
    if substituted.feasibility.verdict == "feasible_with_substitutions":
        assert "not with every number you typed" in substituted.feasibility.headline


def test_a_negative_altitude_is_reported_as_a_rejection_not_a_clamp():
    """The probed sign flip: -500 m used to become +500 m. The user did not ask for
    500 m, so calling it a clamp would be a second untruth."""
    intake = _edited("altitude", "-500 m")
    warning = next(w for w in intake.warnings if w.row_id == "altitude")
    assert warning.kind == "rejected"
    assert I.usable_number(intake, "altitude") is None


def test_the_launch_cap_is_disclosed_when_it_is_tighter_than_the_typed_stall():
    """Two substitutions deep: 99 m/s clamps to 20, then flies against a 10.5 m/s
    hand-launch limit. Neither was visible."""
    intake = _edited("v_stall_max", "99 m/s")
    messages = [w.message for w in intake.warnings if w.row_id == "v_stall_max"]
    assert any("hand launch" in m for m in messages), messages
    cap, source = I.effective_stall_cap(intake)
    assert cap == pytest.approx(A.LAUNCH_VSTALL_MAX["hand"])
    assert source == "hand launch"


def test_warnings_about_a_fixed_row_disappear_when_it_is_fixed():
    """Clamp warnings are recomputed, not accumulated — otherwise the screen keeps
    scolding the user about a value they already corrected."""
    intake = _edited("span_max", "100 m")
    assert any(w.row_id == "span_max" for w in intake.warnings)
    corrected = I.derive(I.apply_edits(intake, [{"id": "span_max", "value": "2.0 m"}]))
    assert not [w for w in corrected.warnings if w.kind in ("clamped", "rejected")]


def test_an_ambiguity_warning_survives_a_later_derive():
    """Event warnings record what happened when the brief was parsed; nothing
    recomputes them, so derive() must not drop them."""
    intake = I.derive(I.parse_brief("span 2 m but also span 5 m"))
    assert any(w.kind == "ambiguous" for w in intake.warnings)


# ===================================================================================
# S3-E1 — the lock toggles are display-only, and now say so
# ===================================================================================

def test_lock_status_is_documented_as_display_only():
    description = I.IntakeRow.model_fields["status"].description or ""
    assert "DISPLAY ONLY" in description
    assert "S7-CAP-3" in description, "the capability item must be named"


def test_lock_status_still_changes_nothing():
    """The executable proof behind the label. If locks ever DO constrain the solve,
    this fails and forces the disclaimer to be removed."""
    def signature(status):
        intake = I.default_intake()
        for row in intake.rows:
            if row.status != "derived":
                row.status = status
        return tuple((r.id, r.number) for r in I.derive(intake).rows
                     if r.tier == "derived")

    assert signature("fixed") == signature("variable")


# ===================================================================================
# S3-E2 / E3 — six of twelve editable rows drive no computation
# ===================================================================================

# Perturbations wide enough to make a difference IF the row mattered. Several bases,
# because a single base hides rows whose caps are not binding there: at the default
# intake the span cap is slack and the launch cap is not tighter than the stall row, so
# neither moves anything — and concluding "inert" from that would be wrong.
_BASES = [
    [],
    [{"id": "payload", "value": "0.25 kg"}],
    [{"id": "mtow_cap", "value": "12 kg"}, {"id": "v_stall_max", "value": "18 m/s"}],
    [{"id": "payload", "value": "5 kg"}, {"id": "v_stall_max", "value": "20 m/s"}],
    [{"id": "altitude", "value": "3000 m"}],
]
_PERTURBATIONS = {
    "mission_type": ["Racing / speed", "Cargo / delivery"],
    "payload": ["2.5 kg", "0.3 kg"],
    "altitude": ["1500 m", "0 m"],
    "endurance": ["120 min", "30 min"],
    "launch": ["catapult", "runway"],
    "regulatory": ["< 2 kg VLOS", "open category"],
    "span_max": ["1.6 m", "0.9 m"],
    "v_stall_max": ["8 m/s", "14 m/s"],
    "mtow_cap": ["5 kg", "1.2 kg"],
    "structure": ["carbon fibre", "EPP foam"],
    "airfoil": ["high-lift", "reflexed"],
    "wind": ["20 m/s gusts", "2 m/s gusts"],
}


def _derived_numbers(edits):
    intake = I.derive(I.apply_edits(I.default_intake(), list(edits)))
    return {r.id: r.number for r in intake.rows if r.tier == "derived"}


@pytest.fixture(scope="module")
def measured_dependencies() -> dict:
    """row id -> the set of derived ids it moves, measured over the base grid.

    This fixture is the oracle for E2/E3 and E4 alike: both the flags and the chips are
    asserted against it, so neither can drift from the code."""
    moves = {rid: set() for rid in _PERTURBATIONS}
    for base in _BASES:
        reference = _derived_numbers(base)
        for rid, values in _PERTURBATIONS.items():
            for value in values:
                after = _derived_numbers(base + [{"id": rid, "value": value}])
                for did, before in reference.items():
                    now = after.get(did)
                    if before is None or now is None:
                        continue
                    if abs(before - now) > 1e-9:
                        moves[rid].add(did)
    return moves


def test_only_the_six_computing_rows_are_flagged(measured_dependencies):
    computing = {rid for rid, moved in measured_dependencies.items() if moved}
    assert computing == I.COMPUTING_ROWS, (
        f"the drives_computation flag set {sorted(I.COMPUTING_ROWS)} disagrees with "
        f"what actually moves numbers: {sorted(computing)}")


def test_inert_rows_do_not_change_derived_values(measured_dependencies):
    """The executable proof behind the "recorded only" label."""
    for rid, moved in measured_dependencies.items():
        if rid not in I.COMPUTING_ROWS:
            assert not moved, f"{rid} is labelled recorded-only but moves {moved}"


def test_every_editable_row_carries_the_flag():
    intake = I.derive(I.default_intake())
    for row in intake.rows:
        if row.status == "derived":
            continue
        assert row.drives_computation == (row.id in I.COMPUTING_ROWS), row.id


def test_custom_rows_are_marked_recorded_only():
    intake = I.derive(I.apply_edits(I.default_intake(), [
        {"id": "custom_max_chord", "label": "Max chord", "value": "0.35 m"}]))
    row = _row(intake, "custom_max_chord")
    assert row.drives_computation is False
    assert "not evaluated at this tier" in row.info
    assert "constraint" not in row.info.replace("does not constrain", ""), \
        "the old text promised custom rows become Stage-2 constraints; nothing does"


# ===================================================================================
# S3-E4 — the driver chips name rows that measurably move that number
# ===================================================================================

def test_driver_chips_match_the_measured_dependency_set(measured_dependencies):
    """The perturbation sweep is the oracle, so the chips cannot drift again."""
    for derived_id, drivers in I.MEASURED_DRIVERS.items():
        actual = {rid for rid, moved in measured_dependencies.items()
                  if derived_id in moved}
        assert set(drivers) == actual, (
            f"{derived_id}: chips name {sorted(drivers)} but the measured drivers are "
            f"{sorted(actual)}")


def test_static_margin_has_no_drivers_and_says_it_is_a_fixed_target():
    """It claimed `wind` and `mission_type`. It is the constant 0.08."""
    assert I.MEASURED_DRIVERS["static_margin"] == []
    info = I._INFO["static_margin"]
    assert "FIXED TARGET" in info.upper()
    assert "8% MAC" in info or "8 % MAC" in info


def test_clmax_no_longer_claims_the_airfoil_row_drives_it():
    """cl_max_3d = 1.30 * 0.90 * cos(sweep): the airfoil row moves nothing."""
    assert "airfoil" not in I.MEASURED_DRIVERS["clmax"]
    assert "cos(sweep)" in I._INFO["clmax"]


def test_no_chip_names_a_row_that_does_not_exist():
    ids = {r.id for r in I.default_intake().rows}
    for derived_id, drivers in I.MEASURED_DRIVERS.items():
        assert set(drivers) <= ids, derived_id


# ===================================================================================
# S3-F7 — the bending index label matches the formula
# ===================================================================================

def test_bending_index_label_matches_the_formula():
    """W*b/8 is the UNIFORM-load result. The elliptic value is W*b/(3*pi), 17.8%
    lower, so the old label overstated the loading it claimed to model. The formula is
    a defensible conservative comparative index and every existing candidate rests on
    it: the LABEL was wrong, not the number (see "What NOT to do" in the plan)."""
    params = A.BWBParams(span_m=1.6, root_chord_m=0.55, centre_body_fraction=0.28,
                         mid_chord_frac=0.6, taper_outer=0.4, sweep_inner_deg=35,
                         sweep_outer_deg=18, root_thickness_pct=14,
                         tip_thickness_pct=10, root_twist_deg=1.0,
                         tip_twist_deg=-3.0, dihedral_deg=3.0)
    panel = A.estimate(params, atmosphere_at(0.0), 18.0, 34.335, "hand")

    assert panel.root_bending_index_nm == pytest.approx(34.335 * 1.6 / 8.0, rel=1e-12)

    description = (A.AeroEstimate.model_fields["root_bending_index_nm"].description
                   or "").lower()
    assert "uniform half-span" in description
    # "elliptic" may appear ONLY as the alternative this is not
    assert "not elliptic" in description

    assumptions = " ".join(panel.assumptions).lower()
    assert "uniform half-span" in assumptions
    assert "not elliptic" in assumptions


@pytest.mark.parametrize("text", [
    A.AeroEstimate.model_fields["root_bending_index_nm"].description or "",
    "\n".join(A.estimate(
        A.BWBParams(span_m=1.6, root_chord_m=0.55, centre_body_fraction=0.28,
                    mid_chord_frac=0.6, taper_outer=0.4, sweep_inner_deg=35,
                    sweep_outer_deg=18, root_thickness_pct=14, tip_thickness_pct=10,
                    root_twist_deg=1.0, tip_twist_deg=-3.0, dihedral_deg=3.0),
        atmosphere_at(0.0), 18.0, 34.335, "hand").assumptions),
], ids=["field-description", "assumptions"])
def test_no_user_visible_string_claims_elliptic_loading(text):
    assert "elliptic half-span load" not in text
    assert "(elliptic half-span loading)" not in text


# ===================================================================================
# S3-F8 — the launch limits stop claiming a derivation they do not have
# ===================================================================================

def _launch_limit_comment() -> str:
    source = inspect.getsource(A)
    marker = source.index("LAUNCH_VSTALL_MAX = {")
    return source[max(0, marker - 1400):marker]


def test_launch_limits_are_labelled_engineering_judgment():
    assert "ENGINEERING JUDGMENT" in _launch_limit_comment().upper()


def test_launch_limits_are_not_claimed_to_be_derived():
    """The old comment said they were "calibrated so the pack's own wing-loading table
    sits at the boundary". Inverting them gives three different CLmax values, none
    equal to the pack's own cap — so the comment asserted a derivation that does not
    exist. It may still QUOTE that old claim, as long as it disowns it."""
    comment = _launch_limit_comment()
    assert "NOT derived from defaults.WING_LOADING" in comment
    assert "independent judgments" in comment

    # any surviving mention of "calibrated" must be negated or deferred, never asserted
    for line in comment.splitlines():
        if "calibrated" in line.lower():
            assert any(marker in line.lower() for marker in
                       ("not ", "previous comment", "claimed", "before treating")), line


def test_the_launch_assumption_string_disowns_the_derivation_too():
    """The comment is for developers; this string is what the user reads."""
    params = A.BWBParams(span_m=1.6, root_chord_m=0.55, centre_body_fraction=0.28,
                         mid_chord_frac=0.6, taper_outer=0.4, sweep_inner_deg=35,
                         sweep_outer_deg=18, root_thickness_pct=14,
                         tip_thickness_pct=10, root_twist_deg=1.0,
                         tip_twist_deg=-3.0, dihedral_deg=3.0)
    assumptions = " ".join(
        A.estimate(params, atmosphere_at(0.0), 18.0, 34.335, "hand").assumptions)
    assert "engineering judgment" in assumptions.lower()
    assert "not derived" in assumptions.lower()


def test_the_implied_clmax_values_disagree_with_the_packs_own_cap():
    """A documenting test: keeps the inconsistency measured rather than remembered.

    Inverting each (launch limit, wing loading) pair gives the CLmax that limit
    implies. The three disagree with each other and none equals CL_MAX_2D *
    CL_MAX_3D_FACTOR, which is what "calibrated" would have required."""
    from domain_packs.bwb.defaults import WING_LOADING

    rho = atmosphere_at(0.0).density_kgm3
    implied = {}
    for launch, v_stall in A.LAUNCH_VSTALL_MAX.items():
        wing_loading = WING_LOADING[launch]
        implied[launch] = (2.0 * wing_loading * 9.81) / (rho * v_stall ** 2)

    cap = A.CL_MAX_2D * A.CL_MAX_3D_FACTOR
    assert len(set(round(v, 6) for v in implied.values())) == 3, implied
    for launch, value in implied.items():
        assert abs(value - cap) > 0.05, (
            f"{launch} implies CLmax {value:.4f}, which now matches the pack cap "
            f"{cap:.4f} — the 'calibrated' claim may have become true; re-check F8")
    assert max(implied.values()) - min(implied.values()) > 0.15, implied


def test_every_declared_constant_has_a_source_or_a_judgment_label():
    """S3-F12: no float in the physics core may sit there unexplained. One line of
    LAMINAR_FRACTION moves every L/D in the product by 11%."""
    import inspect
    import re

    source = inspect.getsource(A)
    cited = re.compile(
        r"Raymer|Hoerner|Blasius|Schlichting|Sadraey|Anderson|Prandtl|"
        r"ENGINEERING JUDGMENT", re.IGNORECASE)
    lines = source.splitlines()
    unexplained = []
    for index, line in enumerate(lines):
        match = re.match(r"^([A-Z][A-Z0-9_]+)\s*[:=]", line)
        if not match or match.group(1) in ("STAGES",):
            continue
        context = "\n".join(lines[max(0, index - 8):index + 3])
        if not cited.search(context):
            unexplained.append(match.group(1))
    assert not unexplained, (
        f"constants with neither a citation nor a judgment label: {unexplained}")


def test_static_margin_target_is_declared_once():
    """S3-F12c: it lived in aero_estimates AND as a MissionDerived default, equal today
    and free to drift."""
    from domain_packs.bwb import constants
    from domain_packs.bwb.schema import MissionDerived

    assert A.STATIC_MARGIN_TARGET is constants.STATIC_MARGIN_TARGET
    assert (MissionDerived.model_fields["static_margin_target"].default
            is constants.STATIC_MARGIN_TARGET)


def test_the_assumptions_string_quantifies_its_own_sensitivity():
    """S3-F12b: "assumes a 30% laminar run" means nothing to a reader who cannot tell
    whether that is worth 1% or 11% of L/D."""
    params = A.BWBParams(span_m=1.6, root_chord_m=0.55, centre_body_fraction=0.28,
                         mid_chord_frac=0.6, taper_outer=0.4, sweep_inner_deg=35,
                         sweep_outer_deg=18, root_thickness_pct=14,
                         tip_thickness_pct=10, root_twist_deg=1.0,
                         tip_twist_deg=-3.0, dihedral_deg=3.0)
    assumptions = " ".join(
        A.estimate(params, atmosphere_at(0.0), 18.0, 34.335, "hand").assumptions)
    assert "laminar" in assumptions.lower()
    assert "%" in assumptions
    assert re.search(r"L/D", assumptions), assumptions


def test_laminar_fraction_sensitivity_is_still_what_the_string_claims():
    """The number in the disclosure is measured here, so the claim cannot go stale."""
    params = A.BWBParams(span_m=1.6, root_chord_m=0.55, centre_body_fraction=0.28,
                         mid_chord_frac=0.6, taper_outer=0.4, sweep_inner_deg=35,
                         sweep_outer_deg=18, root_thickness_pct=14,
                         tip_thickness_pct=10, root_twist_deg=1.0,
                         tip_twist_deg=-3.0, dihedral_deg=3.0)
    baseline = A.estimate(params, atmosphere_at(0.0), 18.0, 34.335, "hand")

    original = A.LAMINAR_FRACTION
    try:
        A.LAMINAR_FRACTION = 0.0
        turbulent = A.estimate(params, atmosphere_at(0.0), 18.0, 34.335, "hand")
    finally:
        A.LAMINAR_FRACTION = original

    drop = (baseline.ld_max - turbulent.ld_max) / baseline.ld_max
    # Re-measured after S6-F2: correcting the transition-Reynolds error REDUCED this
    # lever's leverage, because the laminar segment is no longer understated by 1.83x.
    assert 0.03 < drop < 0.06, (
        f"fully turbulent costs {drop:.1%} of L/D max; the disclosure says ~4%")
    assert math.isfinite(drop)
