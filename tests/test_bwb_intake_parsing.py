"""The intake parser: units, signs, magnitudes, anchoring, merging (S2-D1/D3/D4/D5).

Every case below was measured on the live parser before Stage 2, and every one of them
was HTTP 200 and invisible — the row displayed the user's text while the solver used a
different number, or the same number in a different unit. That combination is worse
than an error: the screen agrees with you and the aircraft does not.

  "-3 kg"        -> +3.0    sign dropped
  "1,500 g"      -> 1.5     thousands separator read as a decimal point (1000x)
  "1.5e3"        -> 1.5     exponent truncated (1000x)
  "9"*400        -> inf     serialised as null; the row silently lost its value
  "1800 mm"      -> 1800 m  unit honoured in the brief, ignored in an edit
  "abc"          -> None    row displayed "abc" as the requirement
  "2.2 m span,
   fly at 300 m" -> span 300 m AND altitude assumed — both stamped "you_said"

Pure `domain_packs`: pydantic only, so this runs on the Windows python as well as in
WSL, which keeps the parser's iteration loop fast.
"""
import math

import pytest

from domain_packs.bwb import intake as I

PROBE_BRIEF = ("Surveillance BWB, 250 g camera, 2.2 m span, fly at 300 m, "
               "45 min endurance, hand launch")


def _row(intake: I.Intake, rid: str):
    return next(r for r in intake.rows if r.id == rid)


def _edit(rid: str, value):
    return I.apply_edits(I.default_intake(), [{"id": rid, "value": value}])


def _messages(warnings) -> list:
    """Warnings are structured (S3-D2); most assertions here are about the prose."""
    return [w.message for w in warnings]


# ===================================================================================
# S2-D1 — one sign-, unit- and magnitude-aware parser
# ===================================================================================

@pytest.mark.parametrize("typed,expected", [
    ("-3 kg", -3.0),
    ("-100", -100.0),
    ("+2.5 kg", 2.5),
])
def test_sign_is_preserved(typed, expected):
    """The old regex started at the first digit, so the minus was simply not there.

    Keeping the sign is the parser's job; deciding a negative mass is unusable belongs
    to the boundary (S2-B5), and the two must not be confused — a parser that silently
    flips the sign leaves nothing downstream able to notice."""
    assert _row(_edit("mtow_cap", typed), "mtow_cap").number == pytest.approx(expected)


@pytest.mark.parametrize("typed,expected", [
    ("1,500 g", 1.5),          # thousands separator, then g -> kg
    ("1,500", 1500.0),
    ("12,345 g", 12.345),
    ("1,5 kg", 1.5),           # European decimal comma
])
def test_commas_follow_the_locale_free_rule(typed, expected):
    """A comma before exactly three digits is a separator; before one or two, a decimal
    point. The old code replaced every comma with a period, so "1,500" became 1.5."""
    assert _row(_edit("mtow_cap", typed), "mtow_cap").number == pytest.approx(expected)


def test_an_ambiguous_comma_is_refused_rather_than_guessed():
    row = _row(_edit("mtow_cap", "1,23,456"), "mtow_cap")
    assert row.unparsed is True
    assert row.number is None
    assert row.value == "1,23,456", "the user's text must survive a parse failure"


@pytest.mark.parametrize("typed,expected", [
    ("1.5e3", 1500.0),
    ("2E2 kg", 200.0),
])
def test_exponent_notation_is_read_not_truncated(typed, expected):
    """"1.5e3" used to become 1.5 — the same 1000x class as the comma bug."""
    assert _row(_edit("mtow_cap", typed), "mtow_cap").number == pytest.approx(expected)


@pytest.mark.parametrize("typed", ["1e400", "9" * 400, "-1e400"])
def test_unrepresentable_magnitudes_are_refused_not_turned_into_infinity(typed):
    """inf serialises to null, so the row silently LOST the user's value."""
    row = _row(_edit("mtow_cap", typed), "mtow_cap")
    assert row.number is None or math.isfinite(row.number)
    assert row.unparsed is True
    assert "too large" in row.parse_error


