"""BWB domain pack — parameter and mission schemas.

Shared by the FoamAgent conda env (py3.12) and the OpenVSP venv (py3.10):
pydantic-only, no OpenVSP imports here.
"""
import math
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from .airfoils import has_section, nearest_by_tc, tc_of
from .atmosphere import AtmosphereState, atmosphere_at
from .constants import STATIC_MARGIN_TARGET

# Default sections. [D] — chosen from the screen, not from a source.
#
# Picked to (a) sit closest to the thicknesses the pack used before S1-C (root 14%,
# tip 9%) so the geometry shift is small and attributable, (b) be inside the trim
# band, and (c) carry high model confidence where possible. Measured at Re 4e5:
#
#   e474    t/c 14.1%  Cm0 +0.0000  CLmax 1.292  confidence 0.97
#   e472    t/c 12.1%  Cm0 -0.0000  CLmax 1.329  confidence 0.97
#   s5010   t/c  9.8%  Cm0 -0.0010  CLmax 1.297  confidence 0.27   cl0 +0.120
#
# The root and blend are symmetric Eppler sections — a thick, near-neutral centre
# body is what a BWB actually wants, and thick REFLEXED sections are rare. The tip
# is a Selig flying-wing section, which unlike the symmetric ones still makes lift
# at zero incidence (cl0 +0.120). Its confidence of 0.27 is the weakest link here:
# NeuralFoil has seen few shapes like it, so its numbers are the least trustworthy
# in the default aircraft. Falsified if the trim solve (S1-D) shows this combination
# cannot trim within the washout the schema allows.
DEFAULT_ROOT_AIRFOIL = "e474"
DEFAULT_MID_AIRFOIL = "e472"
DEFAULT_TIP_AIRFOIL = "s5010"


