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
from typing import Any, Dict, List, Literal, NamedTuple, Optional

from pydantic import BaseModel, Field

from .aero_estimates import LAUNCH_VSTALL_MAX
from .atmosphere import atmosphere_at
from .defaults import PAYLOAD_FRACTION
from .inverse import BINDING_LABELS, LockedValues, solve_requirements
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
    status: Status = Field(
        default="variable",
        description=(
            "Lock state. For the rows in LOCKABLE_ROW_IDS — max span, wing area and "
            "MTOW — 'fixed' CONSTRAINS THE SOLVE (S1-F): the solver must deliver that "
            "exact value or return a CapViolation saying by how much it could not. "
            "For every other row the flag still only records intent for a later stage; "
            "`lockable` says which is which, so the UI need not hard-code the list. "
            "Until S1-F this was display-only on every row (S3-E1 / S7-CAP-3)."))
    origin: Origin = "assumed"
    source: str = ""       # citation for assumed defaults / derived values
    info: str = ""         # definition + formula for the info panel
    drivers: List[Driver] = Field(default_factory=list)  # derived rows only
    # S1-B: the `custom` flag went with user-added rows. The intake is a closed set, so
    # every row is one this pack declares and nothing can be flagged as an addition.
    used_value: str = ""
    """What the solver actually used, when that differs from `value` (S3-D2).

    Empty means "the typed value was used as written". The row can then render
    "you typed 100 m · using 3.0 m" instead of showing a requirement that is not in
    force."""
    drives_computation: bool = False
    """Whether editing this row changes any derived number (S3-E2/E3).

    Measured, not asserted: six of the twelve editable rows change nothing at all, and
    `test_bwb_disclosure.py` perturbs every row and fails if this flag disagrees with
    what actually moved."""
    lockable: bool = False
    """Whether setting `status` to 'fixed' on this row actually constrains the solve.

    Three rows do (S1-F): `span_max`, `s_ref`, `mtow_cap`. They are the ones that pin a
    number the sizing solve would otherwise choose. `cruise_speed` and `ld_est` are
    deliberately NOT lockable — they are outcomes, and a lock on an outcome is a
    requirement wearing the wrong hat."""
    unparsed: bool = False
    """The text in `value` could not be read as a number for this row (S2-D1f).

    The row keeps what the user typed — deleting it would be worse — but the flag lets
    the UI mark it instead of presenting junk as a requirement. `number` is None here,
    so the solver falls back to its documented default; `parse_error` says why."""
    parse_error: str = ""


WarningKind = Literal["clamped", "rejected", "unparsed", "ambiguous", "overridden",
                      "recovered", "not_modelled"]


class IntakeWarning(BaseModel):
    """One thing the user should know about the gap between what they typed and what
    the solver used (S3-D2).

    Structured rather than a sentence because the UI has to render "you typed 100 m ·
    using 3.0 m (schema maximum)" next to the row, and because a test can then assert
    the CLAIM against the COMPUTATION — `used` really is what `to_requirements`
    returns — instead of grepping prose.
    """
    kind: WarningKind
    message: str
    row_id: str = ""
    typed: str = ""        # what the user wrote
    used: str = ""         # what the solver actually used
    limit: str = ""        # the bound that forced the substitution, with its source


class Feasibility(BaseModel):
    verdict: Literal["feasible", "feasible_with_substitutions", "binding", "error"]
    headline: str
    detail: str
    implied: Dict[str, str] = Field(default_factory=dict)
    binding_row_ids: List[str] = Field(default_factory=list)
    substitutions: int = 0
    """How many typed values the solver replaced.

    A verdict of plain "feasible" while a hard cap was silently substituted is the
    dishonest state this exists to prevent: the screen said feasible, the user's 100 m
    span had become 3.0 m, and nothing on the page said so."""


class Intake(BaseModel):
    brief: str = ""
    rows: List[IntakeRow]
    feasibility: Optional[Feasibility] = None
    notes: List[str] = Field(default_factory=list)
    warnings: List[IntakeWarning] = Field(default_factory=list)
    """Everything the screen must say about this intake: an ambiguous brief, a value
    that could not be parsed, a clamp, an override of an earlier edit.

    Two ownerships, which is why `kind` matters. `derive()` OWNS the computed kinds
    (clamped, rejected, unparsed) and rebuilds them from scratch on every call, so a
    fixed row's warning disappears. The event kinds (ambiguous, overridden) record what
    happened when a brief was parsed and survive later derives — nothing recomputes
    them."""


# ---------------------------------------------------------------------------
# info texts — definition + formula per row, mirrored by the UI's i-panels
# ---------------------------------------------------------------------------

_INFO = {
    "mission_type": "Sets every default downstream: surveillance favours efficiency "
                    "and stability; racing favours speed.",
    "payload": "Everything carried. Mass adds to MTOW, which sets the wing area the "
               "stall floor demands. Each item needs a mass.",
    "altitude": "Cruise height. Sets air density rho (ISA) used in every lift and "
                "drag equation.",
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
    "clmax": "Peak 3D lift coefficient before stall. At this tier it is "
             "1.30 × 0.90 × cos(sweep): it responds ONLY to planform sweep, not to "
             "the airfoil row and not to Reynolds number (S7-CAP-4). Enters the "
             "stall equation.",
    "static_margin": "(x_np - x_cg)/MAC. Positive = nose settles back after a gust. "
                     "A FIXED TARGET of 8% MAC (engineering judgment, tailless BWB "
                     "practice) — nothing on this screen changes it.",
    "ld_est": "Equation-level cruise L/D of the sized baseline planform. VSPAERO "
              "and CFD supersede this number.",
}

