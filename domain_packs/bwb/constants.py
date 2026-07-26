"""BWB domain pack — constants shared by more than one module (S3-F12c).

`STATIC_MARGIN_TARGET` was declared twice: once in `aero_estimates.py` and once as the
default of `MissionDerived.static_margin_target` in `schema.py`. They were equal, and
nothing stopped them drifting — an aircraft whose CG is placed for one margin and
reported against another is a stability bug that no test would have caught.

They cannot simply import from each other: `aero_estimates` already imports `schema`,
so `schema` importing `aero_estimates` would be a cycle. This module is the shared
bottom of that dependency, and it deliberately holds nothing else.

Pydantic-free and import-free: safe from both the FoamAgent env (py3.12) and the
OpenVSP venv (py3.10).
"""

STATIC_MARGIN_TARGET = 0.08
"""Target static margin as a fraction of MAC.

ENGINEERING JUDGMENT (Astra): tailless-BWB practice sits around 5-10% MAC, and 8% is
the middle of that band. Not derived from a stability analysis of any particular
airframe, and not validated against flight data. It is the single number that places
the CG (`x_cg_for_target_sm_m = x_np - 0.08 * mac`), so moving it moves every CG this
pack recommends.

Sensitivity: +/-0.02 MAC moves the recommended CG by about 6.6 mm on the 0.332 m
reference MAC.
"""