@pytest.mark.parametrize("typed,expected", [
    ("1800 mm", 1.8),
    ("180 cm", 1.8),
    ("6 ft", 1.8288),
    ("20in", 0.508),
    ("2.2 m", 2.2),
    ("2.2", 2.2),               # bare number: read in the row's canonical unit
])
def test_length_units_are_converted(typed, expected):
    assert _row(_edit("span_max", typed), "span_max").number == pytest.approx(expected)


@pytest.mark.parametrize("typed,expected", [
    ("250 g", 0.25),
    ("2.5 kg", 2.5),
    ("5 lb", 2.26796185),
])
def test_mass_units_are_converted(typed, expected):
    assert _row(_edit("payload", typed), "payload").number == pytest.approx(expected)


@pytest.mark.parametrize("typed,expected", [
    ("12 m/s", 12.0),
    ("25 kt", 12.86111111),
    ("45 km/h", 12.5),
    ("28 mph", 12.51712),
])
def test_speed_units_are_converted(typed, expected):
    assert _row(_edit("v_stall_max", typed), "v_stall_max").number == pytest.approx(
        expected)


@pytest.mark.parametrize("typed,expected", [("90 min", 90.0), ("1.5 h", 90.0)])
def test_time_units_are_converted(typed, expected):
    assert _row(_edit("endurance", typed), "endurance").number == pytest.approx(expected)


def test_a_unit_from_the_wrong_dimension_is_refused_by_name():
    """Silently ignoring the unit is what made "6 ft" mean 6 metres. Naming it lets the
    user see what went wrong."""
    row = _row(_edit("span_max", "3 kg"), "span_max")
    assert row.unparsed is True
    assert "'kg' is not a length unit" in row.parse_error


@pytest.mark.parametrize("typed", ["abc", "", "   ", "soon", "?" * 500])
def test_junk_marks_the_row_unparsed_and_keeps_the_text(typed):
    """The row used to display the junk AS the requirement while the solver quietly
    used the schema default (S2-D1f)."""
    row = _row(_edit("span_max", typed), "span_max")
    assert row.number is None
    assert row.unparsed is True
    assert row.value == typed


def test_text_rows_are_never_marked_unparsed():
    """Mission type and structure are prose. A missing number there is not an error."""
    for rid, value in (("mission_type", "Racing / speed"), ("structure", "carbon")):
        row = _row(_edit(rid, value), rid)
        assert row.unparsed is False
        assert row.value == value


@pytest.mark.parametrize("typed,dimension,expected", [
    ("1800 mm", "length", 1.8),
    ("180 cm", "length", 1.8),
    ("6 ft", "length", 1.8288),
    ("250 g", "mass", 0.25),
    ("25 kt", "speed", 12.861111111111112),
    ("2 h", "time", 120.0),
])
def test_brief_and_edit_paths_agree(typed, dimension, expected):
    """The same string must mean the same thing however it arrives.

    Before this the BRIEF path converted mm/cm and the EDIT path did not, so
    "1800 mm" was 1.8 m when written in a brief and 1800 m when typed into the row."""
    rid = {"length": "span_max", "mass": "payload", "speed": "v_stall_max",
           "time": "endurance"}[dimension]
    keyword = {"span_max": "span", "payload": "payload", "v_stall_max": "stall",
               "endurance": "endurance"}[rid]
    from_edit = _row(_edit(rid, typed), rid).number
    from_brief = _row(I.parse_brief(f"{keyword} {typed}"), rid).number
    assert from_edit == pytest.approx(expected)
    assert from_brief == pytest.approx(expected)


# ===================================================================================
# S2-D3 — the edit payload is type-gated
# ===================================================================================

@pytest.mark.parametrize("value", [[1, 2], {"a": 1}, True, 3 + 0j])
def test_non_scalar_edit_values_are_rejected(value):
    """`value: [1,2]` used to be stored as the string "[1, 2]" with number 1.0. No
    Python repr may reach a user-visible requirement."""
    with pytest.raises(ValueError, match="must be text or a number"):
        I.apply_edits(I.default_intake(), [{"id": "payload", "value": value}])


def test_null_value_restores_the_assumed_default():
    """`null` means "clear this row" — it used to store the literal string "None"."""
    edited = _edit("payload", "7 kg")
    assert _row(edited, "payload").number == pytest.approx(7.0)
    cleared = I.apply_edits(edited, [{"id": "payload", "value": None}])
    row, default = _row(cleared, "payload"), _row(I.default_intake(), "payload")
    assert (row.value, row.number, row.origin) == (default.value, default.number,
                                                   default.origin)