class MissionSpec(BaseModel):
    """What the user wants the drone to do. Parsed from natural language."""
    payload_kg: float = Field(gt=0, le=10, description="Payload mass in kg")
    cruise_speed_ms: float = Field(ge=8, le=60, description="Cruise speed in m/s")
    cruise_altitude_m: float = Field(ge=0, le=4000, description="Cruise altitude AMSL in m")
    mtow_kg: Optional[float] = Field(default=None, gt=0, le=25,
                                     description="Max takeoff mass in kg; estimated from payload if absent")
    launch: Literal["hand", "catapult", "runway"] = "hand"

    # S1-A: `endurance_min` and `range_km` were removed. They were recorded-only goals
    # that no equation read, and a requirement nothing consumes reads as a promise the
    # tool does not keep. Endurance is a propulsion/energy quantity and Stage 1 is
    # aerodynamics-only. No `extra="forbid"` here on purpose: pydantic's default is to
    # ignore unknown keys, which is what lets `mission_derived.json` files written
    # before this change still load. The CL^1.5/CD figure of merit survives as
    # AeroEstimate.power_fom (renamed from endurance_fom) — it compares shapes, not times.

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
    the constructor's guarantees have to survive assignment.

    S1-C: THE SECTION OWNS THICKNESS. `root_thickness_pct` and `tip_thickness_pct`
    used to be free inputs dialled independently of the aerofoil, which was
    incoherent — thickness is a property of a shape, not a knob beside it. They are
    now COMPUTED from the chosen sections and serialised with the model, so a card
    can always show how much depth the choice actually gives you.

    `extra="forbid"` so a construction still passing `root_thickness_pct=14` fails
    loudly. Pydantic's default would ignore it and silently build the DEFAULT
    aerofoil instead — a wrong aircraft with no error."""

    model_config = ConfigDict(validate_assignment=True, extra="forbid")
    span_m: float = Field(ge=0.8, le=3.0, description="Total span, tip to tip")
    root_chord_m: float = Field(ge=0.25, le=1.2, description="Centre-body root chord")
    centre_body_fraction: float = Field(ge=0.12, le=0.45,
                                        description="Centre-body width / span")
    mid_chord_frac: float = Field(ge=0.35, le=0.85,
                                  description="Chord at centre-body/wing blend, as fraction of root chord")
    taper_outer: float = Field(ge=0.15, le=0.7, description="Tip chord / blend chord")
    sweep_inner_deg: float = Field(ge=15, le=60, description="Centre-body LE sweep")
    sweep_outer_deg: float = Field(ge=5, le=40, description="Outer panel LE sweep")
    # The three aerofoil stations. Each fixes the thickness at that station; see
    # DEFAULT_ROOT_AIRFOIL below for why these three are the defaults.
    root_airfoil: str = Field(default=DEFAULT_ROOT_AIRFOIL,
                              description="Centre-body section")
    mid_airfoil: str = Field(default=DEFAULT_MID_AIRFOIL,
                             description="Section at the centre-body/wing blend")
    tip_airfoil: str = Field(default=DEFAULT_TIP_AIRFOIL, description="Tip section")
    root_twist_deg: float = Field(ge=-2, le=5, description="Root incidence")
    tip_twist_deg: float = Field(ge=-7, le=2, description="Tip twist (negative = washout)")
    dihedral_deg: float = Field(ge=0, le=8)

    # -- thickness, owned by the section (S1-C) --------------------------------
    # `computed_field` rather than a plain property so these appear in model_dump()
    # and reach the cockpit card. The maintainer's requirement was that choosing a
    # section must COMMUNICATE the depth it gives you, which it cannot do if the
    # number never leaves the model.
    @computed_field(description="Root t/c %, from the root section")
    @property
    def root_thickness_pct(self) -> float:
        return round(tc_of(self.root_airfoil) * 100.0, 4)

    @computed_field(description="Blend-station t/c %, from the mid section")
    @property
    def mid_thickness_pct(self) -> float:
        return round(tc_of(self.mid_airfoil) * 100.0, 4)

    @computed_field(description="Tip t/c %, from the tip section")
    @property
    def tip_thickness_pct(self) -> float:
        return round(tc_of(self.tip_airfoil) * 100.0, 4)

    @model_validator(mode="before")
    @classmethod
    def _absorb_computed_thickness(cls, data):
        """Let a round-trip of `model_dump()` back through `model_validate()` work.

        `computed_field` puts the three thickness values INTO model_dump, and
        `extra="forbid"` would then reject them coming back — breaking every
        store-and-reload path in the cockpit and the candidate store.

        They are dropped only when they AGREE with the sections, which is what a
        genuine round-trip looks like. A stale caller passing a thickness that no
        section supports still fails loudly, which was the point of forbidding
        extras: silently ignoring `root_thickness_pct=14` would build the DEFAULT
        aerofoil and report a wrong aircraft with no error."""
        if not isinstance(data, dict):
            return data
        pairs = (("root_thickness_pct", "root_airfoil", DEFAULT_ROOT_AIRFOIL),
                 ("mid_thickness_pct", "mid_airfoil", DEFAULT_MID_AIRFOIL),
                 ("tip_thickness_pct", "tip_airfoil", DEFAULT_TIP_AIRFOIL))
        if not any(k in data for k, _, _ in pairs):
            return data
        data = dict(data)

        # A file written BEFORE S1-C has thicknesses and no sections at all. That
        # aircraft is not reproducible — no section is exactly 14.00% t/c — so the
        # nearest one is resolved instead. Without this, every session stored before
        # this change 500s on load, which is how it was found: the suites were green
        # and the running cockpit was not.
        legacy = (not any(sk in data for _, sk, _ in pairs)
                  and any(k in data for k, _, _ in pairs))
        if legacy:
            for key, section_key, _ in pairs:
                if data.get(key) is not None:
                    data[section_key] = nearest_by_tc(float(data[key]) / 100.0)
            if "mid_airfoil" not in data:
                # Pre-S1-C geometry had no blend section; it used the root/tip mean.
                r = data.get("root_thickness_pct")
                t = data.get("tip_thickness_pct")
                if r is not None and t is not None:
                    data["mid_airfoil"] = nearest_by_tc((float(r) + float(t)) / 200.0)

        for key, section_key, fallback in pairs:
            if key not in data:
                continue
            supplied = data.pop(key)
            name = data.get(section_key, fallback)
            if not has_section(name):
                continue                       # the section check will report it
            actual = round(tc_of(name) * 100.0, 4)
            if legacy:
                continue                       # nearest-match, never exact by design
            if supplied is not None and abs(float(supplied) - actual) > 1e-3:
                raise ValueError(
                    f"{key}={supplied} does not match {section_key}={name!r}, which "
                    f"is {actual:.2f}% t/c. Thickness is set BY the section (S1-C) — "
                    f"choose a different aerofoil rather than passing a thickness.")
        return data

    @model_validator(mode="after")
    def cross_checks(self) -> "BWBParams":
        tip_chord = self.root_chord_m * self.mid_chord_frac * self.taper_outer
        if tip_chord < 0.04:
            raise ValueError(
                f"tip chord {tip_chord:.3f} m below 0.04 m manufacturable minimum")
        for field in ("root_airfoil", "mid_airfoil", "tip_airfoil"):
            name = getattr(self, field)
            if not has_section(name):
                raise ValueError(
                    f"{field}={name!r} is not in the screened section library")
        if self.tip_thickness_pct > self.root_thickness_pct:
            raise ValueError(
                f"tip section {self.tip_airfoil!r} ({self.tip_thickness_pct:.1f}% t/c) "
                f"is thicker than root section {self.root_airfoil!r} "
                f"({self.root_thickness_pct:.1f}%) — a BWB thins outboard")
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
    airfoil_sections: List[dict] = Field(
        default_factory=list,
        description="Exact root/mid/tip coordinate identities loaded into OpenVSP",
    )
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
