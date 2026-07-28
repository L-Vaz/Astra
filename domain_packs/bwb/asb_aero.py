"""BWB domain pack — AeroSandbox fast aero+stability evaluator (block A).

The cheap inner-loop tier of the MDO: turns a `BWBParams` into an AeroSandbox
`Airplane` built analytically, mirroring `vsp_builder.py` station-for-station
(XSec_0 root / XSec_1 blend / XSec_2 tip), and runs an AeroBuildup analysis
(vortex-lattice + NeuralFoil viscous section drag — no XFLR5/XFOIL) with stability
derivatives. Returns L/D at trim, static-margin inputs (neutral point + CG),
lateral-directional and pitch derivatives.

Pure Python, in-process (py3.12) — no OpenVSP subprocess in the loop. This is a
low-fidelity screening tier: VSPAERO (winners only) and OpenFOAM remain the
higher-fidelity gates it feeds.

`aerosandbox` is imported lazily inside the functions, so importing this module for
its result type (`AsbAeroResult`, consumed by `objective.py`) does not require the
heavy aero stack.
"""
from __future__ import annotations

import math
from typing import List, Optional

from pydantic import BaseModel, Field

from .schema import BWBParams, MissionDerived

# Reference-geometry source of truth: use Astra's analytic planform quantities
# (same s_ref/mac/span the analytic tier and VSPAERO use) so all tiers' coefficients
# are on one reference and directly comparable.


class AsbAeroResult(BaseModel):
    """Fast aero+stability estimate for one BWBParams. Angles in degrees unless a
    `_rad` suffix says otherwise; stability derivatives are per radian."""
    evidence: str = "vlm+neuralfoil"

    # operating point
    velocity_ms: float
    reynolds_mac: float
    cl_target: float
    alpha_trim_deg: float
    trim_in_sweep: bool = Field(description="alpha_trim inside the probed alpha band")

    # forces at the trimmed cruise CL
    cl: float
    cd: float
    cd_induced: float
    cd_profile: float
    l_over_d: float
    cm_at_trim: float = Field(description="residual pitching moment about the CG at trim")

    # slopes / stability derivatives (per radian)
    cl_alpha_per_rad: float
    cm_alpha_per_rad: float
    cn_beta_per_rad: float
    cl_beta_per_rad: float

    # reference geometry / neutral point (absolute x, apex at root LE = 0)
    mac_m: float
    s_ref_m2: float
    b_ref_m: float
    x_np_m: float = Field(description="neutral point aft of root-LE apex")
    x_cg_m: float = Field(description="CG used (cg_frac_mac behind the MAC LE)")
    cg_frac_mac: float

    # spanwise lift — placeholder in v1 (structures dormant); block B fills the real
    # VLM distribution when the beam surrogate lands.
    span_stations_m: List[float] = Field(default_factory=list)
    spanwise_cl: List[float] = Field(default_factory=list)

    warnings: List[str] = Field(default_factory=list)

    @property
    def static_margin(self) -> float:
        return (self.x_np_m - self.x_cg_m) / self.mac_m if self.mac_m > 0 else 0.0


class AsbAeroError(Exception):
    pass


def _f(x) -> float:
    """Coerce an AeroSandbox scalar/0-d-or-1-d array to a plain float."""
    import numpy as np
    return float(np.asarray(x).ravel()[0])


# S1-C: MAX_TC_SNAP_ERROR, _symmetric_naca and tc_snap_error are gone.
#
# They existed because this tier built every station as a symmetric NACA 00xx snapped
# to an integer percent thickness, which introduced up to 12.5% t/c error at the
# thinnest legal section (0.5/4) and — worse — scored a DIFFERENT SHAPE than the
# preview drew. The snap was justified by NeuralFoil's NACA-4 parametrization being
# fast, but NeuralFoil accepts arbitrary coordinates and CST parameters, so the
# justification never held for named sections.
#
# `BWBParams` now carries real section names, so this builds the actual aerofoil.
# No snap, no rounding error, and the fast tier scores the geometry the user chose.


def bwbparams_to_airplane(p: BWBParams):
    """Analytic BWBParams -> AeroSandbox Airplane, mirroring vsp_builder.py.

    Half-model mirrored (symmetric=True). Three stations along the half-span:
    root (apex, y=0), blend (y=b1), tip (y=b1+b2). Leading edge swept per panel;
    z raised by the dihedral; twist interpolated exactly as the OpenVSP builder does
    (blend twist uses centre_body_fraction). Each station carries its OWN named
    section, so thickness is whatever that aerofoil is — no interpolation and no
    snap (S1-C).
    """
    import aerosandbox as asb

    c_root, c_mid, c_tip = p.chords()
    b1, b2 = p.half_widths()
    mid_twist = p.root_twist_deg + (p.tip_twist_deg - p.root_twist_deg) * p.centre_body_fraction

    tan_in = math.tan(math.radians(p.sweep_inner_deg))
    tan_out = math.tan(math.radians(p.sweep_outer_deg))
    tan_dih = math.tan(math.radians(p.dihedral_deg))

    # leading-edge stations, apex at origin
    x0, y0, z0 = 0.0, 0.0, 0.0
    x1, y1, z1 = b1 * tan_in, b1, b1 * tan_dih
    x2, y2, z2 = x1 + b2 * tan_out, b1 + b2, z1 + b2 * tan_dih

    xsecs = [
        asb.WingXSec(xyz_le=[x0, y0, z0], chord=c_root, twist=p.root_twist_deg,
                     airfoil=asb.Airfoil(p.root_airfoil)),
        asb.WingXSec(xyz_le=[x1, y1, z1], chord=c_mid, twist=mid_twist,
                     airfoil=asb.Airfoil(p.mid_airfoil)),
        asb.WingXSec(xyz_le=[x2, y2, z2], chord=c_tip, twist=p.tip_twist_deg,
                     airfoil=asb.Airfoil(p.tip_airfoil)),
    ]
    wing = asb.Wing(name="bwb", symmetric=True, xsecs=xsecs)
    return asb.Airplane(
        name="bwb",
        xyz_ref=[0.0, 0.0, 0.0],  # set to the CG in evaluate()
        s_ref=p.planform_area_m2(),
        c_ref=p.mean_aero_chord_m(),
        b_ref=p.span_m,
        wings=[wing],
    )


