"""BWB domain pack — Stage-1 requirements intake (three-tier list).

The intake is the single editable artifact of Stage 1: a freeform brief is
parsed into requirement ROWS across three tiers, and everything downstream
(feasibility, concept generation) reads only this list.

  mission   the user's own words (default FIXED)
  vehicle   the hard box: caps and build constraints (FIXED but relaxable)
  derived   computed by physics — never editable; each row names the
            editable rows that DRIVE it, with the direction of effect

Row semantics:
  status  fixed | variable | derived    (lock state; derived rows have none)
  origin  you_said | assumed | edited | derived
          "assumed" rows cite their source and are the ones the user should
          eyeball; editing one flips it to "edited" (visually: yours).

Deterministic only — the regex brief parser is a demo-grade stand-in for the
LLM parse that chat performs via MCP (services/intent.parse_mission). It never
invents values it did not match; unmatched fields keep their cited defaults.

Pydantic-only; safe for both the FoamAgent env (py3.12) and OpenVSP venv (py3.10).
"""
import math
import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from .aero_estimates import LAUNCH_VSTALL_MAX
from .atmosphere import atmosphere_at
from .defaults import PAYLOAD_FRACTION
from .inverse import BINDING_LABELS, solve_requirements
from .requirements import DesignRequirements

Tier = Literal["mission", "vehicle", "derived"]
Status = Literal["fixed", "variable", "derived"]
Origin = Literal["you_said", "assumed", "edited", "derived"]


class Driver(BaseModel):
    """One editable row that moves a derived quantity, with the direction."""
    row_id: str
    effect: str            # e.g. "lower stall speed -> bigger wing -> lower W/S"


class IntakeRow(BaseModel):
    id: str
    tier: Tier
    label: str
    value: str             # display string, exactly as typed/derived
    number: Optional[float] = None   # SI value when the row is numeric
    unit: str = ""
    status: Status = "variable"
    origin: Origin = "assumed"
    source: str = ""       # citation for assumed defaults / derived values
    info: str = ""         # definition + formula for the info panel
    drivers: List[Driver] = Field(default_factory=list)  # derived rows only
    custom: bool = False   # user-added row (recorded; mapped when recognised)


class Feasibility(BaseModel):
    verdict: Literal["feasible", "binding", "error"]
    headline: str
    detail: str
    implied: Dict[str, str] = Field(default_factory=dict)
    binding_row_ids: List[str] = Field(default_factory=list)


class Intake(BaseModel):
    brief: str = ""
    rows: List[IntakeRow]
    feasibility: Optional[Feasibility] = None
    notes: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# info texts — definition + formula per row, mirrored by the UI's i-panels
# ---------------------------------------------------------------------------

_INFO = {
    "mission_type": "Sets every default downstream: surveillance favours endurance "
                    "and stability; racing favours speed.",
    "payload": "Everything carried. Mass adds to MTOW; power draw shrinks endurance. "
               "Each item needs mass + watts.",
    "altitude": "Cruise height. Sets air density rho (ISA) used in every lift and "
                "drag equation.",
    "endurance": "Time aloft — recorded goal only for now: propulsion and energy "
                 "modelling are outside the aero-only scope, so this never resizes "
                 "the wing. It is kept on the record for the propulsion stage.",
    "launch": "Hand launch caps stall speed near "
              f"{LAUNCH_VSTALL_MAX['hand']} m/s — you must throw it flying. "
              "Catapult or runway relaxes the cap.",
    "regulatory": "Legal mass and visibility class. Under 2 kg VLOS keeps paperwork "
                  "minimal in most regions. Recorded — not enforced by the solver.",
    "span_max": "Tip-to-tip cap b, from transport or print-bed size. With area S "
                "fixed it caps aspect ratio: AR = b^2/S, which caps L/D.",
    "v_stall_max": "Slowest level flight: Vs = sqrt(2mg / rho S CLmax). The launch "
                   "method also caps this; the tighter of the two wins. Floors the "
                   "wing area.",
    "mtow_cap": "Max takeoff mass: structure + battery + payload + avionics. The "
                "hard box for the sizing loop.",
    "structure": "Material and build method. 3D-printed LW-PLA is denser than EPP "
                 "foam — raises the empty-mass fraction. Recorded — structures "
                 "sizing is a later stage.",
    "airfoil": "Section family. Tailless BWBs need reflexed/neutral-moment sections; "
               "thickness runs root 14% to tip 9%. Sets CLmax and cruise drag. "
               "Recorded — section choice is applied at geometry.",
    "wind": "Gust level to shrug off. Raises the weight on lateral stability in "
            "concept scoring. Recorded for the objective builder.",
    "wing_loading": "W/S — mass per wing area. Sizes the wing via S = mg/(q CL). "
                    "Lower = floatier, slower stall; higher = faster, gust-punchy.",
    "s_ref": "Wing reference area from the sizing solve: S = W/(q CL) with the "
             "stall floor 2W/(rho Vs^2 CLmax) applied.",
    "cruise_speed": "Best-L/D cruise: V = sqrt(2W / rho S CL*), where CL* = "
                    "sqrt(pi e AR CD0) is the CL of maximum L/D. An outcome of "
                    "sizing, not an input.",
    "cruise_cl": "Lift coefficient the wing must fly at in cruise: CL = W/(q S). "
                 "Kept below 0.75 to stay clear of stall.",
    "clmax": "Peak 3D lift coefficient before stall — a property of the sections "
             "and planform at your Reynolds number. Enters the stall equation.",
    "static_margin": "(x_np - x_cg)/MAC. Positive = nose settles back after a gust. "
                     "Tailless BWBs target ~8% MAC (needs washout + swept tips).",
    "ld_est": "Equation-level cruise L/D of the sized baseline planform. VSPAERO "
              "and CFD supersede this number.",
}

