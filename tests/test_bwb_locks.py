"""Locks that constrain the sizing solve (S1-F).

The contract is one sentence: a lock is honoured exactly, or a `CapViolation` says by
how much it was not. There is no third outcome, and in particular no silent unlock.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from domain_packs.bwb import intake as I
from domain_packs.bwb.inverse import (LOCK_TOL, LockedValues, solve_requirements)
from domain_packs.bwb.requirements import DesignRequirements


def _req(**over) -> DesignRequirements:
    base = dict(mtow_kg=3.3, cruise_speed_ms=18.0, launch="hand")
    base.update(over)
    return DesignRequirements(**base)


# ------------------------------------------------------------------ span lock

@pytest.mark.parametrize("span", [1.2, 1.5, 1.8, 2.0])
def test_a_reachable_span_lock_is_honoured_exactly(span):
    """Not "within the cap" — exactly. Measured free span on this requirement set is
    1.6163 m, so every one of these is a real move in one direction or the other."""
    r = solve_requirements(_req(), locked=LockedValues(span_m=span))
    assert abs(r.params.span_m - span) <= LOCK_TOL["span_m"]
    assert not r.lock_violations


def test_the_span_lock_frees_area_and_aspect_ratio():
    """Pinning span leaves the solver two levers. Measured: locking 1.2 m grows the
    wing to 0.5089 m^2 at AR 2.83, while 2.0 m holds 0.4750 m^2 at AR 8.42."""
    tight = solve_requirements(_req(), locked=LockedValues(span_m=1.2))
    wide = solve_requirements(_req(), locked=LockedValues(span_m=2.0))
    assert tight.params.aspect_ratio() < wide.params.aspect_ratio()
    assert tight.params.aspect_ratio() < 4.0 < wide.params.aspect_ratio()


def test_an_unreachable_span_lock_reports_rather_than_unlocking():
    """Measured: a 2.5 m lock cannot be held because the AR gate (10) stops the wing
    at 2.1795 m. The solver returns the closest legal geometry AND says so."""
    r = solve_requirements(_req(), locked=LockedValues(span_m=2.5))
    assert r.lock_violations, "an unhonoured lock must never pass silently"
    v = next(x for x in r.lock_violations if x.cap == "span lock")
    assert v.required == 2.5
    assert v.achieved < 2.5
    assert "aspect ratio" in v.blocked_by
    assert r.params.span_m == pytest.approx(v.achieved)


# ------------------------------------------------------------------ area lock

@pytest.mark.parametrize("area", [0.60, 0.90, 1.20])
def test_a_reachable_area_lock_is_honoured_exactly(area):
    r = solve_requirements(_req(), locked=LockedValues(s_ref_m2=area))
    assert abs(r.params.planform_area_m2() - area) <= LOCK_TOL["s_ref_m2"]
    assert not r.lock_violations


def test_an_area_lock_below_the_stall_floor_is_honoured_and_the_conflict_stated():
    """The interesting case, because BOTH sides are the operator's instruction: they
    locked a wing they already own, and they asked for a hand launch. Measured: a
    0.30 m^2 lock against a mission needing 0.4750 m^2 to hold the stall cap.

    The lock wins — you cannot un-build a wing — and the unmet requirement is named."""
    r = solve_requirements(_req(), locked=LockedValues(s_ref_m2=0.30))
    assert r.params.planform_area_m2() == pytest.approx(0.30, abs=LOCK_TOL["s_ref_m2"])
    v = next(x for x in r.lock_violations if x.cap == "s_ref lock")
    assert v.achieved == pytest.approx(0.30)
    assert v.required > 0.30
    assert "honoured" in v.message and "requirement was not" in v.message


def test_the_area_lock_frees_span():
    """Measured: 0.60 m^2 locked gives 1.8166 m of span, 1.20 m^2 gives 2.5690 m —
    aspect ratio held at the shape's 5.5 in both."""
    small = solve_requirements(_req(), locked=LockedValues(s_ref_m2=0.60))
    big = solve_requirements(_req(), locked=LockedValues(s_ref_m2=1.20))
    assert big.params.span_m > small.params.span_m
    assert small.params.aspect_ratio() == pytest.approx(big.params.aspect_ratio(),
                                                        rel=1e-3)


