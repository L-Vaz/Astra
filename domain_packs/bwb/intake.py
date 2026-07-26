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
    unparsed: bool = False
    """The text in `value` could not be read as a number for this row (S2-D1f).

    The row keeps what the user typed — deleting it would be worse — but the flag lets
    the UI mark it instead of presenting junk as a requirement. `number` is None here,
    so the solver falls back to its documented default; `parse_error` says why."""
    parse_error: str = ""


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
    warnings: List[str] = Field(default_factory=list)
    """Plain-language notices about THIS intake: an ambiguous brief, a value that could
    not be parsed, a clamp, an override of an earlier edit. Stage 3 (S3-D2) replaces the
    strings with a structured IntakeWarning carrying typed/used/limit."""


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
    "time": {"min": 1.0, "mins": 1.0, "minute": 1.0, "minutes": 1.0,
             "h": 60.0, "hr": 60.0, "hrs": 60.0, "hour": 60.0, "hours": 60.0},
}

CANONICAL_UNIT = {"length": "m", "mass": "kg", "speed": "m/s", "time": "min"}

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
    "endurance": "time",
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


ENDURANCE_MIN_MIN = 1.0
ENDURANCE_MIN_MAX = 600.0
"""MissionSpec.endurance_min is `gt=0, le=600` (schema.py). Endurance drives no
computation at this tier, so a brief outside that range must be recorded and capped,
never rejected: "loiter for 11 hours" parsed 200 OK and then made every subsequent
generate return 500, forever, with no indication which row was at fault (S2-B1)."""


def clamp_endurance(value: float) -> tuple:
    """(value, source) — clamp into the schema range and say so in the row's source."""
    if value > ENDURANCE_MIN_MAX:
        return ENDURANCE_MIN_MAX, (
            f"your brief (capped at {ENDURANCE_MIN_MAX:g} min — endurance is "
            f"recorded only)")
    if value < ENDURANCE_MIN_MIN:
        return ENDURANCE_MIN_MIN, (
            f"your brief (raised to {ENDURANCE_MIN_MIN:g} min — endurance is "
            f"recorded only)")
    return value, "your brief — recorded only (aero scope)"


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
    ("endurance", r"endurance|loiter|aloft|flight time|fly for", "time",
     "≥ {v:g} min"),
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
        if not matches and rid == "endurance":
            # Time units name their own row: nothing else in a drone brief is measured
            # in minutes or hours, so "30 minutes" needs no keyword.
            matches = _brief_matches(t, r"", "time")
        if not matches:
            continue
        value = matches[0][1]
        if rid == "altitude" and not 0 <= value <= 4000:
            continue                     # "at 30000 m" is not an altitude in scope
        distinct = sorted({v for _, v in matches})
        if len(distinct) > 1:
            unit = CANONICAL_UNIT[dimension]
            intake.warnings.append(
                f"{rows[rid].label.lower()}: the brief gives "
                f"{', '.join(f'{v:g} {unit}' for v in distinct)} — used "
                f"{value:g} {unit}")
        if rid == "endurance":
            value, note = clamp_endurance(value)
            _set(rows[rid], display.format(v=value), value)
            rows[rid].source = note
        else:
            _set(rows[rid], display.format(v=value), value)

    matched_launches = [name for name, pattern in _LAUNCH_FAMILIES
                        if re.search(pattern, t)]
    if matched_launches:
        launch = matched_launches[0]
        if len(matched_launches) > 1:
            intake.warnings.append(
                f"launch: the brief mentions {', '.join(matched_launches)} — used "
                f"{launch} (most specific wins)")
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
            merged.warnings.append(
                f"{target.label.lower()}: your brief says {row.value} — replacing "
                f"your earlier edit of {target.value}")
        for field in _PARSED_FIELDS:
            setattr(target, field, getattr(row, field))
    return merged


# ---------------------------------------------------------------------------
# edits — apply a client payload back onto a validated Intake
# ---------------------------------------------------------------------------