# ---------------------------------------------------------------------------
# driver chips — the ⓘ panel's "to change it, change these" list (S3-E4)
#
# The old list was hand-kept and two of its entries were fiction: `static_margin`
# claimed `wind` and `mission_type` as drivers while it is the hardcoded constant
# STATIC_MARGIN_TARGET = 0.08, and `clmax` claimed `airfoil`, which moves nothing at
# all (the airfoil row is inert — S3-E2).
#
# MEASURED_DRIVERS is the measured dependency set: perturb each editable row over a
# grid of base intakes and record which derived numbers move.
# `test_bwb_disclosure.py::test_driver_chips_match_the_measured_dependency_set`
# re-runs that measurement on every test run and fails if this table disagrees, so the
# chips cannot drift back into fiction. The six computing rows all reach every derived
# number through the sizing solve, which is why the lists are identical — that IS the
# finding, not an oversight.
# ---------------------------------------------------------------------------

_COMPUTING = ["payload", "altitude", "launch", "span_max", "v_stall_max", "mtow_cap"]

MEASURED_DRIVERS = {
    "wing_loading": _COMPUTING,
    "s_ref": _COMPUTING,
    "cruise_speed": _COMPUTING,
    "cruise_cl": _COMPUTING,
    "clmax": _COMPUTING,
    # None. It is the constant STATIC_MARGIN_TARGET (aero_estimates.py), not an
    # outcome of anything on this screen.
    "static_margin": [],
    "ld_est": _COMPUTING,
}

# The direction of effect, where there is something specific to say. Pairs without an
# entry get the generic sentence — honest about the mechanism without inventing a
# direction that the solve does not guarantee.
_GENERIC_EFFECT = "changes the sizing solve, which moves this number"
_DRIVER_EFFECTS = {
    "wing_loading": {
        "mtow_cap": "lower MTOW cap → lower W/S",
        "v_stall_max": "lower stall speed → bigger wing → lower W/S",
        "launch": "hand launch caps stall → floors wing area",
        "payload": "less payload → less mass",
        "altitude": "thinner air → more area for the same stall speed",
        "span_max": "span cap limits how the area is spent",
    },
    "s_ref": {
        "v_stall_max": "lower stall speed → larger area",
        "mtow_cap": "more mass → larger area",
        "span_max": "span cap limits how the area is spent",
        "payload": "more payload → more mass → larger area",
        "launch": "a tighter launch cap floors the area",
        "altitude": "thinner air → larger area",
    },
    "cruise_speed": {
        "mtow_cap": "heavier → faster best-L/D speed",
        "v_stall_max": "bigger wing → slower best-L/D speed",
        "altitude": "thinner air → faster",
        "payload": "heavier → faster",
        "span_max": "span cap changes AR, and AR sets CL*",
        "launch": "a tighter launch cap enlarges the wing → slower",
    },
    "cruise_cl": {
        "v_stall_max": "bigger wing → lower cruise CL",
        "mtow_cap": "heavier → higher cruise CL",
        "payload": "heavier → higher cruise CL",
    },
    "clmax": {
        # CLmax = CL_MAX_2D * CL_MAX_3D_FACTOR * cos(sweep) — it responds ONLY to
        # planform sweep, which these rows move through the sizing solve. Notably NOT
        # to the airfoil row, which the old chip claimed.
        row: "changes the solved planform sweep, and CLmax scales with cos(sweep)"
        for row in _COMPUTING
    },
    "ld_est": {
        "span_max": "more span → higher AR → higher L/D",
        "v_stall_max": "tight stall cap → area floor → lower L/D",
        "mtow_cap": "heavier → higher cruise CL → lower L/D",
        "payload": "heavier → higher cruise CL → lower L/D",
        "altitude": "thinner air → different Reynolds and cruise CL",
        "launch": "a tighter launch cap enlarges the wing → lower L/D",
    },
}

