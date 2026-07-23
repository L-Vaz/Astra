"""BWB domain pack — optimisation design space (Slice 2).

A `DesignSpace` wraps `BWBParams` with per-field search bounds `(lo, nominal, hi)`
plus a `frozen` flag, centred on a base wing. It is the contract between the cockpit
(range sliders) and the optimiser (`differential_evolution` bounds):

  - `free_names()` / `de_bounds()` feed scipy DE (frozen fields are held out).
  - `vector_to_params(x)` maps a free-dim vector back to a full, validated BWBParams
    (frozen fields take their nominal). BWBParams' own validators are the geometry
    gate, so an out-of-gate vector raises and the optimiser scores it infeasible.

Field ranges come from `BWBParams` itself (its pydantic Ge/Le metadata), so the hard
bounds have a single source of truth and can never drift from the schema. The design
vector is the existing 12-variable model; the extended 16-parameter station schema
(future phase) can implement this same interface without touching the optimiser.

Pydantic/annotated-types only — no aerosandbox, so this imports anywhere.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from annotated_types import Ge, Le
from pydantic import BaseModel, Field, model_validator

from .schema import BWBParams

# BWBParams fields that are numeric design variables (all of them, in declared order).
DESIGN_FIELDS: List[str] = [
    "span_m", "root_chord_m", "centre_body_fraction", "mid_chord_frac",
    "taper_outer", "sweep_inner_deg", "sweep_outer_deg", "root_thickness_pct",
    "tip_thickness_pct", "root_twist_deg", "tip_twist_deg", "dihedral_deg",
]


def field_hard_range(name: str) -> tuple:
    """(ge, le) hard bounds declared on a BWBParams field. Single source of truth."""
    field = BWBParams.model_fields[name]
    lo = hi = None
    for m in field.metadata:
        if isinstance(m, Ge):
            lo = float(m.ge)
        elif isinstance(m, Le):
            hi = float(m.le)
    if lo is None or hi is None:
        raise ValueError(f"BWBParams.{name} has no Ge/Le bounds to derive a range from")
    return lo, hi


class ParamBound(BaseModel):
    lo: float
    nominal: float
    hi: float
    frozen: bool = False

    @model_validator(mode="after")
    def _ordered(self) -> "ParamBound":
        if not (self.lo <= self.nominal <= self.hi):
            raise ValueError(
                f"bound out of order: lo={self.lo} nominal={self.nominal} hi={self.hi}")
        return self

    @property
    def width(self) -> float:
        return self.hi - self.lo


class DesignSpace(BaseModel):
    bounds: Dict[str, ParamBound] = Field(
        description="per-BWBParams-field search bounds, keyed by field name")

    @model_validator(mode="after")
    def _known_fields(self) -> "DesignSpace":
        unknown = set(self.bounds) - set(DESIGN_FIELDS)
        if unknown:
            raise ValueError(f"unknown design fields: {sorted(unknown)}")
        missing = set(DESIGN_FIELDS) - set(self.bounds)
        if missing:
            raise ValueError(f"design space missing fields: {sorted(missing)}")
        # every bound must stay inside the schema's hard range
        for name, b in self.bounds.items():
            lo, hi = field_hard_range(name)
            if b.lo < lo - 1e-9 or b.hi > hi + 1e-9:
                raise ValueError(
                    f"{name} bounds [{b.lo}, {b.hi}] exceed schema range [{lo}, {hi}]")
        return self

    # ------------------------------------------------------------------ build
    @classmethod
    def from_base(cls, base: BWBParams, spread: float = 0.15,
                  frozen: Optional[List[str]] = None) -> "DesignSpace":
        """Bounds centred on `base`, each half-width = `spread` * the field's hard
        range, clamped into that range. `frozen` fields collapse to their nominal."""
        frozen = set(frozen or [])
        bounds: Dict[str, ParamBound] = {}
        for name in DESIGN_FIELDS:
            lo_hard, hi_hard = field_hard_range(name)
            nominal = float(getattr(base, name))
            half = spread * (hi_hard - lo_hard)
            lo = max(lo_hard, nominal - half)
            hi = min(hi_hard, nominal + half)
            is_frozen = name in frozen
            if is_frozen:
                lo = hi = nominal
            bounds[name] = ParamBound(lo=lo, nominal=nominal, hi=hi, frozen=is_frozen)
        return cls(bounds=bounds)

    def freeze(self, *names: str) -> "DesignSpace":
        """Return a copy with the named fields frozen at their nominal."""
        new = {}
        for name, b in self.bounds.items():
            if name in names:
                new[name] = ParamBound(lo=b.nominal, nominal=b.nominal,
                                       hi=b.nominal, frozen=True)
            else:
                new[name] = b
        return DesignSpace(bounds=new)

    # ------------------------------------------------------------------ DE glue
    def free_names(self) -> List[str]:
        """Fields the optimiser varies (unfrozen and non-degenerate), in canonical
        order."""
        return [n for n in DESIGN_FIELDS
                if not self.bounds[n].frozen and self.bounds[n].width > 0]

    def de_bounds(self) -> List[tuple]:
        """[(lo, hi)] for each free dimension, in free_names() order."""
        return [(self.bounds[n].lo, self.bounds[n].hi) for n in self.free_names()]

    def nominal(self) -> BWBParams:
        return BWBParams(**{n: self.bounds[n].nominal for n in DESIGN_FIELDS})

    def vector_to_params(self, x) -> BWBParams:
        """Map a free-dim vector back to a full BWBParams (frozen -> nominal).
        Raises pydantic ValidationError if the result is out of gate — the caller
        treats that as an infeasible design."""
        free = self.free_names()
        if len(x) != len(free):
            raise ValueError(f"expected {len(free)} free dims, got {len(x)}")
        values = {n: self.bounds[n].nominal for n in DESIGN_FIELDS}
        for name, xi in zip(free, x):
            values[name] = float(xi)
        return BWBParams(**values)