def evaluate(
    params: BWBParams,
    derived: MissionDerived,
    cg_frac_mac: float = 0.25,
    alpha_probe_deg: float = 2.0,
    alpha_band_deg: float = 12.0,
) -> AsbAeroResult:
    """Fast aero+stability at the mission cruise CL.

    CG is placed at `cg_frac_mac` behind the MAC leading edge (quarter-MAC by
    default) — a fixed design assumption, so static margin emerges from the swept
    planform and becomes a real optimiser discriminator. Trim is found by the same
    linear CL(alpha) interpolation VSPAERO uses, then re-evaluated at alpha_trim.
    """
    import aerosandbox as asb
    import numpy as np

    warnings: List[str] = []
    airplane = bwbparams_to_airplane(params)
    wing = airplane.wings[0]

    mac_m = params.mean_aero_chord_m()
    # quarter-MAC x from ASB geometry -> MAC leading edge -> CG at cg_frac behind it
    ac_x = _f(np.asarray(wing.aerodynamic_center())[0])
    x_mac_le = ac_x - 0.25 * mac_m
    x_cg = x_mac_le + cg_frac_mac * mac_m
    airplane.xyz_ref = [x_cg, 0.0, 0.0]

    v = derived.mission.cruise_speed_ms
    re_mac = derived.reynolds_mac
    cl_target = derived.required_cl

    atmos = asb.Atmosphere(altitude=derived.mission.cruise_altitude_m)

    def _op(alpha_deg: float):
        return asb.OperatingPoint(atmosphere=atmos, velocity=v, alpha=alpha_deg, beta=0.0)

    # One stability-derivative pass at the probe alpha gives the lift-curve slope and
    # the derivatives (Cma, Cnb, Clb) + neutral point; the linear regime makes these
    # ~constant over the small step to trim. A second, cheap force-only run at trim
    # gives L/D and the residual trim moment. (Two derivative passes would double the
    # finite-differencing cost for no meaningful fidelity gain in this screening tier.)
    r0 = asb.AeroBuildup(airplane=airplane, op_point=_op(alpha_probe_deg)) \
        .run_with_stability_derivatives()
    cl0 = _f(r0["CL"])
    cl_alpha = _f(r0["CLa"])  # per rad
    if not math.isfinite(cl_alpha) or abs(cl_alpha) < 1e-6:
        raise AsbAeroError(f"degenerate CL_alpha={cl_alpha}")

    alpha_trim_deg = alpha_probe_deg + math.degrees((cl_target - cl0) / cl_alpha)
    trim_in_sweep = (alpha_probe_deg - alpha_band_deg) <= alpha_trim_deg <= (alpha_probe_deg + alpha_band_deg)
    if not trim_in_sweep:
        warnings.append(
            f"alpha_trim {alpha_trim_deg:.1f} deg outside +/-{alpha_band_deg:.0f} deg "
            f"probe band — linear extrapolation, treat with caution")

    rt = asb.AeroBuildup(airplane=airplane, op_point=_op(alpha_trim_deg)).run()
    cl = _f(rt["CL"])
    cd = _f(rt["CD"])
    # induced/profile split via drag components (forces -> coefficients on same q*S)
    q = 0.5 * atmos.density() * v ** 2
    s = params.planform_area_m2()
    cd_induced = _f(rt["D_induced"]) / (q * s) if q * s > 0 else 0.0
    cd_profile = _f(rt["D_profile"]) / (q * s) if q * s > 0 else 0.0
    l_over_d = cl / cd if cd > 0 else 0.0
    cm_trim = _f(rt["Cm"])

    # stability + neutral point from the derivative pass (linear regime)
    x_np = _f(r0["x_np"])

    return AsbAeroResult(
        velocity_ms=v, reynolds_mac=re_mac, cl_target=cl_target,
        alpha_trim_deg=alpha_trim_deg, trim_in_sweep=trim_in_sweep,
        cl=cl, cd=cd, cd_induced=cd_induced, cd_profile=cd_profile,
        l_over_d=l_over_d, cm_at_trim=cm_trim,
        cl_alpha_per_rad=cl_alpha,
        cm_alpha_per_rad=_f(r0["Cma"]),
        cn_beta_per_rad=_f(r0["Cnb"]),
        cl_beta_per_rad=_f(r0["Clb"]),
        mac_m=mac_m, s_ref_m2=s, b_ref_m=params.span_m,
        x_np_m=x_np, x_cg_m=x_cg, cg_frac_mac=cg_frac_mac,
        warnings=warnings,
    )