def test_null_value_removes_a_custom_row():
    added = I.apply_edits(I.default_intake(),
                          [{"id": "custom_chord", "label": "Max chord",
                            "value": "0.35 m"}])
    cleared = I.apply_edits(added, [{"id": "custom_chord", "value": None}])
    assert not any(r.id == "custom_chord" for r in cleared.rows)


# ===================================================================================
# S2-D4 — every brief pattern is anchored to its own number
# ===================================================================================

def test_probe_brief_parses_span_and_altitude_correctly():
    """The exact brief from the probe. Both rows were wrong, and BOTH were stamped
    origin="you_said" — presented back to the user as their own words."""
    ik = I.parse_brief(PROBE_BRIEF)
    assert _row(ik, "span_max").number == pytest.approx(2.2)
    assert _row(ik, "altitude").number == pytest.approx(300.0)
    assert _row(ik, "span_max").origin == "you_said"
    assert _row(ik, "altitude").origin == "you_said"
    assert _row(ik, "payload").number == pytest.approx(0.25)
    assert _row(ik, "endurance").number == pytest.approx(45.0)
    assert _row(ik, "launch").value == "hand"


@pytest.mark.parametrize("brief", [
    "2.2 m span, hand launch",
    "span 2.2 m",
    "span of 2.2 m",
    "max span 2.2 m",
    "wingspan 2.2 m",
    "2.2 m wingspan",
    "span: 2.2 m",
    "keep the span under 2.2 m",
    "2200 mm span",
    "220 cm span",
])
def test_span_before_and_after_keyword(brief):
    assert _row(I.parse_brief(brief), "span_max").number == pytest.approx(2.2)


def test_a_keyword_cannot_reach_across_a_clause_for_its_number():
    """The gap between a keyword and its number may not cross a comma: that is a
    different clause, and reaching into it is how span stole the altitude."""
    ik = I.parse_brief("hand launch, fly at 300 m")
    assert _row(ik, "span_max").origin == "assumed", "span was not stated"
    assert _row(ik, "altitude").number == pytest.approx(300.0)


def test_ambiguous_span_is_reported():
    ik = I.parse_brief("span 2 m but also span 5 m")
    assert _row(ik, "span_max").number == pytest.approx(2.0), "keep the first"
    assert any("2 m" in m and "5 m" in m for m in _messages(ik.warnings)), ik.warnings
    assert {w.kind for w in ik.warnings} == {"ambiguous"}


@pytest.mark.parametrize("brief,expected", [
    ("hand launch", "hand"),
    ("throw it by hand", "hand"),
    ("catapult launch", "catapult"),
    ("bungee launch", "catapult"),
    ("runway takeoff", "runway"),
    ("wheeled takeoff", "runway"),
    # every family at once: most specific must win, deterministically
    ("hand, catapult, bungee, runway, wheel, throw", "runway"),
    ("thrown from a catapult", "catapult"),
])
def test_all_launch_keywords_resolve_deterministically(brief, expected):
    """Resolution used to follow dict order — an implementation detail no user could
    predict from the screen."""
    assert _row(I.parse_brief(brief), "launch").value == expected


def test_multiple_launch_families_are_reported():
    ik = I.parse_brief("hand launch or catapult")
    assert _row(ik, "launch").value == "catapult"
    assert any("catapult" in m and "hand" in m for m in _messages(ik.warnings)), \
        ik.warnings


def test_an_explicit_stall_beats_the_launch_default():
    ik = I.parse_brief("hand launch, stall 8 m/s")
    assert _row(ik, "v_stall_max").number == pytest.approx(8.0)
    assert _row(ik, "v_stall_max").origin == "you_said"


# ===================================================================================
# S2-B1 — endurance is recorded and capped, never fatal
# ===================================================================================

