"""BWB domain pack — parameter and mission schemas.

Shared by the FoamAgent conda env (py3.12) and the OpenVSP venv (py3.10):
pydantic-only, no OpenVSP imports here.
"""
import math
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .atmosphere import AtmosphereState, atmosphere_at
from .constants import STATIC_MARGIN_TARGET


class MissionSpec(BaseModel):
    """What the user wants the drone to do. Parsed from natural language."""
    payload_kg: float = Field(gt=0, le=10, description="Payload mass in kg")
    cruise_speed_ms: float = Field(ge=8, le=60, description="Cruise speed in m/s")
    cruise_altitude_m: float = Field(ge=0, le=4000, description="Cruise altitude AMSL in m")
    mtow_kg: Optional[float] = Field(default=None, gt=0, le=25,
                                     description="Max takeoff mass in kg; estimated from payload if absent")
    endurance_min: Optional[float] = Field(default=None, gt=0, le=600,
                                           description="Recorded goal only — not evaluated (propulsion/energy out of scope)")
    range_km: Optional[float] = Field(default=None, gt=0, le=500,
                                      description="Recorded goal only — not evaluated (propulsion/energy out of scope)")
    launch: Literal["hand", "catapult", "runway"] = "hand"

    @model_validator(mode="after")
    def cross_checks(self) -> "MissionSpec":
        """An aircraft cannot weigh less than what it carries (S2-D7).

        Probe-confirmed before this: payload 6 kg with an MTOW cap of 1 kg wrote a
        mission_derived.json holding mission.mtow_kg = 1.0 alongside
        mission.payload_kg = 6.0 — an artifact that is internally impossible and that
        every downstream stage then reasoned from. Same class of cross-check BWBParams
        already carries."""
        if self.mtow_kg is not None and self.mtow_kg < self.payload_kg:
            raise ValueError(
                f"MTOW {self.mtow_kg:g} kg is below the payload {self.payload_kg:g} kg "
                f"— an aircraft cannot weigh less than what it carries")
        return self


class BWBParams(BaseModel):
    """Parametric blended-wing-body planform. Hard ranges are the geometry gate.

    `validate_assignment=True` because this model is passed by reference and mutated
    (S6-F11): with pydantic's default, `p.centre_body_fraction = 0.0` was accepted and
    then `effective_c4_sweep_deg` and `cl_max_3d` raised ZeroDivisionError while
    `neutral_point_m` returned a SILENTLY WRONG 0.1727 and `mean_tc` a wrong 0.11.
    CLAUDE.md flags mutable-by-reference state as a live pattern in this codebase, so
    the constructor's guarantees have to survive assignment."""

    model_config = ConfigDict(validate_assignment=True)
    span_m: float = Field(ge=0.8, le=3.0, description="Total span, tip to tip")
    root_chord_m: float = Field(ge=0.25, le=1.2, description="Centre-body root chord")
    centre_body_fraction: float = Field(ge=0.12, le=0.45,
                                        description="Centre-body width / span")
    mid_chord_frac: float = Field(ge=0.35, le=0.85,
                                  description="Chord at centre-body/wing blend, as fraction of root chord")
    taper_outer: float = Field(ge=0.15, le=0.7, description="Tip chord / blend chord")
    sweep_inner_deg: float = Field(ge=15, le=60, description="Centre-body LE sweep")
    sweep_outer_deg: float = Field(ge=5, le=40, description="Outer panel LE sweep")
    root_thickness_pct: float = Field(ge=8, le=20, description="Root airfoil t/c %")
    tip_thickness_pct: float = Field(ge=5, le=14, description="Tip airfoil t/c %")
    root_twist_deg: float = Field(ge=-2, le=5, description="Root incidence")
    tip_twist_deg: float = Field(ge=-7, le=2, description="Tip twist (negative = washout)")
    dihedral_deg: float = Field(ge=0, le=8)

    @model_validator(mode="after")
    def cross_checks(self) -> "BWBParams":
        tip_chord = self.root_chord_m * self.mid_chord_frac * self.taper_outer
        if tip_chord < 0.04:
            raise ValueError(
                f"tip chord {tip_chord:.3f} m below 0.04 m manufacturable minimum")
        if self.tip_thickness_pct > self.root_thickness_pct:
            raise ValueError("tip airfoil thicker than root airfoil")
        s_ref = self.planform_area_m2()
        ar = self.span_m ** 2 / s_ref
        if not 2.5 <= ar <= 10.0:
            raise ValueError(f"aspect ratio {ar:.2f} outside sane BWB range [2.5, 10]")
        if self.tip_twist_deg > self.root_twist_deg:
            raise ValueError("tip twist exceeds root twist (wash-in) — unstable for tailless BWB")
        return self

    # -- derived planform quantities (analytic, two trapezoid panels per half) --
    def chords(self) -> tuple:
        c_root = self.root_chord_m
        c_mid = c_root * self.mid_chord_frac
        c_tip = c_mid * self.taper_outer
        return c_root, c_mid, c_tip

    def half_widths(self) -> tuple:
        b1 = self.centre_body_fraction * self.span_m / 2.0
        b2 = self.span_m / 2.0 - b1
        return b1, b2

    def planform_area_m2(self) -> float:
        c_root, c_mid, c_tip = self.chords()
        b1, b2 = self.half_widths()
        return 2.0 * (b1 * (c_root + c_mid) / 2.0 + b2 * (c_mid + c_tip) / 2.0)

    def mean_aero_chord_m(self) -> float:
        def trap_mac(cr: float, ct: float) -> float:
            return (2.0 / 3.0) * (cr + ct - cr * ct / (cr + ct))
        c_root, c_mid, c_tip = self.chords()
        b1, b2 = self.half_widths()
        a1 = b1 * (c_root + c_mid) / 2.0
        a2 = b2 * (c_mid + c_tip) / 2.0
        return (a1 * trap_mac(c_root, c_mid) + a2 * trap_mac(c_mid, c_tip)) / (a1 + a2)

    def aspect_ratio(self) -> float:
        return self.span_m ** 2 / self.planform_area_m2()


