"""Shared hypothesis strategies for the BWB domain schemas (S0-10).

The solver is a bounded fixed-point with clamps and defensive repairs — exactly the
shape of code where hand-picked cases give false confidence. Both crashes found in this
codebase so far (a sub-minimum planform on a very small aircraft, and an
aspect-ratio-gate breach on a heavy aircraft with a tight span cap) were envelope
corners discovered by accident, not by a test.

These strategies generate inputs across the FULL legal envelope so property tests can
assert contracts ("never raises", "the result always re-validates") rather than
individual values.

Pure `domain_packs` — importable on any interpreter, no WSL required.
"""
from hypothesis import assume
from hypothesis import strategies as st
from pydantic import ValidationError

from domain_packs.bwb.requirements import DesignRequirements
from domain_packs.bwb.schema import BWBParams

LAUNCH_METHODS = ["hand", "catapult", "runway"]

# Field ranges mirror the pydantic constraints in domain_packs/bwb/schema.py. Kept as
# floats (not integers) so boundary arithmetic is exercised.
_PARAM_RANGES = {
    "span_m": (0.8, 3.0),
    "root_chord_m": (0.25, 1.2),
    "centre_body_fraction": (0.12, 0.45),
    "mid_chord_frac": (0.35, 0.85),
    "taper_outer": (0.15, 0.7),
    "sweep_inner_deg": (15.0, 60.0),
    "sweep_outer_deg": (5.0, 40.0),
    "root_twist_deg": (-2.0, 5.0),
    "tip_twist_deg": (-7.0, 2.0),
    "dihedral_deg": (0.0, 8.0),
}

# S1-C: thickness is no longer a continuous field to draw — the section owns it. Drawn
# from a fixed set of triples instead, each already thinning outboard so the draw is
# not wasted on combinations the validator rejects. Spans 8.4% to 15.0% root thickness,
# which covers the range the old (8-20, 5-14) float ranges reached in practice.
_SECTION_SETS = [
    ("e474", "e472", "s5010"),     # 14.07 / 12.09 /  9.82  — the pack default
    ("e475", "e474", "oa209"),     # 14.99 / 14.07 /  9.01  — thickest root
    ("e328", "oa212", "oa209"),    # 13.33 / 12.01 /  9.01
    ("oa212", "s3", "s5020"),      # 12.01 / 11.00 /  8.40  — thinnest
]


def _floats(lo: float, hi: float):
    return st.floats(min_value=lo, max_value=hi,
                     allow_nan=False, allow_infinity=False, width=64)


@st.composite
def bwb_params(draw) -> BWBParams:
    """A schema-valid BWBParams.

    Field ranges alone are not sufficient: BWBParams also enforces cross-checks (tip
    chord >= 0.04 m, tip no thicker than root, aspect ratio in [2.5, 10], no wash-in).
    Roughly half of all in-range combinations fail them, so we draw and `assume`.
    """
    kwargs = {name: draw(_floats(lo, hi)) for name, (lo, hi) in _PARAM_RANGES.items()}
    root, mid, tip = draw(st.sampled_from(_SECTION_SETS))
    kwargs.update(root_airfoil=root, mid_airfoil=mid, tip_airfoil=tip)
    try:
        return BWBParams(**kwargs)
    except ValidationError:
        assume(False)  # tell hypothesis to retry with a different draw


@st.composite
def design_requirements(draw) -> DesignRequirements:
    """A legal DesignRequirements across the whole envelope.

    Optional targets are drawn as None roughly half the time, because "no requirement
    set" is the common real case and behaves differently — notably, an unset stall row
    means the launch-method cap is the only cap in play.
    """
    optional = lambda s: st.one_of(st.none(), s)  # noqa: E731
    return DesignRequirements(
        mtow_kg=draw(_floats(0.05, 25.0)),
        cruise_speed_ms=draw(_floats(8.0, 60.0)),
        cruise_altitude_m=draw(_floats(0.0, 4000.0)),
        launch=draw(st.sampled_from(LAUNCH_METHODS)),
        cl_cruise_target=draw(optional(_floats(0.0501, 0.75))),
        ld_min=draw(optional(_floats(0.1, 40.0))),
        v_stall_max_ms=draw(optional(_floats(5.0, 20.0))),
        span_max_m=draw(optional(_floats(0.8, 3.0))),
    )


def row_values():
    """Strings a user could plausibly type into a requirement row.

    Deliberately includes the forms the current parser mishandles (signs, thousands
    separators, imperial units, exponent overflow) so property tests can pin the
    contract rather than the current behaviour.
    """
    return st.one_of(
        st.just(""),
        st.just("   "),
        st.text(max_size=40),
        st.from_regex(r"-?\d{1,4}(\.\d{1,3})? ?(m|cm|mm|kg|g|m/s|ft|in)?\Z",
                      fullmatch=True),
        st.sampled_from([
            "-3 kg", "1,500", "1800 mm", "6 ft", "20in", "1e400", "9" * 400,
            "0", "-0", "NaN", "inf", "abc",
        ]),
    )
