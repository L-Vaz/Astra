# Standard Atmosphere System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make one U.S. Standard Atmosphere 1976 state control every Astra BWB aerodynamic and CFD flow calculation from 0 through 4,000 m geometric altitude.

**Architecture:** Commit a verified 100 m SI dataset and load it through one Python atmosphere service. Carry an immutable `AtmosphereState` through mission, aero, cockpit, VSPAERO, and simulation artifacts; send the canonical rows to JavaScript for instant previews; deterministically enforce the contracted kinematic viscosity in Foundation OpenFOAM v10 before solving.

**Tech Stack:** Python 3.10+, Pydantic 2, pytest, JavaScript ES modules, Node.js parity tests, FastAPI, Foundation OpenFOAM v10 dictionaries.

## Global Constraints

- Authority: U.S. Standard Atmosphere, 1976, NOAA-S/T 76-1562 / NASA-TM-X-74335.
- Altitude is geometric altitude above mean sea level, valid inclusively from 0 through 4,000 m.
- The committed table has 100 m spacing and SI units; runtime lookup has no network access.
- Interpolate geopotential altitude, temperature, pressure, density, and dynamic viscosity linearly; calculate kinematic viscosity as `mu / rho`.
- Reject invalid or out-of-range inputs; never clamp or extrapolate.
- Preserve old `mission_derived.json` readability and existing flat output fields.
- Preserve the inherited Foam-Agent LangGraph workflow as the CFD spine.
- Foundation OpenFOAM v10 uses `constant/physicalProperties`.
- Preserve unrelated dirty-worktree changes, especially the existing cockpit asset-version edits.

---

### Task 1: Canonical Atmosphere Dataset and Lookup Service

**Files:**
- Create: `domain_packs/bwb/data/us_standard_atmosphere_1976_0_4000m.csv`
- Create: `domain_packs/bwb/data/us_standard_atmosphere_1976.metadata.json`
- Create: `domain_packs/bwb/atmosphere.py`
- Create: `verification/generate_standard_atmosphere.py`
- Create: `tests/test_bwb_atmosphere.py`

**Interfaces:**
- Produces: `AtmosphereState`, `InterpolationProvenance`, `atmosphere_at(altitude_m)`, `atmosphere_table_payload()`, and `DATASET_SHA256`.
- `AtmosphereState` fields use the exact names in the design spec.

- [ ] **Step 1: Write failing lookup, interpolation, invariant, range, and checksum tests**

```python
def test_standard_atmosphere_checkpoints():
    sea = atmosphere_at(0.0)
    assert sea.temperature_k == pytest.approx(288.15, rel=1e-10)
    assert sea.pressure_pa == pytest.approx(101325.0, rel=1e-10)
    assert sea.density_kgm3 == pytest.approx(1.224999, rel=2e-6)
    assert sea.dynamic_viscosity_kgms == pytest.approx(1.78938e-5, rel=2e-6)
    assert atmosphere_at(4000.0).kinematic_viscosity_m2s == pytest.approx(2.02746e-5, rel=2e-6)

def test_interpolation_derives_kinematic_viscosity():
    state = atmosphere_at(1275.0)
    assert state.interpolation.lower_table_altitude_m == 1200.0
    assert state.interpolation.upper_table_altitude_m == 1300.0
    assert state.interpolation.fraction == pytest.approx(0.75)
    assert state.kinematic_viscosity_m2s == pytest.approx(
        state.dynamic_viscosity_kgms / state.density_kgm3, rel=1e-12)

@pytest.mark.parametrize("altitude", [-1.0, 4000.01, float("nan"), float("inf")])
def test_invalid_altitudes_are_rejected(altitude):
    with pytest.raises(ValueError):
        atmosphere_at(altitude)
```

- [ ] **Step 2: Run `python -m pytest tests/test_bwb_atmosphere.py -q` and confirm import/test failures**

