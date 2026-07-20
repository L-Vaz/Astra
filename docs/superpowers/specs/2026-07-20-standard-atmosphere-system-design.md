# Standard Atmosphere System Design

**Date:** 2026-07-20

**Status:** Approved for implementation

**Authority:** U.S. Standard Atmosphere, 1976 (NOAA-S/T 76-1562 / NASA-TM-X-74335)

## Objective

Replace Astra's independent density formulas and fixed air-viscosity constants with one traceable, altitude-dependent atmosphere state used by mission sizing, analytical aerodynamics, inverse sizing, candidate generation, sensitivity analysis, cockpit previews, derivation explanations, VSPAERO, OpenFOAM case generation, y+ reporting, evaluation, and saved artifacts.

The default represents the standard atmosphere, not the weather on a particular day. A measured-temperature override is outside this change.

## Authoritative Source

The engineering authority is the U.S. Standard Atmosphere, 1976:

- NOAA-S/T 76-1562
- NASA-TM-X-74335
- NASA record: https://ntrs.nasa.gov/citations/19770009539
- NOAA publication: https://www.ngdc.noaa.gov/stp/space-weather/online-publications/miscellaneous/us-standard-atmosphere-1976/us-standard-atmosphere_st76-1562_noaa.pdf

The standard defines the thermodynamic atmosphere and its tabulated properties. Dynamic viscosity follows its Equation 51 and adopted constants. Kinematic viscosity follows Equation 52, `nu = mu / rho`.

Astra will commit a reproducibly generated subset rather than depend on a website at runtime. The subset covers geometric altitude from 0 through 4,000 m at 100 m intervals and uses SI units.

## Data Artifacts

The BWB domain pack will own:

- `domain_packs/bwb/data/us_standard_atmosphere_1976_0_4000m.csv`
- `domain_packs/bwb/data/us_standard_atmosphere_1976.metadata.json`
- a deterministic generator or verification script containing the cited standard constants and equations

The CSV columns are:

- `geometric_altitude_m`
- `geopotential_altitude_m`
- `temperature_k`
- `pressure_pa`
- `density_kgm3`
- `dynamic_viscosity_kgms`
- `kinematic_viscosity_m2s`

The metadata records the source identifiers and URLs, SI units, altitude interpretation, 100 m row interval, inclusive valid range, interpolation policy, generator version, and the CSV SHA-256 checksum.

The runtime does not use network access and does not silently regenerate the dataset.

## Altitude and Interpolation Policy

`cruise_altitude_m` remains geometric altitude above mean sea level, matching its existing AMSL meaning. The resolved state also records the corresponding geopotential altitude used by the standard.

For an exact table altitude, the service returns that row. Between rows, it linearly interpolates:

- geopotential altitude
- temperature
- pressure
- density
- dynamic viscosity

It then computes kinematic viscosity as `dynamic_viscosity / density`. Kinematic viscosity is not independently interpolated because doing so can violate the exact identity `nu = mu / rho`.

The state records the lower row altitude, upper row altitude, interpolation fraction, and whether the result is an exact row. Requests outside 0 through 4,000 m are rejected rather than clamped or extrapolated.

## Core API and Validation

`domain_packs/bwb/atmosphere.py` will own table loading and expose conceptually:

```python
atmosphere_at(altitude_m: float) -> AtmosphereState
```

`AtmosphereState` contains:

- geometric and geopotential altitude
- temperature in kelvin and Celsius
- pressure
- density
- dynamic and kinematic viscosity
- source identifier and dataset checksum
- interpolation provenance

The loader validates once that the table is ordered, unique, complete over the declared range, finite, and positive where appropriate. It verifies every row's `nu = mu / rho` within a strict relative tolerance and verifies the metadata checksum. Each lookup validates the requested altitude and the resolved result.

## Mission Schema and Compatibility

Newly derived missions carry a complete atmosphere state. Existing flat fields such as `air_density_kgm3` and `reynolds_mac` remain readable for API and artifact compatibility during migration.

Old `mission_derived.json` files that lack the new atmosphere object remain loadable. A Pydantic compatibility validator reconstructs the standard atmosphere from the saved mission altitude when necessary. New artifacts always serialize the explicit atmosphere object and its dataset checksum.

Reynolds number is computed from the resolved state:

```text
Re = rho * V * L / mu = V * L / nu
```

Tests require both forms to agree within numerical tolerance.

## Python Consumer Migration

All BWB paths will receive an `AtmosphereState` or resolve one from altitude at their boundary. Independent viscosity selection is removed from:

- mission derivation and enriched requirements
- analytical MAC and tip Reynolds numbers
- skin friction, parasite drag, and L/D estimates
- inverse sizing and requirements checking
- candidate generation and variant sizing
- sensitivity calculations
- derivation/explanation graphs
- cockpit server calculations
- MCP mission summaries
- VSPAERO reference Reynolds inputs