_DERIVED_DRIVERS = {
    derived_id: [Driver(row_id=row,
                        effect=_DRIVER_EFFECTS.get(derived_id, {}).get(
                            row, _GENERIC_EFFECT))
                 for row in rows]
    for derived_id, rows in MEASURED_DRIVERS.items()
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
    return Intake(brief="", rows=_mark_lockable(rows))


# ---------------------------------------------------------------------------
# brief parser — deterministic, never invents what it did not match
# ---------------------------------------------------------------------------

_NUM = r"(\d+(?:\.\d+)?)"

# ---------------------------------------------------------------------------
# quantity parsing — ONE parser for the brief path and the edit path (S2-D1)
#
# The old `_num` was `re.search(r"(\d+(?:\.\d+)?)", text.replace(",", "."))`: no sign,
# no units, no anchoring, no range check. Every one of these was HTTP 200 and invisible:
#
#   "-3 kg"   -> +3.0      sign dropped
#   "1,500"   -> 1.5       thousands separator read as a decimal point (1000x)
#   "1.5e3"   -> 1.5       exponent truncated (1000x)
#   "1e400"   -> 1.0       exponent truncated
#   "9"*400   -> inf       serialised as null, so the row silently lost its value
#   "6 ft"    -> 6 m       unit ignored
#   "1800 mm" -> 1800 m    unit ignored on the EDIT path but honoured on the BRIEF
#                          path, so the same string meant different things depending
#                          on how it arrived
#   "abc"     -> None      row displayed the junk as the requirement while the solver
#                          used the schema default
#
# The replacement is unit-aware, sign-aware and magnitude-checked, returns WHY it
# failed, and is used by both paths so they cannot disagree.
# ---------------------------------------------------------------------------

# Conversion to the canonical SI unit of each dimension. Values are exact where the
# definition is exact (international foot, pound, nautical mile).
_UNITS: Dict[str, Dict[str, float]] = {
    "length": {"m": 1.0, "cm": 0.01, "mm": 0.001, "km": 1000.0,
               "in": 0.0254, "inch": 0.0254, "inches": 0.0254,
               "ft": 0.3048, "foot": 0.3048, "feet": 0.3048},
    "mass": {"kg": 1.0, "g": 0.001, "gram": 0.001, "grams": 0.001,
             "lb": 0.45359237, "lbs": 0.45359237, "pound": 0.45359237,
             "pounds": 0.45359237},
    "speed": {"m/s": 1.0, "ms": 1.0, "mps": 1.0, "m/sec": 1.0,
              "kt": 0.5144444444444445, "kts": 0.5144444444444445,
              "knot": 0.5144444444444445, "knots": 0.5144444444444445,
              "km/h": 1.0 / 3.6, "kmh": 1.0 / 3.6, "kph": 1.0 / 3.6,
              "mph": 0.44704},
}
# S1-A: the "time" dimension went with the endurance row. `endurance` was its only
# consumer, so minutes and hours are no longer units this pack recognises anywhere —
# "45 min" in a brief now yields a bare number that no row claims, which is the point.

CANONICAL_UNIT = {"length": "m", "mass": "kg", "speed": "m/s"}

# Which dimension each row's number is in. Rows absent from this table are text rows
# (mission type, launch method, structure...): a number in them is recorded if one
# happens to be there, and its absence is not an error.
ROW_DIMENSION = {
    "payload": "mass",
    "mtow_cap": "mass",
    "span_max": "length",
    "altitude": "length",
    "v_stall_max": "speed",
    "cruise_speed": "speed",
}

# Longest-first so "m/s" wins over "m" and "mm" over "m".
_UNIT_ALTERNATION = "|".join(
    re.escape(u) for u in sorted(
        {u for table in _UNITS.values() for u in table}, key=len, reverse=True))
_NUMBER_TOKEN = r"[+-]?\d[\d,]*(?:\.\d+)?(?:[eE][+-]?\d+)?"
_QUANTITY_RE = re.compile(
    rf"(?P<number>{_NUMBER_TOKEN})\s*(?P<unit>{_UNIT_ALTERNATION})?(?![a-z/])",
    re.IGNORECASE)


class Quantity(NamedTuple):
    """The discriminated result of parsing one quantity (S2-D1e).

    `ok=False` carries the reason so the caller can tell the user WHY their entry was
    not understood instead of silently substituting a default."""
    value: Optional[float]
    unit: str          # the unit found in the text, "" when none was written
    ok: bool
    reason: str        # "" when ok


def _digits_to_float(raw: str) -> tuple:
    """(value, reason). Handles sign, thousands separators and exponents.

    Locale-free comma rule (S2-D1c): a comma followed by exactly three digits is a
    thousands separator; a comma followed by one or two digits is a decimal point.
    Anything else is ambiguous and is rejected rather than guessed — guessing is how
    "1,500" became 1.5.
    """
    text = raw.replace(" ", "")
    if "," in text:
        if re.fullmatch(r"[+-]?\d+,\d{1,2}", text):
            text = text.replace(",", ".")            # European decimal comma
        elif re.fullmatch(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?", text):
            text = text.replace(",", "")             # thousands separators
        else:
            return None, (f"{raw!r} mixes commas ambiguously — write 1500 or 1,500 "
                          f"for one thousand five hundred")
    try:
        value = float(text)
    except ValueError:
        return None, f"{raw!r} is not a number"
    if not math.isfinite(value):
        # 1e400 and a 400-digit integer both land here. The old code returned inf,
        # which pydantic then serialised as null and the row lost its value entirely.
        return None, f"{raw!r} is too large to represent"
    return value, ""


def parse_quantity(text: str, dimension: Optional[str] = None) -> Quantity:
    """Parse the first quantity in `text`, converting to `dimension`'s SI unit.

    A bare number is accepted and read in the canonical unit (typing "2.2" for a span
    means 2.2 m). A unit from the WRONG dimension is refused by name rather than
    silently ignored, because ignoring it is what made "6 ft" mean 6 metres.
    """
    if text is None:
        return Quantity(None, "", False, "no value given")
    stripped = str(text).strip()
    if not stripped:
        return Quantity(None, "", False, "no value given")

    match = _QUANTITY_RE.search(stripped)
    if not match:
        return Quantity(None, "", False, f"no number found in {stripped!r}")

    value, reason = _digits_to_float(match.group("number"))
    if value is None:
        return Quantity(None, "", False, reason)

    unit = (match.group("unit") or "").lower()
    if dimension is None:
        return Quantity(value, unit, True, "")

    table = _UNITS[dimension]
    if not unit:
        return Quantity(value, "", True, "")
    if unit in table:
        return Quantity(value * table[unit], unit, True, "")
    return Quantity(
        None, unit, False,
        f"{unit!r} is not a {dimension} unit — use "
        f"{', '.join(sorted(set(table)))}")


def _num(text: str) -> Optional[float]:
    """Dimensionless number for text rows. Kept for callers that have no dimension."""
    return parse_quantity(text).value


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


# S1-A: ENDURANCE_MIN_MIN / ENDURANCE_MIN_MAX / clamp_endurance were removed with the
# endurance row. They existed to keep "loiter for 11 hours" from making every later
# generate return 500 (S2-B1) — a clamp guarding a value nothing computed with. With
# the row gone there is no value to clamp and no failure mode to guard.


# A keyword and its number belong to the same clause. The gap between them may not
# contain a digit (that would be a DIFFERENT quantity) nor a comma, semicolon or full
# stop (that would be a different clause). The old `[^.\d]{0,20}` allowed commas, which
# is exactly how "2.2 m span, fly at 300 m" made span 300 m (S2-D4).
_GAP = r"[^\d,;.]{0,12}"

# Each row: the keywords that name it and the dimension its number is in. Both
# orderings are tried — "span 2.2 m" and "2.2 m span" — because English uses both.
_BRIEF_ROWS = [
    ("span_max", r"span|wingspan", "length", "≤ {v:g} m"),
    ("mtow_cap", r"mtow|take-?off (?:mass|weight)|all-?up|total mass", "mass",
     "≤ {v:g} kg"),
    ("payload", r"payload|camera|sensor|gimbal|carry", "mass", "{v:g} kg"),
    ("altitude", r"altitude|amsl|agl|fly at|flying at|cruise at|at", "length",
     "{v:g} m AMSL"),
    ("v_stall_max", r"stall", "speed", "≤ {v:g} m/s"),
]

# Most specific launch family first, so a brief naming two of them resolves the same
# way every time. The old loop resolved by dict order, which is not a rule anyone can
# predict from the UI (S2-D4d).
_LAUNCH_FAMILIES = [
    ("runway", r"runway|wheel"),
    ("catapult", r"catapult|bungee"),
    ("hand", r"hand|throw"),
]


def _brief_matches(text: str, keywords: str, dimension: str) -> List[tuple]:
    """(position, value) for every quantity in `text` attached to `keywords`.

    Returns all of them, not the first, so the caller can report an ambiguous brief
    instead of silently picking one (S2-D4c).
    """
    units = "|".join(re.escape(u) for u in
                     sorted(_UNITS[dimension], key=len, reverse=True))
    number = _NUMBER_TOKEN
    if keywords:
        # \b so "at" does not match inside "catapult" or "data".
        word = rf"\b(?:{keywords})\b"
        patterns = [
            # keyword first: "span 2.2 m", "span of 2.2" (unit optional — the row's
            # own dimension supplies it)
            rf"{word}{_GAP}(?P<q>{number}\s*(?:{units})?)(?![a-z0-9/])",
            # value first: "2.2 m span" (unit required, or any bare number before the
            # keyword would qualify)
            rf"(?P<q>{number}\s*(?:{units}))(?![a-z0-9/]){_GAP}{word}",
        ]
    else:
        # No keyword: the unit alone identifies the row, so it is mandatory.
        patterns = [rf"(?P<q>{number}\s*(?:{units}))(?![a-z0-9/])"]
    found = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            parsed = parse_quantity(match.group("q"), dimension)
            if parsed.ok and parsed.value is not None:
                found.append((match.start("q"), parsed.value))
    # de-duplicate by position: both orderings can match the same characters
    unique = {}
    for position, value in sorted(found):
        unique.setdefault(position, value)
    return sorted(unique.items())


def parse_brief(text: str) -> Intake:
    """Parse a freeform brief into the tiered list. Matched fields become
    you_said + fixed; everything else keeps its cited assumed default.

    Every pattern is anchored to its OWN number (S2-D4). Before this, the span pattern
    reached across a comma into the next clause: "2.2 m span, fly at 300 m" produced
    span_max = 300 m, and the altitude branch then discarded its own correct match
    because it equalled the span — two wrong requirements, both stamped
    origin="you_said" and presented back to the user as their own words.
    """
    intake = default_intake()
    rows = {r.id: r for r in intake.rows}
    t = " " + text.lower().replace("\n", " ") + " "

    for pat, label in _MISSION_WORDS:
        if re.search(pat, t):
            _set(rows["mission_type"], label)
            break

    for rid, keywords, dimension, display in _BRIEF_ROWS:
        matches = _brief_matches(t, keywords, dimension)
        if not matches:
            continue
        value = matches[0][1]
        if rid == "altitude" and not 0 <= value <= 4000:
            continue                     # "at 30000 m" is not an altitude in scope
        distinct = sorted({v for _, v in matches})
        if len(distinct) > 1:
            unit = CANONICAL_UNIT[dimension]
            intake.warnings.append(IntakeWarning(
                kind="ambiguous", row_id=rid,
                typed=", ".join(f"{v:g} {unit}" for v in distinct),
                used=f"{value:g} {unit}",
                message=(f"{rows[rid].label}: the brief gives "
                         f"{', '.join(f'{v:g} {unit}' for v in distinct)} — used "
                         f"{value:g} {unit} (the first one)")))
        _set(rows[rid], display.format(v=value), value)

    matched_launches = [name for name, pattern in _LAUNCH_FAMILIES
                        if re.search(pattern, t)]
    if matched_launches:
        launch = matched_launches[0]
        if len(matched_launches) > 1:
            intake.warnings.append(IntakeWarning(
                kind="ambiguous", row_id="launch",
                typed=", ".join(matched_launches), used=launch,
                message=(f"Launch and recovery: the brief mentions "
                         f"{', '.join(matched_launches)} — used {launch} (the most "
                         f"specific method wins)")))
        _set(rows["launch"], launch)
        cap = LAUNCH_VSTALL_MAX[launch]
        stall_row = rows["v_stall_max"]
        if stall_row.origin == "assumed":
            stall_row.value, stall_row.number = f"≤ {cap} m/s", cap
            stall_row.source = f"{launch} launch (Sadraey)"

    intake.brief = text
    return intake


# Fields copied from a freshly parsed row onto the stored one. `status` and `info` are
# deliberately absent: the lock state is the user's, and the info text is the pack's.
_PARSED_FIELDS = ("value", "number", "unit", "origin", "source", "unparsed",
                  "parse_error")


def merge_brief(current: Intake, text: str) -> Intake:
    """Parse `text` and merge it ONTO the stored intake (S2-D5).

    `parse_brief` starts from `default_intake()` and returns a fresh list, so using its
    result directly is a silent reset: parsing after editing reverted an edited
    `mtow_cap` to the assumed default and DELETED a user-added custom row, with a 200
    and no warning.

    Precedence: what this brief says wins, because the user just typed it; a prior edit
    survives where the brief is silent; custom rows are never touched. Where the brief
    contradicts an edit, the override is reported so it is visible rather than merely
    correct.
    """
    parsed = parse_brief(text)
    merged = current.model_copy(deep=True)
    merged.brief = text
    merged.warnings = list(parsed.warnings)
    stored = {r.id: r for r in merged.rows}

    for row in parsed.rows:
        target = stored.get(row.id)
        if target is None:                 # a row the stored intake never had
            merged.rows.append(row.model_copy(deep=True))
            continue
        if row.origin != "you_said":
            continue                       # the brief is silent: keep what is stored
        if target.origin == "edited" and target.value != row.value:
            merged.warnings.append(IntakeWarning(
                kind="overridden", row_id=target.id, typed=target.value,
                used=row.value,
                message=(f"{target.label}: your brief says {row.value} — replacing "
                         f"your earlier edit of {target.value}")))
        for field in _PARSED_FIELDS:
            setattr(target, field, getattr(row, field))
    return merged


# ---------------------------------------------------------------------------
# edits — apply a client payload back onto a validated Intake
# ---------------------------------------------------------------------------

# S1-B: MAX_ROWS = 64 was removed. It bounded a runaway client because every unknown
# edit id became a new row, so an unbounded row count was unbounded disk (S1-A3: 500
# edits in one call produced a 519-row intake). `apply_edits` now refuses unknown ids
# outright, so the row list cannot grow at all and the cap became unreachable. The
# attack is closed at its source rather than bounded downstream.


_KNOWN_ROW_IDS: Optional[tuple] = None


def known_row_ids() -> tuple:
    """The closed set of requirement ids Stage 1 accepts.

    Both halves matter: `default_intake()` supplies the 11 input rows, and `derive()`
    appends the 7 derived ones (wing_loading, s_ref, cruise_speed, cruise_cl, clmax,
    static_margin, ld_est). Building this from `default_intake()` alone made
    `apply_edits` refuse a legitimate edit to `cruise_speed`, which the disclosure
    suite caught immediately.

    Computed from the pack itself so it cannot drift from the rows that actually exist.
    Cached because `derive()` runs the sizing solve."""
    global _KNOWN_ROW_IDS
    if _KNOWN_ROW_IDS is None:
        _KNOWN_ROW_IDS = tuple(r.id for r in derive(default_intake()).rows)
    return _KNOWN_ROW_IDS


def _checked_scalar(rid: str, value: Any) -> Any:
    """Reject payload types that only ever reach the user as a Python repr (S2-D3).

    Confirmed before this: `value: [1,2]` was stored as the string "[1, 2]" with
    number 1.0, and `value: null` as the literal string "None". `bool` is excluded
    explicitly because it is an int subclass, so `True` would otherwise pass."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(
            f"row '{rid}': value must be text or a number, got "
            f"{type(value).__name__}")
    return value


def _set_row_value(row: IntakeRow, text: str, origin_when_assumed: str,
                   source_when_assumed: str) -> None:
    """Store a user-supplied value, parsed in the row's own dimension (S2-D1).

    A value that cannot be read keeps the user's text but sets `unparsed`, so the row
    can never display something the solver is not using without saying so."""
    row.value = text
    parsed = parse_quantity(text, ROW_DIMENSION.get(row.id))
    if parsed.ok:
        row.number, row.unparsed, row.parse_error = parsed.value, False, ""
    elif row.id in ROW_DIMENSION:
        row.number, row.unparsed, row.parse_error = None, True, parsed.reason
    else:
        # A text row (mission type, structure, ...). No number is expected, so a
        # missing one is not an error.
        row.number, row.unparsed, row.parse_error = None, False, ""
    if row.origin == "assumed":
        row.origin = origin_when_assumed
        row.source = source_when_assumed


def _restore_default(intake: Intake, row: IntakeRow) -> None:
    """`value: null` means 'clear this row', i.e. go back to the assumed default."""
    default = next((r for r in default_intake().rows if r.id == row.id), None)
    if default is None:
        # No default to restore, so the row is not one this pack declares — it can only
        # have come from an intake stored before S1-B closed the set. Drop it.
        intake.rows = [r for r in intake.rows if r.id != row.id]
        return
    for field in ("value", "number", "unit", "origin", "source", "unparsed",
                  "parse_error"):
        setattr(row, field, getattr(default, field))


def apply_edits(current: Intake, edits: List[Dict[str, Any]]) -> Intake:
    """Merge client row edits onto a CLOSED set of requirements (S1-B).

    Value changes on assumed rows flip origin to 'edited'. Derived rows never accept
    edits. An id this pack does not declare is REFUSED by name — it does not become a
    row.

    Unknown ids used to be accepted as "custom" vehicle rows. The UI offered them as
    "+ Add constraint — max chord, materials, anything", and the row's own info text
    then admitted it was "recorded on this list only ... does not constrain the
    solver". Measured: adding one changed none of the derived values. Refusing the id
    is the honest version of a row that does nothing.

    Raises ValueError for an unknown id, or a value that is not text or a number.
    Edits to declared rows are always allowed."""
    rows = {r.id: r for r in current.rows}
    for e in edits:
        rid = str(e.get("id", ""))
        if rid in rows:
            row = rows[rid]
            if row.status == "derived":
                continue
            if "value" in e:
                if e["value"] is None:
                    # "clear this row" — restore the assumed default. The old code
                    # str()'d it and stored the literal string "None" (S2-D3).
                    _restore_default(current, row)
                elif str(_checked_scalar(rid, e["value"])) != row.value:
                    _set_row_value(row, str(e["value"]), origin_when_assumed="edited",
                                   source_when_assumed="your edit")
            if e.get("status") in ("fixed", "variable"):
                row.status = e["status"]
        elif rid in known_row_ids():
            # Declared, but not on THIS intake yet — a derived row that `derive()` has
            # not appended. There is nothing to edit (derived rows never accept edits,
            # see the `status == "derived"` branch above), so this is a no-op, not a
            # refusal. Keying the refusal on `current.rows` instead of the declared set
            # is exactly the bug the disclosure suite caught here.
            continue
        elif rid:
            # Refused, not ignored. A silently dropped edit is the failure mode this
            # pack has spent several stages removing: the user sees their requirement
            # accepted and no number moves.
            known = known_row_ids()
            raise ValueError(
                f"'{rid}' is not a requirement Stage 1 has. The intake is a closed set "
                f"of {len(known)} rows and none can be added (S1-B). Known ids: "
                f"{', '.join(sorted(known))}")
    return current


# ---------------------------------------------------------------------------
# derive — compute the derived tier + feasibility from the editable tiers
# ---------------------------------------------------------------------------

def _mark_lockable(rows: List[IntakeRow]) -> List[IntakeRow]:
    """Set `lockable` from the one declared list, so the UI never hard-codes it."""
    for r in rows:
        r.lockable = r.id in LOCKABLE_ROW_IDS
    return rows


def _row(intake: Intake, rid: str) -> Optional[IntakeRow]:
    return next((r for r in intake.rows if r.id == rid), None)


# The range each row's number must land in, and its source. ONE table, read by both
# the solver path (`usable_number`) and the disclosure (`substitution_warnings`), so
# the clamp the user is told about is by construction the clamp that happened — a
# second, hand-maintained copy of these bounds is exactly how a disclosure goes stale.
#
# `below` says what a value under the floor means: "clamp" it up, or "reject" it and
# fall back to the documented default. An MTOW cap of 0 is rejected rather than
# clamped because 0.01 kg is not what the user meant; the payload-fraction estimate is.
ROW_LIMITS = {
    "payload":      (0.01, 10.0, "clamp", "MissionSpec payload range"),
    "mtow_cap":     (0.0, 25.0, "reject", "MissionSpec MTOW range / 25 kg class limit"),
    "altitude":     (0.0, 4000.0, "clamp", "the pack's ISA table (0-4000 m)"),
    "v_stall_max":  (5.0, 20.0, "clamp", "DesignRequirements stall range"),
    "span_max":     (0.8, 3.0, "clamp", "BWBParams span range"),
    "cruise_speed": (8.0, 60.0, "clamp", "MissionSpec cruise-speed range"),
}


def _format(rid: str, value: float) -> str:
    unit = CANONICAL_UNIT.get(ROW_DIMENSION.get(rid, ""), "")
    return f"{value:g} {unit}".strip()


def resolved_number(intake: Intake, rid: str) -> tuple:
    """(value the solver will use, disclosure or None) for one numeric row (S3-D2).

    The single place a typed number becomes a used number. Everything the app says
    about substitution is derived from this function's second return value, so the
    claim cannot drift from the computation.
    """
    row = _row(intake, rid)
    if row is None:
        return None, None
    if row.unparsed:
        return None, IntakeWarning(
            kind="unparsed", row_id=rid, typed=row.value, used="the assumed default",
            message=(f"{row.label}: could not read \"{row.value}\" "
                     f"({row.parse_error}) — using the assumed default instead"))
    number = row.number
    if number is None:
        return None, None

    lo, hi, below, source = ROW_LIMITS.get(rid, (None, None, "clamp", ""))
    if not math.isfinite(number) or number < 0:
        # Includes the probed sign flip: altitude -500 used to become +500. A negative
        # altitude is a REJECTION, not a clamp — the user did not ask for 500 m.
        return None, IntakeWarning(
            kind="rejected", row_id=rid, typed=row.value, used="the assumed default",
            limit=source,
            message=(f"{row.label}: {row.value} is not a usable value — using the "
                     f"assumed default instead"))
    if lo is not None and number < lo:
        if below == "reject":
            return None, IntakeWarning(
                kind="rejected", row_id=rid, typed=row.value,
                used="the assumed default", limit=source,
                message=(f"{row.label}: {_format(rid, number)} cannot size an "
                         f"aircraft — using the assumed default instead"))
        return lo, IntakeWarning(
            kind="clamped", row_id=rid, typed=row.value, used=_format(rid, lo),
            limit=f"{_format(rid, lo)} minimum ({source})",
            message=(f"{row.label}: you typed {row.value} — using "
                     f"{_format(rid, lo)}, the lowest this tier supports "
                     f"({source})"))
    if hi is not None and number > hi:
        return hi, IntakeWarning(
            kind="clamped", row_id=rid, typed=row.value, used=_format(rid, hi),
            limit=f"{_format(rid, hi)} maximum ({source})",
            message=(f"{row.label}: you typed {row.value} — using "
                     f"{_format(rid, hi)}, the highest this tier supports "
                     f"({source})"))
    return number, None


def usable_number(intake: Intake, rid: str) -> Optional[float]:
    """The value the solver uses for a row, or None when there is nothing usable.

    Weak on purpose (S2-B5): a negative mass, a NaN or an unreadable string yields
    None so the caller applies its documented default, rather than a 500 or a sign
    flip. Out-of-range but sensible values are clamped, and every substitution is
    disclosed by `substitution_warnings`."""
    return resolved_number(intake, rid)[0]


def effective_stall_cap(intake: Intake) -> tuple:
    """(cap, source) — the stall speed actually in force, which is the tighter of the
    typed cap and the launch method's limit."""
    launch_row = _row(intake, "launch")
    launch = (launch_row.value.strip().lower() if launch_row else "hand")
    if launch not in LAUNCH_VSTALL_MAX:
        launch = "hand"
    typed = usable_number(intake, "v_stall_max")
    launch_cap = LAUNCH_VSTALL_MAX[launch]
    if typed is None or launch_cap < typed:
        return launch_cap, f"{launch} launch"
    return typed, "your stall cap"


def substitution_warnings(intake: Intake) -> List[IntakeWarning]:
    """Every gap between what the rows say and what the solver uses."""
    out = []
    for rid in ROW_LIMITS:
        _, warning = resolved_number(intake, rid)
        if warning is not None:
            out.append(warning)

    # The launch method silently overrides a looser stall cap: typing 99 m/s clamps to
    # 20 and then flies against a 10.5 m/s hand-launch limit. Two substitutions deep,
    # and neither was visible.
    typed = usable_number(intake, "v_stall_max")
    cap, source = effective_stall_cap(intake)
    stall_row = _row(intake, "v_stall_max")
    if typed is not None and stall_row is not None and cap < typed - 1e-9:
        out.append(IntakeWarning(
            kind="clamped", row_id="v_stall_max", typed=stall_row.value,
            used=_format("v_stall_max", cap), limit=f"{source} ({cap:g} m/s)",
            message=(f"{stall_row.label}: {source} limits you to {cap:g} m/s, which "
                     f"is tighter than the {typed:g} m/s in this row")))
    return out


def refresh_disclosure(intake: Intake) -> List[IntakeWarning]:
    """Bring every row's disclosure fields up to date; return the substitutions.

    Cheap (no solve), so a stored intake written by an older build can be shown
    honestly the moment it is READ — otherwise `drives_computation` is False on every
    row of an existing session and the screen labels the six rows that do drive the
    design "recorded only", which is the E2/E3 disclosure pointing the wrong way.
    """
    substitutions = substitution_warnings(intake)
    by_row = {w.row_id: w for w in substitutions}
    for row in intake.rows:
        if row.status == "derived":
            continue
        row.drives_computation = row.id in COMPUTING_ROWS
        warning = by_row.get(row.id)
        row.used_value = warning.used if warning else ""
    return substitutions


LOCKABLE_ROW_IDS = ("span_max", "s_ref", "mtow_cap")
"""The rows whose 'fixed' status constrains the solve (S1-F).

Chosen because each pins a number the sizing solve would otherwise choose for you: the
span you can transport, the wing you have already built, the mass you must not exceed.
`cruise_speed` and `ld_est` are excluded on purpose — they are OUTCOMES of the sizing,
and locking an outcome is a requirement in disguise, which the requirements panel
already expresses properly."""


def to_locked(intake: Intake) -> LockedValues:
    """Read the lock toggles into the solver's constraint object (S1-F).

    A row only locks when it is BOTH marked fixed and carries a usable number. A lock
    with nothing to lock to is not an error and not a constraint — it is a toggle the
    operator flipped before typing a value."""
    def pinned(rid: str):
        row = _row(intake, rid)
        if row is None or row.status != "fixed":
            return None
        n = row.number
        return n if (n is not None and n > 0) else None

    return LockedValues(span_m=pinned("span_max"), s_ref_m2=pinned("s_ref"),
                        mtow_kg=pinned("mtow_cap"))


def to_requirements(intake: Intake) -> DesignRequirements:
    """Map editable rows onto DesignRequirements. Cruise speed starts at a
    neutral guess — derive() then iterates it to the best-L/D speed."""
    payload = usable_number(intake, "payload") or 1.0
    mtow = usable_number(intake, "mtow_cap") or payload / PAYLOAD_FRACTION
    mtow = min(max(mtow, payload), 25.0)
    launch_row = _row(intake, "launch")
    launch = (launch_row.value.strip().lower() if launch_row else "hand")
    if launch not in ("hand", "catapult", "runway"):
        launch = "hand"
    return DesignRequirements(
        mtow_kg=mtow, cruise_speed_ms=18.0,
        cruise_altitude_m=usable_number(intake, "altitude") or 100.0,
        launch=launch,
        v_stall_max_ms=usable_number(intake, "v_stall_max"),
        span_max_m=usable_number(intake, "span_max"))


# Kinds derive() owns and rebuilds every call. The rest record a past event (an
# ambiguous brief, an overridden edit) and must survive.
_RECOMPUTED_KINDS = {"clamped", "rejected", "unparsed"}

# The six editable rows that measurably move a derived number. The other five
# (mission_type, regulatory, structure, airfoil, wind)
# change nothing at all — verified by perturbing each row and comparing all seven
# derived values (S3-E2/E3). `test_bwb_disclosure.py` re-measures this on every run, so
# the day one of them starts mattering the flag cannot stay wrong.
COMPUTING_ROWS = {"payload", "altitude", "launch", "span_max", "v_stall_max",
                  "mtow_cap"}


def derive(intake: Intake) -> Intake:
    """Recompute the derived tier + feasibility. Cruise speed is solved to the
    best-L/D fixed point: V = sqrt(2W / rho S CL*), iterated with the sizing."""
    intake.rows = [r for r in intake.rows if r.status != "derived"]
    intake.warnings = [w for w in intake.warnings
                       if w.kind not in _RECOMPUTED_KINDS]
    # Assumed defaults track their basis: an untouched MTOW cap follows the
    # payload row (payload / payload-fraction), so parsing "180 g camera"
    # doesn't leave a stale 3.3 kg default behind.
    mtow_row, payload_row = _row(intake, "mtow_cap"), _row(intake, "payload")
    # S1-F: a LOCKED MTOW stops following the payload row. This auto-follow is the one
    # thing the intake changes behind the operator's back, so it is the one thing a
    # lock on this row has to stop.
    if (mtow_row is not None and mtow_row.status != "fixed"
            and mtow_row.origin == "assumed"
            and payload_row is not None and payload_row.number):
        mtow_row.number = round(min(payload_row.number / PAYLOAD_FRACTION, 25.0), 2)
        mtow_row.value = f"≤ {mtow_row.number:g} kg"
        mtow_row.source = f"payload / {PAYLOAD_FRACTION:.0%} payload fraction"
    try:
        req = to_requirements(intake)
        locked = to_locked(intake)
        rho = atmosphere_at(req.cruise_altitude_m).density_kgm3
        w = req.mtow_kg * 9.81
        res = solve_requirements(req, locked=locked)
        for _ in range(6):
            v_best = math.sqrt(2.0 * w / (rho * res.panel.s_ref_m2
                                          * max(res.panel.cl_star, 0.05)))
            v_best = min(max(v_best, 8.0), 60.0)
            if abs(v_best - req.cruise_speed_ms) < 0.05:
                break
            req = req.model_copy(update={"cruise_speed_ms": v_best})
            res = solve_requirements(req, locked=locked)
    except Exception as e:  # never crash the intake — report instead
        intake.feasibility = Feasibility(
            verdict="error", headline="Could not size these requirements",
            detail=str(e))
        return intake

    # What the screen must say about the gap between typed and used (S3-D2). Computed
    # AFTER the solve so a row that could not be sized is still described.
    substitutions = refresh_disclosure(intake)
    intake.warnings += substitutions

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
    # `s_ref` is a derived row, so it is rebuilt here rather than existing at
    # construction — mark the whole set again so its `lockable` flag is right.
    _mark_lockable(intake.rows)

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
    count = len(substitutions)
    if res.binding_constraint is None and not unmet:
        # "Feasible" while a typed value was quietly replaced is the dishonest verdict
        # this state exists to prevent (S3-D2d).
        intake.feasibility = Feasibility(
            verdict="feasible_with_substitutions" if count else "feasible",
            headline=("Feasible — but not with every number you typed" if count
                      else "Feasible — requirements close"),
            detail=(("Sized at best-L/D cruise, after replacing "
                     f"{count} value{'s' if count != 1 else ''} you typed. ")
                    if count else "Sized at best-L/D cruise. ")
                   + "; ".join(res.notes[:2]),
            implied=implied, substitutions=count,
            binding_row_ids=[w.row_id for w in substitutions if w.row_id])
    else:
        intake.feasibility = Feasibility(
            verdict="binding",
            headline=f"Limited by {BINDING_LABELS.get(res.binding_constraint, 'a constraint')}",
            detail=res.binding_explanation,
            implied=implied, substitutions=count,
            binding_row_ids=binding_rows.get(res.binding_constraint or "", []))
    intake.notes = res.notes
    return intake