_DERIVED_DRIVERS = {
    "wing_loading": [
        Driver(row_id="mtow_cap", effect="lower MTOW cap → lower W/S"),
        Driver(row_id="v_stall_max", effect="lower stall speed → bigger wing → lower W/S"),
        Driver(row_id="launch", effect="hand launch caps stall → floors wing area"),
        Driver(row_id="payload", effect="less payload → less mass"),
    ],
    "s_ref": [
        Driver(row_id="v_stall_max", effect="lower stall speed → larger area"),
        Driver(row_id="mtow_cap", effect="more mass → larger area"),
        Driver(row_id="span_max", effect="span cap limits how the area is spent"),
    ],
    "cruise_speed": [
        Driver(row_id="mtow_cap", effect="heavier → faster best-L/D speed"),
        Driver(row_id="v_stall_max", effect="bigger wing → slower best-L/D speed"),
        Driver(row_id="altitude", effect="thinner air → faster"),
    ],
    "cruise_cl": [
        Driver(row_id="v_stall_max", effect="bigger wing → lower cruise CL"),
        Driver(row_id="mtow_cap", effect="heavier → higher cruise CL"),
    ],
    "clmax": [
        Driver(row_id="airfoil", effect="higher-lift sections → higher CLmax"),
        Driver(row_id="altitude", effect="denser air → higher Reynolds → better CLmax"),
    ],
    "static_margin": [
        Driver(row_id="wind", effect="gustier → larger margin"),
        Driver(row_id="mission_type", effect="aerobatic → smaller, surveillance → larger"),
    ],
    "ld_est": [
        Driver(row_id="span_max", effect="more span → higher AR → higher L/D"),
        Driver(row_id="v_stall_max", effect="tight stall cap → area floor → lower L/D"),
    ],
}


# ---------------------------------------------------------------------------
# defaults — a complete list before any brief is parsed
# ---------------------------------------------------------------------------

