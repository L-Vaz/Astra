"""Stage-1 intake — parser, tiers, edits, and feasibility."""
import pytest

from domain_packs.bwb import intake as I

BRIEF = ("I'm building a drone for supervising a farm. It should fly at around "
         "100 m, span under 2 m, carry my 180 g camera, and stay up for at "
         "least 45 minutes. Hand launched.")


def _row(ik: I.Intake, rid: str) -> I.IntakeRow:
    r = next((x for x in ik.rows if x.id == rid), None)
    assert r is not None, f"missing row {rid}"
    return r


def test_parse_extracts_stated_fields_as_you_said():
    ik = I.parse_brief(BRIEF)
    assert _row(ik, "mission_type").value == "Surveillance"
    for rid, num in (("payload", 0.18), ("altitude", 100.0),
                     ("span_max", 2.0)):
        row = _row(ik, rid)
        assert row.origin == "you_said" and row.status == "fixed"
        assert row.number == pytest.approx(num)
    assert _row(ik, "launch").value == "hand"


def test_unmatched_fields_keep_cited_assumed_defaults():
    ik = I.parse_brief(BRIEF)
    for rid in ("structure", "airfoil", "wind", "regulatory"):
        row = _row(ik, rid)
        assert row.origin == "assumed" and row.source


def test_parser_never_invents_from_empty_brief():
    ik = I.parse_brief("make me something nice")
    assert all(r.origin == "assumed" for r in ik.rows)


def test_derive_adds_readonly_derived_tier_with_drivers():
    ik = I.derive(I.parse_brief(BRIEF))
    derived = [r for r in ik.rows if r.tier == "derived"]
    assert {r.id for r in derived} >= {"wing_loading", "s_ref", "cruise_speed",
                                       "cruise_cl", "clmax", "static_margin"}
    assert all(r.status == "derived" and r.origin == "derived" for r in derived)
    wl = _row(ik, "wing_loading")
    driver_ids = {d.row_id for d in wl.drivers}
    editable_ids = {r.id for r in ik.rows if r.status != "derived"}
    assert driver_ids and driver_ids <= editable_ids


def test_assumed_mtow_tracks_payload():
    ik = I.derive(I.parse_brief(BRIEF))
    assert _row(ik, "mtow_cap").number == pytest.approx(0.6, abs=0.01)


def test_feasibility_reports_implied_sizing():
    ik = I.derive(I.parse_brief(BRIEF))
    f = ik.feasibility
    assert f is not None and f.verdict in ("feasible", "binding")
    assert {"MTOW", "wing area", "cruise", "stall", "L/D"} <= set(f.implied)


def test_cruise_speed_is_best_ld_fixed_point():
    ik = I.derive(I.parse_brief(BRIEF))
    v = _row(ik, "cruise_speed").number
    assert 8.0 <= v <= 60.0
    # consistency: CL at that speed matches the panel's cruise CL
    assert _row(ik, "cruise_cl").number == pytest.approx(
        (0.6 * 9.81) / (0.5 * 1.213 * v ** 2 * _row(ik, "s_ref").number),
        rel=0.05)


def test_edit_flips_assumed_to_edited_and_rederives():
    ik = I.derive(I.parse_brief(BRIEF))
    ik = I.derive(I.apply_edits(ik, [{"id": "v_stall_max", "value": "≤ 8 m/s",
                                      "status": "fixed"}]))
    row = _row(ik, "v_stall_max")
    assert row.origin == "edited" and row.number == pytest.approx(8.0)
    assert ik.feasibility.verdict in ("feasible", "binding")
    # a tighter stall cap can only grow the wing
    assert _row(ik, "s_ref").number >= 0.102


def test_you_said_edit_keeps_you_said_origin():
    ik = I.derive(I.parse_brief(BRIEF))
    ik = I.derive(I.apply_edits(ik, [{"id": "span_max", "value": "≤ 1.5 m"}]))
    assert _row(ik, "span_max").origin == "you_said"
    assert _row(ik, "span_max").number == pytest.approx(1.5)