- [ ] **Step 3: Implement the generator and commit the generated dataset**

```python
def standard_row(geometric_altitude_m: float) -> dict[str, float]:
    h = EARTH_RADIUS_M * geometric_altitude_m / (EARTH_RADIUS_M + geometric_altitude_m)
    temperature = SEA_LEVEL_TEMPERATURE_K + LAPSE_RATE_KPM * h
    pressure = SEA_LEVEL_PRESSURE_PA * (temperature / SEA_LEVEL_TEMPERATURE_K) ** PRESSURE_EXPONENT
    density = pressure * MOLAR_MASS_KGPMOL / (UNIVERSAL_GAS_CONSTANT * temperature)
    dynamic_viscosity = SUTHERLAND_BETA * temperature ** 1.5 / (temperature + SUTHERLAND_K)
    return {"geometric_altitude_m": geometric_altitude_m,
            "geopotential_altitude_m": h, "temperature_k": temperature,
            "pressure_pa": pressure, "density_kgm3": density,
            "dynamic_viscosity_kgms": dynamic_viscosity,
            "kinematic_viscosity_m2s": dynamic_viscosity / density}
```

- [ ] **Step 4: Implement cached CSV loading, metadata/checksum validation, binary-search lookup, and payload serialization**

```python
def atmosphere_at(altitude_m: float) -> AtmosphereState:
    if not math.isfinite(altitude_m) or not MIN_ALTITUDE_M <= altitude_m <= MAX_ALTITUDE_M:
        raise ValueError(f"geometric altitude {altitude_m!r} outside [0, 4000] m")
    lower, upper = _bracket(float(altitude_m))
    fraction = 0.0 if lower.altitude_m == upper.altitude_m else (
        altitude_m - lower.altitude_m) / (upper.altitude_m - lower.altitude_m)
    values = {name: _lerp(getattr(lower, name), getattr(upper, name), fraction)
              for name in _INTERPOLATED_FIELDS}
    values["kinematic_viscosity_m2s"] = (
        values["dynamic_viscosity_kgms"] / values["density_kgm3"])
    return AtmosphereState(
        geometric_altitude_m=float(altitude_m),
        geopotential_altitude_m=values["geopotential_altitude_m"],
        temperature_k=values["temperature_k"],
        temperature_c=values["temperature_k"] - 273.15,
        pressure_pa=values["pressure_pa"],
        density_kgm3=values["density_kgm3"],
        dynamic_viscosity_kgms=values["dynamic_viscosity_kgms"],
        kinematic_viscosity_m2s=values["kinematic_viscosity_m2s"],
        source=SOURCE_ID,
        dataset_sha256=DATASET_SHA256,
        interpolation=InterpolationProvenance(
            lower_table_altitude_m=lower.altitude_m,
            upper_table_altitude_m=upper.altitude_m,
            fraction=fraction,
            exact_row=lower.altitude_m == upper.altitude_m))
```

- [ ] **Step 5: Run the atmosphere tests and the generator `--check` mode; expect all pass**

- [ ] **Step 6: Commit the dataset, service, generator, and tests**

---

### Task 2: Mission Schema, Derivation, and Backward Compatibility

**Files:**
- Modify: `domain_packs/bwb/schema.py`
- Modify: `domain_packs/bwb/defaults.py`
- Create: `tests/test_bwb_mission_atmosphere.py`
- Modify: `verification/verify_golden.py`

**Interfaces:**
- Consumes: `atmosphere_at(float) -> AtmosphereState`.
- Produces: `MissionDerived.atmosphere: AtmosphereState`; retains `air_density_kgm3` and `reynolds_mac`.

- [ ] **Step 1: Add failing tests for mission propagation and loading a legacy payload without `atmosphere`**