def default_intake() -> Intake:
    rows = [
        IntakeRow(id="mission_type", tier="mission", label="Mission type",
                  value="General purpose UAV", origin="assumed",
                  source="no mission stated", info=_INFO["mission_type"]),
        IntakeRow(id="payload", tier="mission", label="Payload", value="1.0 kg",
                  number=1.0, unit="kg", origin="assumed",
                  source="small-UAV typical", info=_INFO["payload"]),
        IntakeRow(id="altitude", tier="mission", label="Operating altitude",
                  value="100 m AMSL", number=100.0, unit="m", origin="assumed",
                  source="small-UAV typical", info=_INFO["altitude"]),
        IntakeRow(id="endurance", tier="mission", label="Endurance goal",
                  value="not set", origin="assumed",
                  source="recorded only — aero scope", info=_INFO["endurance"]),
        IntakeRow(id="launch", tier="mission", label="Launch and recovery",
                  value="hand", origin="assumed", source="most common for this size",
                  info=_INFO["launch"]),
        IntakeRow(id="regulatory", tier="mission", label="Regulatory class",
                  value="< 25 kg · VLOS", origin="assumed",
                  source="schema cap", info=_INFO["regulatory"]),
        IntakeRow(id="span_max", tier="vehicle", label="Max span", value="3.0 m",
                  number=3.0, unit="m", origin="assumed",
                  source="schema cap", info=_INFO["span_max"]),
        IntakeRow(id="v_stall_max", tier="vehicle", label="Stall speed",
                  value=f"≤ {LAUNCH_VSTALL_MAX['hand']} m/s",
                  number=LAUNCH_VSTALL_MAX["hand"], unit="m/s", origin="assumed",
                  source="hand launch (Sadraey)", info=_INFO["v_stall_max"]),
        IntakeRow(id="mtow_cap", tier="vehicle", label="MTOW cap", value="3.3 kg",
                  number=round(1.0 / PAYLOAD_FRACTION, 1), unit="kg",
                  origin="assumed",
                  source=f"payload / {PAYLOAD_FRACTION:.0%} payload fraction",
                  info=_INFO["mtow_cap"]),
        IntakeRow(id="structure", tier="vehicle", label="Structure",
                  value="3D printed LW-PLA", origin="assumed",
                  source="your workshop", info=_INFO["structure"]),
        IntakeRow(id="airfoil", tier="vehicle", label="Sections",
                  value="BWB blend, t/c 14% → 9%", origin="assumed",
                  source="tailless typical", info=_INFO["airfoil"]),
        IntakeRow(id="wind", tier="vehicle", label="Wind tolerance",
                  value="≤ 8 m/s gusts", origin="assumed",
                  source="open-field typical", info=_INFO["wind"]),
    ]
    return Intake(brief="", rows=rows)


# ---------------------------------------------------------------------------
# brief parser — deterministic, never invents what it did not match
# ---------------------------------------------------------------------------

_NUM = r"(\d+(?:\.\d+)?)"

_MISSION_WORDS = [
    (r"photo|film|camera work|cinemat", "Aerial photography"),
    (r"surveil|supervis|monitor|inspect|patrol", "Surveillance"),
    (r"map|survey", "Mapping / survey"),
    (r"rac|speed run|fast", "Racing / speed"),
    (r"cargo|deliver|drop", "Cargo / delivery"),
    (r"farm|agri|crop", "Agricultural monitoring"),
]


def _num(text: str) -> Optional[float]:
    m = re.search(_NUM, text.replace(",", "."))
    return float(m.group(1)) if m else None


def _set(row: IntakeRow, value: str, number: Optional[float] = None) -> None:
    row.value = value
    row.number = number
    row.origin = "you_said"
    row.status = "fixed"
    row.source = "your brief"


def parse_brief(text: str) -> Intake:
    """Parse a freeform brief into the tiered list. Matched fields become
    you_said + fixed; everything else keeps its cited assumed default."""
    intake = default_intake()
    rows = {r.id: r for r in intake.rows}
    t = " " + text.lower().replace("\n", " ") + " "

    for pat, label in _MISSION_WORDS:
        if re.search(pat, t):
            _set(rows["mission_type"], label)
            break

    m = re.search(rf"span[^.\d]{{0,20}}{_NUM}\s*(m|cm|mm)\b", t)
    if m:
        v = float(m.group(1)) * {"m": 1, "cm": 0.01, "mm": 0.001}[m.group(2)]
        _set(rows["span_max"], f"≤ {v:g} m", v)

    m = (re.search(rf"(?:at|altitude[^.\d]{{0,12}}|fly[^.\d]{{0,12}})\s*{_NUM}\s*m\b", t)
         or re.search(rf"{_NUM}\s*m\s*(?:agl|amsl|altitude)", t))
    if m:
        v = float(m.group(1))
        if 0 <= v <= 4000 and abs(v - (rows["span_max"].number or -1)) > 1e-9:
            _set(rows["altitude"], f"{v:g} m AMSL", v)

    m = (re.search(rf"{_NUM}\s*(g|kg)\b[^.]{{0,30}}(?:camera|payload|sensor|gimbal)", t)
         or re.search(rf"(?:carry|payload|camera)[^.\d]{{0,25}}{_NUM}\s*(g|kg)\b", t))
    if m:
        v = float(m.group(1)) * (0.001 if m.group(2) == "g" else 1.0)
        _set(rows["payload"], f"{v:g} kg", v)

    m = re.search(rf"{_NUM}\s*(min|minutes|h|hour|hours)\b", t)
    if m:
        v = float(m.group(1)) * (60.0 if m.group(2).startswith("h") else 1.0)
        _set(rows["endurance"], f"≥ {v:g} min", v)
        rows["endurance"].source = "your brief — recorded only (aero scope)"

    m = re.search(rf"(?:mtow|takeoff|take-off|total)[^.\d]{{0,20}}{_NUM}\s*kg\b", t)
    if m:
        v = float(m.group(1))
        _set(rows["mtow_cap"], f"≤ {v:g} kg", v)

    for kw, val in (("catapult", "catapult"), ("bungee", "catapult"),
                    ("runway", "runway"), ("wheel", "runway"),
                    ("hand", "hand"), ("throw", "hand")):
        if kw in t:
            _set(rows["launch"], val)
            cap = LAUNCH_VSTALL_MAX[val]
            r = rows["v_stall_max"]
            if r.origin == "assumed":
                r.value, r.number = f"≤ {cap} m/s", cap
                r.source = f"{val} launch (Sadraey)"
            break

    m = re.search(rf"stall[^.\d]{{0,20}}{_NUM}\s*(?:m/s|ms)", t)
    if m:
        _set(rows["v_stall_max"], f"≤ {float(m.group(1)):g} m/s", float(m.group(1)))

    intake.brief = text
    return intake


