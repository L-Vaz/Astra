"""DesignSpace tests — pure, no AeroSandbox.

Bounds derive from BWBParams' own Ge/Le metadata, so these check that the search
space can never drift outside the schema gate, that freezing works, and that the
free-vector <-> BWBParams mapping is correct.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from domain_packs.bwb.design_space import (DESIGN_FIELDS, DesignSpace, ParamBound,
                                           field_hard_range)
from domain_packs.bwb.schema import BWBParams


def _base() -> BWBParams:
    return BWBParams(span_m=1.8, root_chord_m=0.5, centre_body_fraction=0.28,
                     mid_chord_frac=0.55, taper_outer=0.5, sweep_inner_deg=34,
                     sweep_outer_deg=20, root_airfoil="e474", mid_airfoil="e472", tip_airfoil="s5010",
                     root_twist_deg=2, tip_twist_deg=-3, dihedral_deg=3)


def test_field_hard_range_matches_schema():
    assert field_hard_range("span_m") == (0.8, 3.0)
    assert field_hard_range("dihedral_deg") == (0.0, 8.0)
    assert field_hard_range("sweep_inner_deg") == (15.0, 60.0)


def test_from_base_covers_all_fields_within_schema():
    ds = DesignSpace.from_base(_base(), spread=0.15)
    assert set(ds.bounds) == set(DESIGN_FIELDS)
    for name, b in ds.bounds.items():
        lo, hi = field_hard_range(name)
        assert lo - 1e-9 <= b.lo <= b.hi <= hi + 1e-9
        assert b.lo <= b.nominal <= b.hi


def test_from_base_nominal_is_base_value():
    base = _base()
    ds = DesignSpace.from_base(base, spread=0.15)
    for name in DESIGN_FIELDS:
        assert ds.bounds[name].nominal == pytest.approx(getattr(base, name))


def test_spread_half_width_clamped_to_schema():
    base = _base()
    ds = DesignSpace.from_base(base, spread=0.1)
    lo, hi = field_hard_range("span_m")
    half = 0.1 * (hi - lo)
    assert ds.bounds["span_m"].lo == pytest.approx(max(lo, base.span_m - half))
    assert ds.bounds["span_m"].hi == pytest.approx(min(hi, base.span_m + half))
    # dihedral base=3, range 0..8, half=0.8 -> lo clamped at >=0
    assert ds.bounds["dihedral_deg"].lo == pytest.approx(3 - 0.8)


def test_freeze_excludes_from_free_dims():
    ds = DesignSpace.from_base(_base(), spread=0.15, frozen=["span_m", "dihedral_deg"])
    assert "span_m" not in ds.free_names()
    assert "dihedral_deg" not in ds.free_names()
    for n in ("span_m", "dihedral_deg"):
        b = ds.bounds[n]
        assert b.frozen and b.lo == b.hi == b.nominal
    assert len(ds.de_bounds()) == len(ds.free_names())


def test_freeze_method_matches_constructor():
    a = DesignSpace.from_base(_base(), frozen=["taper_outer"])
    b = DesignSpace.from_base(_base()).freeze("taper_outer")
    assert a.free_names() == b.free_names()


def test_vector_to_params_maps_free_and_keeps_frozen():
    base = _base()
    ds = DesignSpace.from_base(base, spread=0.15,
                              frozen=[n for n in DESIGN_FIELDS if n != "sweep_outer_deg"])
    assert ds.free_names() == ["sweep_outer_deg"]
    p = ds.vector_to_params([22.0])
    assert p.sweep_outer_deg == pytest.approx(22.0)
    # every frozen field keeps the base value
    for n in DESIGN_FIELDS:
        if n != "sweep_outer_deg":
            assert getattr(p, n) == pytest.approx(getattr(base, n))


def test_vector_wrong_length_raises():
    ds = DesignSpace.from_base(_base())
    with pytest.raises(ValueError):
        ds.vector_to_params([1.0])  # far too few dims


def test_nominal_round_trips():
    base = _base()
    nom = DesignSpace.from_base(base).nominal()
    assert nom.model_dump() == pytest.approx(base.model_dump())


def test_parambound_rejects_out_of_order():
    with pytest.raises(ValueError):
        ParamBound(lo=2.0, nominal=1.0, hi=3.0)


def test_designspace_rejects_bounds_beyond_schema():
    lo, hi = field_hard_range("span_m")
    bad = {n: ParamBound(lo=field_hard_range(n)[0], nominal=field_hard_range(n)[0],
                         hi=field_hard_range(n)[1]) for n in DESIGN_FIELDS}
    bad["span_m"] = ParamBound(lo=lo, nominal=hi, hi=hi + 5.0)  # beyond schema le
    with pytest.raises(ValueError):
        DesignSpace(bounds=bad)