Temporary compatibility wrappers may accept density only for old external callers, but no in-repository production path may use an uncited viscosity fallback.

## Cockpit Behavior

The server includes the canonical atmosphere table metadata and rows in bootstrap data, along with the resolved mission atmosphere. The JavaScript preview uses one shared interpolation module and the same `nu = mu / rho` rule. `cockpit.js`, `req_panel.js`, `aero_mirror.js`, and the Why panel consume that shared state rather than duplicate density formulas or viscosity constants.

Changing altitude immediately refreshes temperature, pressure, density, both viscosities, dynamic pressure, MAC Reynolds number, drag estimates, and tip-Reynolds warnings. Server calculations remain authoritative when requirements or missions are persisted. Cross-language fixture tests compare Python and JavaScript over exact rows, interpolated points, and endpoints.

## VSPAERO and Saved Fast-Aero Results

The VSPAERO `ReCref` value comes from `MissionDerived.atmosphere` and the mission velocity and MAC. The invocation and `fast_aero.json` record altitude, temperature, density, both viscosities, reference length, velocity, Reynolds number, source identifier, and dataset checksum.

## OpenFOAM Flow Contract

Before simulation, Astra writes a deterministic `flow_conditions.json` containing:

- altitude and standard-atmosphere properties
- freestream velocity
- reference length and area
- reference Reynolds number
- atmosphere source and checksum

The natural-language requirement may explain these values, but it is not the correctness mechanism.

After the inherited input writer creates the Foundation OpenFOAM v10 case, a deterministic service writes the contracted kinematic viscosity to `constant/physicalProperties`. It then parses that dictionary and verifies the written dimension set and value against the contract. A mismatch rejects the case before solver execution. The validator does not rely on an LLM statement or a text search alone.

The existing inherited graph remains the CFD spine; the contract enforcement wraps the case-writing boundary rather than redesigning graph orchestration.

## y+ and Evaluation

Any y+ prediction added to the mesh workflow receives the contracted kinematic viscosity and records it with its wall-distance assumptions. Existing post-solve y+ extraction is augmented with the atmosphere and viscosity provenance needed to reproduce the assessment.

Evaluation artifacts include or reference the same `flow_conditions.json`. Where a quality contract or adjudication object exists, its flow section uses the same structured object rather than copying numbers. Astra currently has post-solve evaluation but no separate quality-contract or adjudicator subsystem; this change establishes the reusable flow contract without introducing a second decision engine.

## Artifact Reproducibility

The resolved atmosphere or a direct reference to `flow_conditions.json` is recorded in:

- `mission_derived.json`
- `fast_aero.json`
- `flow_conditions.json`
- mesh/y+ reports when present
- generated simulation case provenance
- `evaluation.json`
- stage manifests

The CSV checksum makes old runs reproducible if a later atmosphere dataset is introduced. A changed dataset requires a new source/version identifier rather than silently replacing the existing file.

## Error Handling

The system rejects:

- non-finite altitude or atmospheric values
- altitude outside the declared range
- malformed, unordered, duplicated, or incomplete table rows
- non-positive temperature, pressure, density, or viscosity
- metadata/checksum disagreement
- row or resolved-state `nu` inconsistency
- OpenFOAM viscosity or dimensions inconsistent with the flow contract

Errors identify the violated field, source dataset, requested altitude, and relevant expected value.

## Verification

Tests cover:

1. Exact lookups at every row and focused checkpoints at 0, 1,000, 2,000, and 4,000 m.
2. Linear interpolation and provenance at representative off-grid altitudes.
3. Endpoint behavior and rejection outside the range.
4. Dataset checksum and structural validation.
5. `nu = mu / rho` and the two Reynolds formulations.
6. Mission derivation, analytical aero, tip Reynolds, inverse sizing, variants, candidates, and sensitivities.
7. Derivation graph agreement with the analytical estimator.
8. Python/JavaScript atmosphere and aero parity.
9. VSPAERO reference Reynolds provenance.
10. OpenFOAM dictionary injection, readback, and deliberate-mismatch rejection.
11. Loading pre-change session JSON.
12. Golden geometry remaining unchanged and Reynolds changing only by the atmosphere-model difference.
13. Repository-wide scans that allow fixed viscosity values only in explicit test fixtures or unrelated FoamBench benchmark prompts.

## Delivery Sequence

Implementation proceeds in three testable slices:

1. Canonical dataset, atmosphere service, schema compatibility, and Python analytical consumers.
2. Cockpit parity, VSPAERO propagation, and artifact provenance.
3. Deterministic OpenFOAM flow contract, viscosity injection/readback, evaluation provenance, and full verification.

Each slice must pass its focused tests before the next begins. Final completion requires the deterministic golden verification that does not need an LLM; environment-dependent VSPAERO or OpenFOAM checks will be reported separately if the local doctor identifies unavailable runtimes.