# ---------------------------------------------------------------------------
# edits — apply a client payload back onto a validated Intake
# ---------------------------------------------------------------------------

def apply_edits(current: Intake, edits: List[Dict[str, Any]]) -> Intake:
    """Merge client row edits. Value changes on assumed rows flip origin to
    'edited'. Derived rows never accept edits. Unknown ids become custom
    vehicle rows (recorded; the solver maps recognised ids only)."""
    rows = {r.id: r for r in current.rows}
    for e in edits:
        rid = str(e.get("id", ""))
        if rid in rows:
            row = rows[rid]
            if row.status == "derived":
                continue
            if "value" in e and str(e["value"]) != row.value:
                row.value = str(e["value"])
                row.number = _num(row.value)
                if row.origin == "assumed":
                    row.origin = "edited"
                    row.source = "your edit"
            if e.get("status") in ("fixed", "variable"):
                row.status = e["status"]
        elif rid and e.get("label"):
            current.rows.append(IntakeRow(
                id=rid, tier="vehicle", label=str(e["label"]),
                value=str(e.get("value", "")), number=_num(str(e.get("value", ""))),
                status="fixed" if e.get("status") != "variable" else "variable",
                origin="edited", source="your addition", custom=True,
                info=_INFO.get(rid, "Custom requirement — recorded and passed to "
                                    "Stage 2 as a constraint when recognised.")))
    return current


# ---------------------------------------------------------------------------
# derive — compute the derived tier + feasibility from the editable tiers
# ---------------------------------------------------------------------------

def _row(intake: Intake, rid: str) -> Optional[IntakeRow]:
    return next((r for r in intake.rows if r.id == rid), None)


def to_requirements(intake: Intake) -> DesignRequirements:
    """Map editable rows onto DesignRequirements. Cruise speed starts at a
    neutral guess — derive() then iterates it to the best-L/D speed."""
    payload = (_row(intake, "payload") or IntakeRow(
        id="payload", tier="mission", label="", value="")).number or 1.0
    mtow_row = _row(intake, "mtow_cap")
    mtow = (mtow_row.number if mtow_row and mtow_row.number
            else payload / PAYLOAD_FRACTION)
    mtow = min(max(mtow, payload), 25.0)
    alt_row = _row(intake, "altitude")
    stall_row = _row(intake, "v_stall_max")
    span_row = _row(intake, "span_max")
    launch_row = _row(intake, "launch")
    launch = (launch_row.value.strip().lower() if launch_row else "hand")
    if launch not in ("hand", "catapult", "runway"):
        launch = "hand"
    stall = stall_row.number if stall_row and stall_row.number else None
    if stall is not None:
        stall = min(max(stall, 5.0), 20.0)
    span = span_row.number if span_row and span_row.number else None
    if span is not None:
        span = min(max(span, 0.8), 3.0)
    return DesignRequirements(
        mtow_kg=mtow, cruise_speed_ms=18.0,
        cruise_altitude_m=min(max((alt_row.number if alt_row and alt_row.number
                                   else 100.0), 0.0), 4000.0),
        launch=launch, v_stall_max_ms=stall, span_max_m=span)