@pytest.mark.parametrize("brief,expected", [
    ("loiter for 11 hours", 600.0),
    ("12 hour endurance", 600.0),
    ("endurance 900 min", 600.0),
    ("45 min endurance", 45.0),
])
def test_endurance_is_clamped_at_parse_time(brief, expected):
    """MissionSpec caps endurance_min at 600. Parsing 660 and letting every later
    generate raise is indefensible for a row that drives no computation."""
    row = _row(I.parse_brief(brief), "endurance")
    assert row.number == pytest.approx(expected)
    if expected == 600.0:
        assert "capped at 600 min" in row.source


# ===================================================================================
# S2-B5 — no stored number, however damaged, reaches the solver unchecked
# ===================================================================================

@pytest.mark.parametrize("rid,number", [
    ("payload", -3.0), ("mtow_cap", -1.0), ("altitude", -500.0),
    ("span_max", float("nan")), ("v_stall_max", float("inf")),
])
def test_unusable_stored_numbers_fall_back_to_defaults(rid, number):
    """A stored intake is a file, and S1-B3 shows files get damaged. Negative,
    NaN and inf must not reach DesignRequirements — that is a 500."""
    ik = I.default_intake()
    _row(ik, rid).number = number
    assert I.usable_number(ik, rid) is None
    requirements = I.to_requirements(ik)      # must not raise
    assert requirements.mtow_kg > 0
    rejected = [w for w in I.substitution_warnings(ik) if w.row_id == rid]
    assert rejected and rejected[0].kind == "rejected", I.substitution_warnings(ik)


def test_an_unparsed_row_is_reported_as_unusable():
    ik = I.derive(_edit("span_max", "wide enough"))
    unparsed = [w for w in ik.warnings if w.kind == "unparsed"]
    assert unparsed and unparsed[0].row_id == "span_max"
    assert "could not read" in unparsed[0].message


# ===================================================================================
# S2-D5 — parsing a brief merges, never resets
# ===================================================================================

def test_parse_preserves_user_edits_and_custom_rows():
    edited = I.apply_edits(I.parse_brief(PROBE_BRIEF), [
        {"id": "mtow_cap", "value": "4.5 kg", "status": "fixed"},
        {"id": "custom_max_chord", "label": "Max chord", "value": "0.35 m"},
    ])
    merged = I.merge_brief(edited, "surveillance, 2.2 m span, hand launch")
    assert _row(merged, "mtow_cap").number == pytest.approx(4.5)
    assert any(r.id == "custom_max_chord" for r in merged.rows)


def test_brief_overriding_an_edit_is_reported():
    edited = I.apply_edits(I.default_intake(), [{"id": "span_max", "value": "1.4 m"}])
    merged = I.merge_brief(edited, "2.2 m span")
    assert _row(merged, "span_max").number == pytest.approx(2.2), "the brief wins"
    assert any("1.4" in m and "2.2" in m for m in _messages(merged.warnings)), \
        merged.warnings


def test_a_silent_brief_does_not_disturb_a_stored_row():
    edited = I.apply_edits(I.default_intake(), [{"id": "span_max", "value": "1.4 m"}])
    merged = I.merge_brief(edited, "hand launch")
    assert _row(merged, "span_max").number == pytest.approx(1.4)
    assert not merged.warnings


def test_replace_starts_from_defaults():
    """The deliberate start-over path stays available — it is just no longer the only
    behaviour."""
    edited = I.apply_edits(I.default_intake(), [{"id": "span_max", "value": "1.4 m"}])
    fresh = I.parse_brief("hand launch")
    assert _row(fresh, "span_max").number == pytest.approx(
        _row(I.default_intake(), "span_max").number)
    assert _row(edited, "span_max").number == pytest.approx(1.4)


# ===================================================================================
# S2-D7 — an aircraft cannot weigh less than what it carries
# ===================================================================================

def test_mission_spec_rejects_payload_above_mtow():
    from domain_packs.bwb.schema import MissionSpec

    with pytest.raises(ValueError, match="below the payload"):
        MissionSpec(payload_kg=6.0, cruise_speed_ms=18.0, cruise_altitude_m=100.0,
                    mtow_kg=1.0)


def test_mission_spec_allows_mtow_equal_to_payload():
    from domain_packs.bwb.schema import MissionSpec

    spec = MissionSpec(payload_kg=2.0, cruise_speed_ms=18.0, cruise_altitude_m=100.0,
                       mtow_kg=2.0)
    assert spec.mtow_kg == pytest.approx(spec.payload_kg)