```python
def test_derive_carries_resolved_atmosphere():
    derived = derive(MissionSpec(payload_kg=1.5, cruise_speed_ms=25,
                                 cruise_altitude_m=1275, launch="hand"))
    assert derived.atmosphere.geometric_altitude_m == 1275
    assert derived.air_density_kgm3 == derived.atmosphere.density_kgm3
    assert derived.reynolds_mac == pytest.approx(
        25 * derived.params.mean_aero_chord_m() /
        derived.atmosphere.kinematic_viscosity_m2s)

def test_legacy_mission_derived_reconstructs_atmosphere():
    old = derive(MISSION).model_dump(exclude={"atmosphere"})
    loaded = MissionDerived.model_validate(old)
    assert loaded.atmosphere.geometric_altitude_m == MISSION.cruise_altitude_m
```

- [ ] **Step 2: Run the focused test and confirm it fails before implementation**

- [ ] **Step 3: Add `atmosphere` to `MissionDerived` with a before-validator that reconstructs only missing legacy state**

- [ ] **Step 4: Replace `MU_AIR` use in `derive`; keep `isa_density` as a deprecated compatibility wrapper returning `atmosphere_at(altitude_m).density_kgm3`**

- [ ] **Step 5: Run mission, golden-no-LLM, and existing inverse/derivation tests**

- [ ] **Step 6: Commit the schema and mission migration**

---

### Task 3: Analytical Aero, Inverse Sizing, Variants, Sensitivity, and Derivation Graph

**Files:**
- Modify: `domain_packs/bwb/aero_estimates.py`
- Modify: `domain_packs/bwb/inverse.py`
- Modify: `domain_packs/bwb/variants.py`
- Modify: `domain_packs/bwb/sensitivity.py`
- Modify: `domain_packs/bwb/derivation.py`
- Modify: `tests/test_bwb_inverse.py`
- Modify: `tests/test_bwb_sensitivity.py`
- Modify: `tests/test_bwb_derivation.py`

**Interfaces:**
- `estimate(params, atmosphere, v_ms, weight_n, launch) -> AeroEstimate`.
- `derive_graph(params, atmosphere, v_ms, weight_n, launch) -> DerivationGraph`.

- [ ] **Step 1: Change focused tests to supply `AtmosphereState` and assert MAC/tip Reynolds use its viscosity**

```python
state = atmosphere_at(4000.0)
panel = estimate(params, state, 25.0, 49.05, "hand")
assert panel.reynolds_mac == pytest.approx(25.0 * params.mean_aero_chord_m() /
                                          state.kinematic_viscosity_m2s)
assert panel.tip_re == pytest.approx(25.0 * params.chords()[2] /
                                    state.kinematic_viscosity_m2s)
```

- [ ] **Step 2: Run the three focused test modules and confirm signature failures**

- [ ] **Step 3: Replace estimator density parameters with `AtmosphereState`, using `state.density_kgm3` for q/stall and `state.kinematic_viscosity_m2s` for Reynolds**

- [ ] **Step 4: Resolve atmosphere once at each inverse/sensitivity boundary and pass the object through every estimate call**

- [ ] **Step 5: Replace the fixed-viscosity derivation leaf with altitude, temperature, pressure, density, dynamic viscosity, and kinematic viscosity nodes carrying interpolation provenance**

- [ ] **Step 6: Run all BWB analytical tests and confirm parity between estimator and derivation graph**

- [ ] **Step 7: Commit the analytical migration**

---

### Task 4: Intent, MCP, VSPAERO, and Fast-Aero Provenance

**Files:**
- Modify: `src/services/intent.py`
- Modify: `src/services/vspaero.py`
- Modify: `src/mcp/fastmcp_server.py`
- Create: `tests/test_bwb_atmosphere_services.py`

**Interfaces:**
- Enriched requirements include all atmosphere values and the source/checksum.
- `fast_aero.json` gains an `atmosphere` object and `reference_reynolds` provenance without removing existing fields.

- [ ] **Step 1: Write failing tests for enriched requirements, MCP summary fields, and the VSPAERO `--recref` argument**