MAX_ROWS = 64
"""Upper bound on rows in one intake (S1-A3).

Every unknown edit id becomes a new custom row and every save writes a new immutable
version, so an unbounded row count is unbounded disk: 500 edits in one call produced a
519-row intake. The default list is 19 rows, so 64 leaves room for a real user's
additions while keeping a runaway client bounded."""


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
    if default is None:                       # a custom row: drop it entirely
        intake.rows = [r for r in intake.rows if r.id != row.id]
        return
    for field in ("value", "number", "unit", "origin", "source", "unparsed",
                  "parse_error"):
        setattr(row, field, getattr(default, field))


def apply_edits(current: Intake, edits: List[Dict[str, Any]]) -> Intake:
    """Merge client row edits. Value changes on assumed rows flip origin to
    'edited'. Derived rows never accept edits. Unknown ids become custom
    vehicle rows (recorded; the solver maps recognised ids only).

    Raises ValueError when a request would push the intake past MAX_ROWS, or when a
    value is not text or a number; edits to EXISTING rows are always allowed, so a
    full intake stays editable."""
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
        elif rid and e.get("label"):
            if "value" in e and e["value"] is not None:
                _checked_scalar(rid, e["value"])
            if len(current.rows) >= MAX_ROWS:
                raise ValueError(
                    f"intake already has {len(current.rows)} rows (limit "
                    f"{MAX_ROWS}); custom row '{rid}' was not added. Remove a custom "
                    f"row first.")
            custom = IntakeRow(
                id=rid, tier="vehicle", label=str(e["label"]), value="",
                status="fixed" if e.get("status") != "variable" else "variable",
                origin="edited", source="your addition", custom=True,
                info=_INFO.get(rid, "Custom requirement — recorded and passed to "
                                    "Stage 2 as a constraint when recognised."))
            _set_row_value(custom, str(e.get("value", "")),
                           origin_when_assumed="edited",
                           source_when_assumed="your addition")
            current.rows.append(custom)
    return current


# ---------------------------------------------------------------------------
# derive — compute the derived tier + feasibility from the editable tiers
# ---------------------------------------------------------------------------

def _row(intake: Intake, rid: str) -> Optional[IntakeRow]:
    return next((r for r in intake.rows if r.id == rid), None)


def usable_number(intake: Intake, rid: str) -> Optional[float]:
    """The row's number when it can be used at all, else None (S2-B5).

    "Usable" is deliberately weak: finite and non-negative. Out-of-range but sensible
    values are still clamped downstream (and disclosed by S3-D2); what this rejects is
    input no clamp can rescue — a negative mass, a NaN, an unreadable string — where
    the honest move is the documented default rather than a 500 or a sign flip.

    A stored intake is a file, and S1-B3 shows files get damaged; nothing downstream
    may assume the number in one is sane.
    """
    row = _row(intake, rid)
    if row is None or row.number is None:
        return None
    if not math.isfinite(row.number) or row.number < 0:
        return None
    return row.number


def unusable_row_warnings(intake: Intake) -> List[str]:
    """Plain-language notice for every numeric row whose value cannot be used."""
    out = []
    for rid in ROW_DIMENSION:
        row = _row(intake, rid)
        if row is None:
            continue
        if row.unparsed:
            out.append(f"{row.label.lower()}: could not read {row.value!r} "
                       f"({row.parse_error}) — using the assumed default instead")
        elif row.number is not None and usable_number(intake, rid) is None:
            out.append(f"{row.label.lower()}: {row.value!r} is not a usable "
                       f"number — using the assumed default instead")
    return out


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
    stall = usable_number(intake, "v_stall_max")
    if stall is not None:
        stall = min(max(stall, 5.0), 20.0)
    span = usable_number(intake, "span_max")
    if span is not None:
        span = min(max(span, 0.8), 3.0)
    return DesignRequirements(
        mtow_kg=mtow, cruise_speed_ms=18.0,
        cruise_altitude_m=min(max(usable_number(intake, "altitude") or 100.0,
                                  0.0), 4000.0),
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