def derive(intake: Intake) -> Intake:
    """Recompute the derived tier + feasibility. Cruise speed is solved to the
    best-L/D fixed point: V = sqrt(2W / rho S CL*), iterated with the sizing."""
    intake.rows = [r for r in intake.rows if r.status != "derived"]
    # Assumed defaults track their basis: an untouched MTOW cap follows the
    # payload row (payload / payload-fraction), so parsing "180 g camera"
    # doesn't leave a stale 3.3 kg default behind.
    mtow_row, payload_row = _row(intake, "mtow_cap"), _row(intake, "payload")
    if (mtow_row is not None and mtow_row.origin == "assumed"
            and payload_row is not None and payload_row.number):
        mtow_row.number = round(min(payload_row.number / PAYLOAD_FRACTION, 25.0), 2)
        mtow_row.value = f"≤ {mtow_row.number:g} kg"
        mtow_row.source = f"payload / {PAYLOAD_FRACTION:.0%} payload fraction"
    try:
        req = to_requirements(intake)
        rho = atmosphere_at(req.cruise_altitude_m).density_kgm3
        w = req.mtow_kg * 9.81
        res = solve_requirements(req)
        for _ in range(6):
            v_best = math.sqrt(2.0 * w / (rho * res.panel.s_ref_m2
                                          * max(res.panel.cl_star, 0.05)))
            v_best = min(max(v_best, 8.0), 60.0)
            if abs(v_best - req.cruise_speed_ms) < 0.05:
                break
            req = req.model_copy(update={"cruise_speed_ms": v_best})
            res = solve_requirements(req)
    except Exception as e:  # never crash the intake — report instead
        intake.feasibility = Feasibility(
            verdict="error", headline="Could not size these requirements",
            detail=str(e))
        return intake

    p = res.panel

    def drow(rid: str, label: str, value: str, number: float, unit: str = "",
             source: str = "") -> IntakeRow:
        return IntakeRow(id=rid, tier="derived", label=label, value=value,
                         number=number, unit=unit, status="derived",
                         origin="derived", source=source, info=_INFO[rid],
                         drivers=_DERIVED_DRIVERS.get(rid, []))

    intake.rows += [
        drow("wing_loading", "Wing loading", f"{p.wing_loading_kgm2:.1f} kg/m²",
             p.wing_loading_kgm2, "kg/m²", "from MTOW and wing sizing"),
        drow("s_ref", "Wing area", f"{p.s_ref_m2:.3f} m²", p.s_ref_m2, "m²",
             "sizing solve"),
        drow("cruise_speed", "Cruise speed", f"{p.v_ms:.1f} m/s", p.v_ms, "m/s",
             "best-L/D fixed point"),
        drow("cruise_cl", "Cruise CL", f"{p.cl_cruise:.3f}", p.cl_cruise, "",
             "CL = W/(q S)"),
        drow("clmax", "CLmax (3D)", f"{p.cl_max_3d:.2f}", p.cl_max_3d, "",
             "sections at this Reynolds"),
        drow("static_margin", "Static margin target", "8 % MAC", 0.08, "",
             "tailless BWB practice"),
        drow("ld_est", "Cruise L/D (equation)", f"{p.ld_cruise:.1f}",
             p.ld_cruise, "", "equation tier — VSPAERO/CFD supersede"),
    ]

    unmet = [s for s in res.requirements if s.met is False]
    implied = {
        "MTOW": f"{req.mtow_kg:.2f} kg",
        "wing area": f"{p.s_ref_m2:.3f} m²",
        "span": f"{p.span_m:.2f} m",
        "cruise": f"{p.v_ms:.1f} m/s",
        "stall": f"{p.v_stall_ms:.1f} m/s",
        "L/D": f"{p.ld_cruise:.1f}",
    }
    binding_rows = {"stall_at_launch": ["launch"], "stall_requirement": ["v_stall_max"],
                    "span_cap": ["span_max"], "cruise_cl_cap": ["mtow_cap"],
                    "ar_gate": [], "physics_limit": []}
    if res.binding_constraint is None and not unmet:
        intake.feasibility = Feasibility(
            verdict="feasible", headline="Feasible — requirements close",
            detail="Sized at best-L/D cruise. " + "; ".join(res.notes[:2]),
            implied=implied)
    else:
        intake.feasibility = Feasibility(
            verdict="binding",
            headline=f"Limited by {BINDING_LABELS.get(res.binding_constraint, 'a constraint')}",
            detail=res.binding_explanation,
            implied=implied,
            binding_row_ids=binding_rows.get(res.binding_constraint or "", []))
    intake.notes = res.notes
    return intake