# ------------------------------------------------------------ both, and AR

def test_locking_both_span_and_area_pins_aspect_ratio():
    """AR = b^2/S, so two locks are three. Measured: 1.5 m with 0.35 m^2 gives AR 6.43;
    1.5 m with 0.90 m^2 gives AR 2.50."""
    a = solve_requirements(_req(), locked=LockedValues(span_m=1.5, s_ref_m2=0.35))
    b = solve_requirements(_req(), locked=LockedValues(span_m=1.5, s_ref_m2=0.90))
    assert a.params.aspect_ratio() == pytest.approx(1.5 ** 2 / 0.35, rel=1e-3)
    assert b.params.aspect_ratio() == pytest.approx(1.5 ** 2 / 0.90, rel=1e-3)


def test_an_impossible_pair_reports_every_lock_it_broke():
    """0.9 m span with 0.05 m^2 of wing is outside the schema in two directions at
    once. Measured: three violations, and the geometry returned is legal."""
    r = solve_requirements(_req(), locked=LockedValues(span_m=0.9, s_ref_m2=0.05))
    caps = {v.cap for v in r.lock_violations}
    assert caps == {"span lock", "s_ref lock"}
    assert len(r.lock_violations) >= 2
    assert r.params.span_m >= 0.8          # the schema minimum still holds


# ------------------------------------------------------- the L/D loop interaction

def test_a_geometry_lock_stops_the_ld_loop_instead_of_breaking_the_lock():
    """The L/D loop raises aspect ratio by trading area against span. Under a lock it
    has no lever left, so it must not run — chasing L/D by quietly moving a locked
    number is precisely the silent unlock this item forbids. It says so in a note."""
    req = _req(ld_min=40.0)          # unreachable on purpose
    r = solve_requirements(req, locked=LockedValues(span_m=1.5))
    assert abs(r.params.span_m - 1.5) <= LOCK_TOL["span_m"]
    assert any("lock" in n and "aspect ratio" in n for n in r.notes), r.notes


def test_without_a_lock_the_ld_loop_still_runs():
    """Guard the guard: the note above must not be the loop being broken for everyone."""
    free_low = solve_requirements(_req(ld_min=1.0))
    free_high = solve_requirements(_req(ld_min=40.0))
    assert free_high.params.aspect_ratio() > free_low.params.aspect_ratio()


# ------------------------------------------------------------------- no lock

def test_no_locks_is_bit_identical_to_before():
    """A LockedValues with nothing set must be indistinguishable from passing none —
    otherwise every existing golden moved for no physical reason."""
    a = solve_requirements(_req())
    b = solve_requirements(_req(), locked=LockedValues())
    assert a.params.model_dump() == b.params.model_dump()
    assert not a.lock_violations and not b.lock_violations


# ------------------------------------------------------------------- the intake

def test_a_locked_mtow_stops_following_the_payload_row():
    """The one thing the intake changes behind the operator's back: an ASSUMED MTOW
    tracks payload / payload-fraction. A lock must stop it, and nothing else does."""
    free = I.default_intake()
    I.apply_edits(free, [{"id": "payload", "value": "1.5 kg"}])
    free = I.derive(free)
    assert I._row(free, "mtow_cap").number == pytest.approx(1.5 / 0.30, abs=0.02)

    pinned = I.default_intake()
    I._row(pinned, "mtow_cap").status = "fixed"
    I.apply_edits(pinned, [{"id": "payload", "value": "1.5 kg"}])
    pinned = I.derive(pinned)
    assert I._row(pinned, "mtow_cap").number == pytest.approx(3.3)


def test_to_locked_ignores_a_toggle_with_no_number():
    """A lock with nothing to lock to is a toggle flipped early, not a constraint."""
    ik = I.default_intake()
    row = I._row(ik, "span_max")
    row.status = "fixed"
    row.number = None
    assert I.to_locked(ik).span_m is None
    assert not I.to_locked(ik).any_locked


def test_to_locked_reads_only_fixed_rows():
    ik = I.derive(I.default_intake())
    assert not I.to_locked(ik).any_locked
    I._row(ik, "span_max").status = "fixed"
    locked = I.to_locked(ik)
    assert locked.span_m == pytest.approx(3.0)
    assert locked.s_ref_m2 is None and locked.mtow_kg is None