- [ ] **Step 2: Run the focused service tests and confirm missing atmosphere provenance**

- [ ] **Step 3: Format enriched flow conditions from `MissionDerived.atmosphere`, including pressure, temperature, `mu`, `nu`, and source**

- [ ] **Step 4: Add atmosphere fields to plan-design summaries and fast-aero output; derive `ReCref` from velocity, MAC, and `nu`**

- [ ] **Step 5: Run focused tests plus `python verification/verify_golden.py --no-llm`**

- [ ] **Step 6: Commit the service and VSPAERO propagation**

---

### Task 5: Cockpit Python and JavaScript Parity

**Files:**
- Create: `src/cockpit/static/atmosphere.js`
- Modify: `src/cockpit/server.py`
- Modify: `src/cockpit/static/aero_mirror.js`
- Modify: `src/cockpit/static/cockpit.js`
- Modify: `src/cockpit/static/req_panel.js`
- Modify: `src/cockpit/static/why_panel.js`
- Modify: `src/cockpit/static/index.html`
- Modify: `tests/test_requirements_api.py`
- Create: `tests/js/atmosphere_parity.mjs`

**Interfaces:**
- Bootstrap adds `atmosphere_table` and resolved `derived.atmosphere`.
- `resolveAtmosphere(altitudeM, tablePayload)` mirrors Python interpolation.
- Aero contexts become `{atmosphere, v, w, launch}`.

- [ ] **Step 1: Add failing API assertions for table metadata/rows and resolved atmosphere**

- [ ] **Step 2: Add a Node parity script that consumes JSON fixtures from Python and fails until `atmosphere.js` exists**

```javascript
for (const fixture of fixtures) {
  const actual = resolveAtmosphere(fixture.altitude_m, fixture.table);
  assert.ok(Math.abs(actual.density_kgm3 - fixture.expected.density_kgm3) < 1e-12);
  assert.ok(Math.abs(actual.kinematic_viscosity_m2s -
                     actual.dynamic_viscosity_kgms / actual.density_kgm3) < 1e-15);
}
```

- [ ] **Step 3: Serve `atmosphere_table_payload()` and use `atmosphere_at()` in instant derivation and explanation endpoints**

- [ ] **Step 4: Implement the JS lookup and change the aero mirror, mission sizing, requirements panel, and Why panel to use it**

- [ ] **Step 5: Display resolved temperature, pressure, density, `mu`, and `nu` in the mission-derived panel without changing geometry controls**

- [ ] **Step 6: Run Python API tests and Node parity tests; preserve and update existing asset query versions deliberately**

- [ ] **Step 7: Commit only the atmosphere-related hunks, preserving pre-existing cockpit edits**

---

### Task 6: Deterministic OpenFOAM Flow Contract and Viscosity Gate

**Files:**
- Create: `src/services/flow_conditions.py`
- Modify: `src/services/simulate.py`
- Modify: `src/mcp/fastmcp_server.py`
- Create: `tests/test_flow_conditions.py`

**Interfaces:**
- `build_flow_contract(derived, reference_length_m, reference_area_m2) -> FlowConditions`.
- `write_flow_contract(path, contract)` writes `flow_conditions.json`.
- `enforce_openfoam_viscosity(case_dir, contract)` writes and reads back `constant/physicalProperties`.
- `validate_openfoam_viscosity(case_dir, contract)` raises `FlowConditionMismatch` on value/dimension disagreement.

- [ ] **Step 1: Write failing round-trip and deliberate-mismatch tests using a temporary Foundation v10 dictionary**

