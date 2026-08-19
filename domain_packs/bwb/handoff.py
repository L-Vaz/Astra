"""The Stage-1 → Stage-2 handoff artifact (S1-I).

WHAT IT IS FOR
--------------
Stage 1 is a concept explorer: it picks a starting aircraft from a mission, using
tier-0 methods that are fast, transparent and in places frankly approximate. Stage 2 is
an MDO with a VLM in the loop. The handoff is the contract between them.

The thing that makes it worth a module rather than a `model_dump()` is the EVIDENCE
BLOCK. Stage 2 will re-derive most of these numbers with a better method, and it needs
to know which of its inherited values are worth arguing with. A CLmax computed from a
measured section table at the right Reynolds and a span efficiency the project has
explicitly declined to trust are both floats on the same object; without a grade
attached they are indistinguishable, and the optimiser will happily anchor on the
second one.

Grades follow the repository protocol:

    [A]  primary source, cited to an equation or section
    [B]  established textbook, cited to edition and page
    [C]  measured in this project, with the reproducing command recorded
    [D]  judgment — NOT a fact; carries a sensitivity and what would falsify it

WHAT STAGE 2 IS EXPECTED TO DO WITH IT
--------------------------------------
Take `geometry` as the starting point, `airfoil_selection` as the exact approved
root/mid/tip coordinate identity, `locks` as constraints it may not violate,
`requirements` as the objectives, and treat `performance` as a prior to be replaced —
not as truth. Anything graded `[D]`, and the three quantities flagged in
`known_disagreements`, should be re-derived before they influence a decision. The
approved sections may be re-evaluated, but must not be silently replaced by generic
thickness-scaled shapes.

FORMAT
------
A pydantic model, serialised as JSON. Deliberately not a file format with its own
parser: Stage 2 does not exist yet, and inventing a wire protocol for a consumer whose
shape is unknown is how a schema ends up fitting nobody. `SCHEMA_VERSION` is here so
that when Stage 2 does exist, it can tell what it is reading.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .aero_estimates import AeroEstimate
from .airfoils import provenance as airfoil_provenance, table_sha256
from .airfoil_coordinates import archive_sha256 as coordinate_archive_sha256
from .atmosphere import atmosphere_at, atmosphere_provenance
from .inverse import CapViolation, LockedValues
from .requirements import DesignRequirements, RequirementStatus
from .schema import BWBParams

SCHEMA_VERSION = "1.0.0"


class Evidence(BaseModel):
    """One inherited number, and how far to trust it."""
    quantity: str
    value: Optional[float] = None
    grade: str = Field(description="A | B | C | D")
    source: str
    caveat: str = ""


class Stage1Handoff(BaseModel):
    """Everything Stage 2 needs, plus everything it needs to distrust."""
    schema_version: str = SCHEMA_VERSION

    geometry: BWBParams
    flight_condition: Dict[str, Any] = Field(
        description="altitude_m, cruise_speed_ms, weight_n, launch — the point every "
                    "performance number below was evaluated at")

    requirements: DesignRequirements
    requirement_status: List[RequirementStatus] = Field(
        default_factory=list,
        description="Which targets the delivered geometry actually meets. An unmet "
                    "requirement is an objective for Stage 2, not a defect to hide.")

    locks: LockedValues = Field(
        default_factory=LockedValues,
        description="HARD constraints. Stage 2 may not move these; if its search "
                    "cannot satisfy them it must report that, exactly as Stage 1 does.")
    lock_violations: List[CapViolation] = Field(
        default_factory=list,
        description="Locks Stage 1 could not honour. A non-empty list means the "
                    "operator asked for something this planform family cannot deliver "
                    "— Stage 2 inherits the conflict, not a resolution of it.")

    performance: Dict[str, Any] = Field(
        description="The tier-0 panel. A PRIOR for Stage 2, not a target to match.")
    airfoil_selection: Dict[str, Any] = Field(
        default_factory=dict,
        description="The approved root/mid/tip file-airfoil identities and immutable "
                    "selection hashes. Empty only for legacy handoffs.")

    evidence: List[Evidence] = Field(default_factory=list)
    known_disagreements: List[str] = Field(
        default_factory=list,
        description="Places where two models in this pack give different answers and "
                    "Stage 1 declined to pick. Each is a decision waiting for better "
                    "evidence, and each is a trap for an optimiser that anchors on "
                    "whichever number it happens to read.")
    provenance: Dict[str, Any] = Field(default_factory=dict)


def _evidence(p: BWBParams, panel: AeroEstimate) -> List[Evidence]:
    """Grade every number Stage 2 inherits. Ungraded quantities are not exported."""
    return [
        Evidence(quantity="cl_max_3d", value=panel.cl_max_3d, grade="A",
                 source="Schrenk spanwise load, NACA TM-948 pp. 2-3, over the "
                        "committed NeuralFoil section screen; solved self-consistently "
                        "at the stall Reynolds",
                 caveat="TM-948 has NO sweep term. The cos(Lc/4) factor is a labelled "
                        "patch applied on top and is the weakest link in this value. "
                        "Section CLmax is interpolated between the three stations as a "
                        "VALUE, which the paper does not cover."),
        Evidence(quantity="stall_station_frac", value=panel.stall_station_frac,
                 grade="A", source="same solve — the station where local demand first "
                                   "reaches local capability",
                 caveat="The OUTBOARD flag threshold (0.60 semi-span) is [D] judgment; "
                        "the station itself is computed."),
        Evidence(quantity="v_stall_ms", value=panel.v_stall_ms, grade="A",
                 source="2W/(rho S CLmax), with CLmax as above",
                 caveat="Inherits every caveat on cl_max_3d."),
        Evidence(quantity="cm_trim_residual", value=panel.cm_trim_residual, grade="A",
                 source="strip moment about the CG: section Cm0 at each quarter chord "
                        "plus local lift through the swept arm, on the Schrenk loading",
                 caveat="The moment ARM is exact; the LOAD it multiplies is Schrenk's "
                        "straight-wing approximation on a swept planform."),
        Evidence(quantity="effective_dihedral_deg", value=panel.effective_dihedral_deg,
                 grade="B",
                 source="McCormick, Aerodynamics/Aeronautics/Flight Mechanics, Wiley "
                        "1979, p. 547, via Kuhlman, RC Soaring Digest Jan-Mar 2000",
                 caveat="Scales with CL, so it is a different number at every speed — "
                        "8.95 deg at CLmax against 5.08 at cruise on the reference "
                        "wing. Excludes winglets, fuselage and fin, of which this "
                        "configuration has none."),
        Evidence(quantity="cd_profile_sections", value=panel.cd_profile_sections,
                 grade="C",
                 source="strip integral of the committed NeuralFoil CD ladder at each "
                        "station's local cl and chord Reynolds",
                 caveat="DISAGREES with cd0 by a factor of ~1.37. Not used in the "
                        "polar. The table's own median interpolation error is 5.04%."),
        Evidence(quantity="span_efficiency_planar", value=panel.span_efficiency_planar,
                 grade="B",
                 source="Prandtl lifting line, delta = sum n(An/A1)^2, e = 1/(1+delta); "
                        "framework from Stengel MAE331 L5 slides 6, 8",
                 caveat="INVISCID and PLANAR. Not the Oswald e used in cd_cruise, and "
                        "roughly 0.16 higher. Loses the sweep sensitivity the Raymer "
                        "fit has, because lifting line has no sweep term."),
        Evidence(quantity="cd0", value=panel.cd0, grade="D",
                 source="Raymer wetted-area buildup: flat-plate Cf x Hoerner form "
                        "factor x wetted-area fit x a lumped 1.10 interference margin",
                 caveat="Blind to the aerofoil except through t/c, and 37% above the "
                        "measured section sum. The 1.10 is unallocated judgment. This "
                        "is the number that drives cd_cruise and therefore every L/D."),
        Evidence(quantity="oswald_e", value=panel.oswald_e, grade="D",
                 source="Raymer straight/swept fits, blended 0-30 deg LE sweep",
                 caveat="Non-monotonic in sweep for AR 2.50-4.79 (adding sweep reports "
                        "BETTER e, the wrong sign), exceeds 1.0 unclamped, and 20.7% "
                        "of the AR x sweep grid sits on a clamp with zero gradient. "
                        "Do not finite-difference it."),
        Evidence(quantity="x_np_m", value=panel.x_np_m, grade="D",
                 source="area-weighted planform centroid of the two trapezoids",
                 caveat="Ignores the higher lift-per-area of the outer panels, which "
                        "biases x_np FORWARD and the static margin OPTIMISTIC. "
                        "Unconservative on the one number that is safety-critical for "
                        "a tailless aircraft. Replace with the VLM value immediately."),
        Evidence(quantity="root_bending_index_nm", value=panel.root_bending_index_nm,
                 grade="D", source="W*b/8, a comparative index",
                 caveat="This is the UNIFORM-load result, ~18% above the elliptic one. "
                        "Not a structural sizing and not a load case."),
    ]


KNOWN_DISAGREEMENTS = [
    "Profile drag: the wetted-area correlation reads ~1.37x the strip integral of the "
    "measured section data (0.01360 against 0.00990 at zero lift on the reference "
    "wing). Stage 1 kept the correlation in the polar and reports both. Resolving it "
    "needs a CFD or tunnel point, not another correlation.",
    "Span efficiency: the inviscid lifting-line value (0.978) sits ~0.16 above the "
    "Raymer Oswald fit (0.816). They are different quantities — Oswald e carries "
    "viscous lift-dependent drag under an inviscid name — and combining either with "
    "the wrong drag term double-counts. Stage 1 reports both and uses the fit.",
    "Trim and static margin are two demands on one CG. On the reference wing the CG "
    "that trims at cruise leaves only 0.58% static margin against an 8% target, and no "
    "washout inside the schema range trims it at the 8% CG. Stage 2 must treat CG "
    "position as a design variable, not a derived constant.",
]


def build_handoff(params: BWBParams, panel: AeroEstimate,
                  requirements: DesignRequirements,
                  requirement_status: Optional[List[RequirementStatus]] = None,
                  locked: Optional[LockedValues] = None,
                  lock_violations: Optional[List[CapViolation]] = None,
                  altitude_m: float = 0.0,
                  airfoil_selection: Optional[Dict[str, Any]] = None) -> Stage1Handoff:
    """Assemble the artifact from one solved concept."""
    return Stage1Handoff(
        geometry=params,
        flight_condition={
            "altitude_m": altitude_m,
            "cruise_speed_ms": panel.v_ms,
            "weight_n": panel.weight_n,
            "launch": requirements.launch,
            "reynolds_mac": panel.reynolds_mac,
        },
        requirements=requirements,
        requirement_status=list(requirement_status or []),
        locks=locked or LockedValues(),
        lock_violations=list(lock_violations or []),
        performance=panel.model_dump(),
        airfoil_selection=(
            {
                "version": airfoil_selection["version"],
                "selection_hash": airfoil_selection["selection_hash"],
                "schedule_hash": airfoil_selection["schedule_hash"],
                "sections": [
                    {
                        "station": station,
                        "name": airfoil_selection["schedule"][station],
                        "sha256": airfoil_selection["coordinate_hashes"][station],
                        "openvsp_type": "XS_FILE_AIRFOIL",
                    }
                    for station in ("root", "mid", "tip")
                ],
            }
            if airfoil_selection else {}
        ),
        evidence=_evidence(params, panel),
        known_disagreements=list(KNOWN_DISAGREEMENTS),
        provenance={
            "airfoil_table_sha256": table_sha256(),
            "airfoil_coordinate_archive_sha256": coordinate_archive_sha256(),
            "airfoil_table": {
                k: airfoil_provenance().get(k)
                for k in ("sections", "re_grid", "cl_ladder", "neuralfoil",
                          "aerosandbox", "cl_ladder_interpolation_error_pct")
            },
            "atmosphere": atmosphere_provenance(atmosphere_at(altitude_m)),
            "methods": {
                "spanwise_load": "Schrenk, NACA TM-948 (1940)",
                "trim": "strip moment about the CG, quarter-chord section AC",
                "span_efficiency": "Prandtl lifting line (reported, not in the polar)",
                "profile_drag": "NeuralFoil section CD ladder (reported, not in the polar)",
                "effective_dihedral": "McCormick 1979 p. 547 via Kuhlman RCSD 2000",
                "parasite_drag": "Raymer wetted-area buildup (IN the polar)",
                "oswald_e": "Raymer straight/swept fits (IN the polar)",
            },
        },
    )