class GateResult(BaseModel):
    gate: str
    passed: bool
    reason: str = ""


class GeometryReport(BaseModel):
    """Everything the geometry stage asserts and produced."""
    params: BWBParams
    s_ref_m2: float
    mean_aero_chord_m: float
    aspect_ratio: float
    bbox_m: List[float] = Field(description="[xmin,ymin,zmin,xmax,ymax,zmax] of the STL")
    span_stl_m: Optional[float] = Field(
        default=None, description="Measured y-extent of the built STL")
    span_delta_frac: Optional[float] = Field(
        default=None,
        description=("(STL span - requested span) / requested span, recorded on EVERY "
                     "build (S7-I3). The real bias is +0.22% to +1.10% from the "
                     "ROUND_END_CAP tip, and the 5% gate is far too loose to see it. "
                     "Tightening the gate would fail builds that pass today, so the "
                     "delta is accumulated first and the decision taken with data."))
    openvsp_conventions: dict = Field(
        default_factory=dict,
        description=("The OpenVSP conventions this build depended on — twist and sweep "
                     "locations, whether Span is projected, the tip cap (S7-I5). They "
                     "were previously unstated defaults, so a version change would "
                     "have silently altered every geometry."))
    n_facets: int
    watertight: bool
    open_edges: int
    gates: List[GateResult]
    warnings: List[str] = Field(default_factory=list)
    artifacts: dict = Field(default_factory=dict,
                            description="name -> path: vsp3, stl, degen_csv, renders")

    @property
    def all_gates_pass(self) -> bool:
        return all(g.passed for g in self.gates)


class MissionDerived(BaseModel):
    """Physics derived from the mission by the intent stage (democratization layer)."""
    mission: MissionSpec
    atmosphere: AtmosphereState
    mtow_kg: float
    air_density_kgm3: float
    weight_n: float
    wing_loading_kgm2: float
    s_ref_target_m2: float
    required_cl: float
    reynolds_mac: float
    turbulence_model: str = "kOmegaSST"
    y_plus_target: float = 50.0
    # One declaration, in constants.py (S3-F12c). This used to be a second literal
    # 0.08 alongside aero_estimates.STATIC_MARGIN_TARGET: equal, independent, and free
    # to drift into a CG placed for one margin and reported against another.
    static_margin_target: float = STATIC_MARGIN_TARGET
    params: BWBParams
    assumptions: List[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def restore_legacy_atmosphere(cls, data):
        """Load artifacts written before AtmosphereState was serialized."""
        if not isinstance(data, dict) or data.get("atmosphere") is not None:
            return data
        mission = data.get("mission")
        if isinstance(mission, MissionSpec):
            altitude_m = mission.cruise_altitude_m
        elif isinstance(mission, dict):
            altitude_m = mission.get("cruise_altitude_m")
        else:
            altitude_m = None
        if altitude_m is None:
            return data
        restored = dict(data)
        atmosphere = atmosphere_at(float(altitude_m))
        restored["atmosphere"] = atmosphere
        restored["air_density_kgm3"] = atmosphere.density_kgm3
        params = restored.get("params")
        speed = mission.cruise_speed_ms if isinstance(mission, MissionSpec) else mission.get("cruise_speed_ms")
        if params is not None and speed is not None:
            parsed_params = params if isinstance(params, BWBParams) else BWBParams.model_validate(params)
            restored["reynolds_mac"] = (
                float(speed) * parsed_params.mean_aero_chord_m()
                / atmosphere.kinematic_viscosity_m2s
            )
        return restored

    @model_validator(mode="after")
    def validate_atmosphere_consistency(self):
        if not math.isclose(
            self.mission.cruise_altitude_m,
            self.atmosphere.geometric_altitude_m,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise ValueError("atmosphere does not match mission altitude")
        if not math.isclose(
            self.air_density_kgm3,
            self.atmosphere.density_kgm3,
            rel_tol=1e-12,
            abs_tol=0.0,
        ):
            raise ValueError("flat density does not match atmosphere state")
        expected_reynolds = (
            self.mission.cruise_speed_ms * self.params.mean_aero_chord_m()
            / self.atmosphere.kinematic_viscosity_m2s
        )
        if not math.isclose(
            self.reynolds_mac, expected_reynolds, rel_tol=1e-12, abs_tol=0.0
        ):
            raise ValueError("mission Reynolds number is inconsistent with atmosphere")
        return self