```python
def test_enforcement_round_trip(tmp_path, contract):
    path = tmp_path / "constant" / "physicalProperties"
    path.parent.mkdir()
    path.write_text("FoamFile { version 2.0; }\nnu [0 2 -1 0 0 0 0] 9e-5;\n")
    enforce_openfoam_viscosity(str(tmp_path), contract)
    assert validate_openfoam_viscosity(str(tmp_path), contract) == pytest.approx(
        contract.atmosphere.kinematic_viscosity_m2s)

def test_validator_rejects_wrong_nu(tmp_path, contract):
    path = tmp_path / "constant" / "physicalProperties"
    path.parent.mkdir()
    path.write_text("FoamFile { version 2.0; }\nnu [0 2 -1 0 0 0 0] 9e-5;\n")
    with pytest.raises(FlowConditionMismatch):
        validate_openfoam_viscosity(str(tmp_path), contract)
```

- [ ] **Step 2: Run the focused tests and confirm missing-service failures**

- [ ] **Step 3: Implement Pydantic contract models, atomic JSON writing, a narrowly scoped `nu` entry parser/updater, and strict dimensions/value validation**

- [ ] **Step 4: Extend `compose_requirement` with the explicit contracted values for guidance while keeping deterministic enforcement authoritative**

- [ ] **Step 5: In the stage-5 MCP path, write `flow_conditions.json`, run the inherited graph, enforce/read back `physicalProperties`, and fail the stage on mismatch before recording success**

- [ ] **Step 6: Run contract tests and simulation service tests**

- [ ] **Step 7: Commit the OpenFOAM gate**

---

### Task 7: Evaluation and Artifact Provenance

**Files:**
- Modify: `src/services/evaluate.py`
- Modify: `src/mcp/fastmcp_server.py`
- Modify: `src/services/design_session.py`
- Create: `tests/test_atmosphere_artifacts.py`

**Interfaces:**
- `evaluate_case(case_dir: str, outdir: str, required_cl: float, vlm_cl: float, extra_slices: list[str] | None, ref: dict, flow_conditions: dict | None = None) -> dict` records flow provenance.
- Stage summaries/manifests include atmosphere source and checksum while inventory hashing remains unchanged.

- [ ] **Step 1: Add failing tests that evaluation records viscosity beside y+ and manifests retain the atmosphere checksum**

- [ ] **Step 2: Run focused tests and confirm the fields are absent**

- [ ] **Step 3: Load the simulation flow contract in `evaluate_results` and pass it into `evaluate_case`**

- [ ] **Step 4: Add flow conditions to evaluation output and y+ provenance without changing existing verdict thresholds**

- [ ] **Step 5: Add source/checksum to relevant stage summaries and run manifest/evaluation tests**

- [ ] **Step 6: Commit artifact provenance**

---

### Task 8: Repository Audit and Final Verification

**Files:**
- Modify: `verification/verify_golden.py`
- Modify: `verification/verify_revisions.py`
- Modify: `reports/verification-2026-07-10/references.md`
- Modify: affected tests discovered by the audit

**Interfaces:**
- No production BWB path retains `MU_AIR`, `1.81e-5`, `1.48e-5`, or its own ISA density formula.
- Benchmark prompts and unrelated inherited FoamBench fixtures remain unchanged.

- [ ] **Step 1: Run the production-code constant audit**

```powershell
rg -n "MU_AIR|1\.81e-5|1\.48e-5|2\.25577e-5|kinematic viscosity" domain_packs/bwb src/cockpit src/services src/mcp verification tests
```

- [ ] **Step 2: Remove or justify every production hit and update source accounting**

- [ ] **Step 3: Run `python -m pytest tests -q`**

- [ ] **Step 4: Run `python verification/verify_golden.py --no-llm` and confirm geometry gates and the atmosphere assertions pass**

- [ ] **Step 5: Run `python verification/doctor.py`; execute environment-dependent VSPAERO/OpenFOAM checks only when their runtimes are reported available**

- [ ] **Step 6: Run `git diff --check`, inspect the complete diff, and confirm unrelated dirty files were not staged**

- [ ] **Step 7: Commit final verification and documentation updates**