def test_derived_rows_reject_edits():
    ik = I.derive(I.parse_brief(BRIEF))
    before = _row(ik, "wing_loading").value
    ik = I.derive(I.apply_edits(ik, [{"id": "wing_loading", "value": "99"}]))
    assert _row(ik, "wing_loading").value == before


def test_an_unknown_row_id_is_refused_by_name():
    """S1-B: replaces test_custom_row_lands_in_vehicle_tier. That row landed in the
    vehicle tier and moved nothing; the requirement set is now closed."""
    ik = I.derive(I.parse_brief(BRIEF))
    before = [r.id for r in ik.rows]
    with pytest.raises(ValueError) as exc:
        I.apply_edits(ik, [{"id": "custom_max_chord_1", "label": "Max chord",
                            "value": "≤ 0.35 m", "status": "fixed"}])
    assert "custom_max_chord_1" in str(exc.value)
    assert "closed set" in str(exc.value)
    assert [r.id for r in ik.rows] == before, "a refused edit must not mutate the intake"


def test_an_unknown_id_without_a_label_is_also_refused():
    """The old code only created a row when a `label` was supplied, so an unknown id
    without one was dropped in silence — accepted with a 200 and no effect."""
    with pytest.raises(ValueError, match="closed set"):
        I.apply_edits(I.default_intake(), [{"id": "endurance", "value": "45 min"}])


def test_every_declared_row_id_is_editable():
    """The closed set must be the set that actually exists: every id known_row_ids()
    reports must be accepted by apply_edits, or the refusal message is a lie.

    This is the test that caught known_row_ids() being built from default_intake()
    alone, which omitted the seven rows derive() appends and made an edit to
    `cruise_speed` raise."""
    ik = I.derive(I.default_intake())
    assert len(I.known_row_ids()) == len(ik.rows)
    for rid in I.known_row_ids():
        I.apply_edits(ik, [{"id": rid, "value": "1"}])   # must not raise


def test_lock_flag_persists():
    """The lock flag survives a round-trip through apply_edits.

    RENAMED from test_lock_toggle_roundtrip. That name implied the lock feature works;
    this test only ever proved the FLAG is stored. Locks are currently inert — the
    solver never reads `status` — so the old name made a dead feature look verified.
    The real contract is asserted (and expected to fail) below.
    """
    ik = I.derive(I.parse_brief(BRIEF))
    ik = I.derive(I.apply_edits(ik, [{"id": "payload", "status": "variable"}]))
    assert _row(ik, "payload").status == "variable"
    ik = I.derive(I.apply_edits(ik, [{"id": "payload", "status": "fixed"}]))
    assert _row(ik, "payload").status == "fixed"


def _derived_signature(ik: I.Intake) -> tuple:
    """Every computed value in the derived tier, for equality comparison."""
    return tuple(
        (r.id, None if r.number is None else round(r.number, 9))
        for r in ik.rows if r.tier == "derived"
    )


def _intake_with_all_locks(status: str) -> I.Intake:
    ik = I.default_intake()
    for row in ik.rows:
        if row.status != "derived":
            row.status = status
    return ik


def test_locking_a_row_constrains_the_solve():
    """Locking every editable row must change the outcome versus freeing every row.

    This is the contract the UI advertises: a lock means 'the solver may not move
    this'. It was an xfail from S3-E1 until S1-F made it true — measured, the two
    signatures were byte-identical, and are now 7.064 vs 3.519 kg/m^2 wing loading.
    """
    free = I.derive(_intake_with_all_locks("variable"))
    fixed = I.derive(_intake_with_all_locks("fixed"))
    assert _derived_signature(free) != _derived_signature(fixed)


def test_tiny_aircraft_derives_without_error():
    ik = I.derive(I.parse_brief("carry a 50 g sensor, span under 1 m, hand launch"))
    assert ik.feasibility.verdict in ("feasible", "binding")
