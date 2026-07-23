/* Requirements panel — the explorer's primary inputs.
 *
 * Edit a requirement -> debounced POST /api/requirements {persist:false} ->
 * the solved geometry morphs the preview mesh live. Releasing the control
 * commits {persist:true}. The inverse solver lives SERVER-SIDE ONLY
 * (domain_packs/bwb/inverse.py) — this file has no sizing math of its own,
 * so the cockpit and chat can never disagree about what requirements mean.
 * The only client math is the aero_mirror forward estimate used to refresh
 * "achieved" readouts instantly while the user drags a geometry slider.
 */
import * as ATMOSPHERE from './atmosphere.js?v=atm1';
import * as AERO from './aero_mirror.js?v=atm1';

/* Presentation only — validity bounds come from the served
 * DesignRequirements json-schema (S.reqSchema), i.e. the pydantic Field
 * constraints. uiMin may NARROW a uselessly loose schema floor (mtow gt=0,
 * ld gt=0) for slider ergonomics; it can never widen the schema range. */
const REQ_META = {
  mtow_kg:          { label: 'Takeoff mass', unit: 'kg', step: 0.1, dp: 1, uiMin: 0.5,
                      kind: 'flight', hint: 'the weight the wing must carry' },
  cruise_speed_ms:  { label: 'Cruise speed', unit: 'm/s', step: 0.5, dp: 1,
                      kind: 'flight', hint: 'sets dynamic pressure q = ½ρV²' },
  cl_cruise_target: { label: 'Cruise CL =', unit: '', step: 0.01, dp: 2,
                      kind: 'target', hint: 'equality: sets wing area S = W/(q·CL). Off = size to best-L/D CL*' },
  ld_min:           { label: 'Cruise L/D ≥', unit: '', step: 0.5, dp: 1, uiMin: 5,
                      kind: 'target', hint: 'objective: solver raises aspect ratio until met or a limit binds' },
  v_stall_max_ms:   { label: 'Stall speed ≤', unit: 'm/s', step: 0.1, dp: 1,
                      kind: 'target', hint: 'hard: floors the wing area (launch method also caps this)' },
  span_max_m:       { label: 'Span ≤', unit: 'm', step: 0.05, dp: 2,
                      kind: 'target', hint: 'hard: transport / handling limit' },
};

/* Optional pydantic fields arrive as anyOf [{number…}, {null}]; exclusive
 * bounds are nudged by one step so the slider can never submit a value the
 * server would reject. */
function schemaBounds(key, meta) {
  const prop = (C.S.reqSchema || {})[key] || {};
  const facet = prop.anyOf ? (prop.anyOf.find(p => p.type === 'number') || {}) : prop;
  let min = facet.minimum != null ? facet.minimum
    : facet.exclusiveMinimum != null ? facet.exclusiveMinimum + meta.step : meta.uiMin;
  let max = facet.maximum != null ? facet.maximum
    : facet.exclusiveMaximum != null ? facet.exclusiveMaximum - meta.step : meta.uiMax;
  if (meta.uiMin != null) min = Math.max(min, meta.uiMin);
  if (meta.uiMax != null) max = Math.min(max, meta.uiMax);
  return { min: +min.toFixed(6), max: +max.toFixed(6) };  // strip float noise
}

function metTolerances() {
  return (C.S.reqMeta && C.S.reqMeta.tolerances) || {};
}
const STATUS_FIELD = { cl_cruise_target: 'cl', ld_min: 'ldCruise',
                       v_stall_max_ms: 'vStall', span_max_m: 'span' };

let C = null;              // context injected by cockpit.js: {S, toast, syncAll}
let els = {};              // key -> {row, check, range, num}
let lastSolve = null;      // last server response
let debounceTimer = null;

export function flightCtx() {
  const r = C.S.requirements;
  const atmosphere = ATMOSPHERE.resolveAtmosphere(
    r.cruise_altitude_m, C.S.atmosphereTable);
  return { atmosphere, v: +r.cruise_speed_ms,
    w: +r.mtow_kg * 9.81, launch: r.launch || 'hand' };
}

function collect() {
  const out = { cruise_altitude_m: C.S.requirements.cruise_altitude_m,
                launch: document.getElementById('req-launch').value };
  for (const [key, meta] of Object.entries(REQ_META)) {
    const e = els[key];
    if (meta.kind === 'flight') out[key] = +e.num.value;
    else out[key] = e.check.checked ? +e.num.value : null;
  }
  return out;
}

function requestSolve(persist) {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(() => doSolve(persist), persist ? 0 : 120);
}

async function doSolve(persist) {
  let r, d;
  try {
    r = await fetch('/api/requirements', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: C.S.sid, requirements: collect(),
                             keep_shape: true, persist }) });
    d = await r.json();
  } catch (e) { C.toast('err', `Requirements solve failed: ${e.message}`); return; }
  if (!r.ok) {
    (Array.isArray(d.detail) ? d.detail : [{ message: String(d.detail) }])
      .forEach(x => C.toast('err', `Requirement rejected: ${x.message}`));
    return;
  }
  lastSolve = d;
  C.S.requirements = { ...d.requirements };
  C.S.params = { ...d.proposed_params };
  renderStatus(d.statuses, d.binding_constraint, d.binding_explanation, d.notes);
  C.syncAll();
  if (persist) {
    C.toast('ok', d.binding_constraint
      ? `Requirements saved (v${d.requirements_version}). Solver stopped at: ${d.binding_explanation}.`
      : `Requirements saved (v${d.requirements_version}) — all targets met by the solved geometry. Apply to rebuild the STL.`);
  }
}

function chip(label, cls, title = '') {
  return `<span class="req-chip ${cls}" title="${title}">${label}</span>`;
}

function renderStatus(statuses, binding, explanation, notes) {
  const chips = document.getElementById('req-chips');
  chips.innerHTML = statuses.map(s => {
    if (s.met == null) return chip(`${s.label} ${fmtN(s.achieved)}${s.unit ? ' ' + s.unit : ''}`,
      'info', 'no requirement set — achieved value shown');
    const mark = s.met ? '✓' : '✗';
    const cls = s.met ? 'met' : 'unmet';
    return chip(`${mark} ${s.label} ${fmtN(s.achieved)}${s.unit ? ' ' + s.unit : ''} (target ${fmtN(s.target)})`,
      cls, s.margin != null ? `margin ${fmtN(s.margin)}` : '');
  }).join('');
  const banner = document.getElementById('binding-banner');
  if (!binding) {
    banner.className = 'ok';
    banner.innerHTML = `<div class="bhead">All requirements met</div>
      The solved geometry satisfies every target you set. Apply to rebuild the STL.`;
  } else {
    banner.className = '';
    banner.innerHTML = `<div class="bhead">Binding limit: ${binding.replaceAll('_', ' ')}</div>${explanation}`;
  }
  document.getElementById('req-notes').innerHTML =
    (notes && notes.length) ? '◈ ' + notes.join('<br>◈ ') : '';
  // reflect met/unmet on the rows too
  for (const s of statuses) {
    const e = els[s.name];
    if (e) e.row.classList.toggle('unmet', s.met === false);
  }
}

/* Called by cockpit.js syncAll: the user may have dragged a GEOMETRY slider —
 * refresh the achieved values instantly (client mirror), flag manual mode. */
export function refreshAchieved() {
  if (!C || !C.S.requirements) return;
  let est;
  try { est = AERO.estimate(C.S.params, flightCtx()); } catch { return; }
  for (const [key, field] of Object.entries(STATUS_FIELD)) {
    const e = els[key];
    if (!e) continue;
    const target = e.check.checked ? +e.num.value : null;
    const v = est[field];
    let met = null;
    if (target != null) {
      const tol = metTolerances();  // solver-owned (inverse.MET_TOLERANCES)
      if (key === 'ld_min') met = v >= target - (tol.ld_min || 0);
      else if (key === 'cl_cruise_target') met = Math.abs(v - target) <= (tol.cl_cruise_target_rel || 0) * target;
      else met = v <= target + (tol[key] || 0);
    }
    e.row.querySelector('.req-achieved').innerHTML =
      `achieved <span class="${met === false ? 'bad' : 'ok'}">${fmtN(v)}${REQ_META[key].unit ? ' ' + REQ_META[key].unit : ''}</span>`
      + (met === false ? ' — unmet' : '');
    e.row.classList.toggle('unmet', met === false);
  }
}

export function renderFromState() {
  if (!C || !C.S.requirements) return;
  const r = C.S.requirements;
  for (const [key, meta] of Object.entries(REQ_META)) {
    const e = els[key];
    if (!e) continue;
    const v = r[key];
    if (meta.kind === 'flight') {
      if (v != null) { e.range.value = v; e.num.value = (+v).toFixed(meta.dp); }
    } else {
      e.check.checked = v != null;
      e.row.classList.toggle('off', v == null);
      e.range.disabled = e.num.disabled = (v == null);
      if (v != null) { e.range.value = v; e.num.value = (+v).toFixed(meta.dp); }
    }
  }
  const ls = document.getElementById('req-launch');
  if (ls) ls.value = r.launch || 'hand';
  refreshAchieved();
}

function fmtN(v) {
  if (v == null) return '—';
  const a = Math.abs(v);
  return a >= 100 ? v.toFixed(0) : a >= 10 ? v.toFixed(1) : a >= 1 ? v.toFixed(2) : v.toFixed(3);
}

function buildRow(key, meta) {
  const { min, max } = schemaBounds(key, meta);
  const div = document.createElement('div');
  div.className = 'req-row' + (meta.kind === 'target' ? ' off' : '');
  div.innerHTML = `
    <div class="req-head">
      ${meta.kind === 'target' ? '<input type="checkbox" title="Enable this requirement">' : ''}
      <label>${meta.label}</label><span class="unit">${meta.unit}</span>
    </div>
    <div class="ctl-row">
      <input type="range" min="${min}" max="${max}" step="${meta.step}">
      <input type="number" min="${min}" max="${max}" step="${meta.step}">
    </div>
    ${meta.kind === 'target' ? '<div class="req-achieved"></div>' : ''}
    <div class="req-hint">${meta.hint}</div>`;
  const range = div.querySelector('input[type=range]');
  const num = div.querySelector('input[type=number]');
  const check = div.querySelector('input[type=checkbox]');

  range.addEventListener('input', () => { num.value = (+range.value).toFixed(meta.dp); requestSolve(false); });
  range.addEventListener('change', () => requestSolve(true));
  num.addEventListener('change', () => {
    let v = +num.value;
    if (Number.isNaN(v)) { C.toast('warn', `${meta.label}: not a number — reverted.`); renderFromState(); return; }
    v = Math.min(max, Math.max(min, v));
    num.value = v.toFixed(meta.dp); range.value = v;
    requestSolve(true);
  });
  if (check) check.addEventListener('change', () => {
    div.classList.toggle('off', !check.checked);
    range.disabled = num.disabled = !check.checked;
    if (check.checked && num.value === '') {
      // seed from the currently achieved value so enabling is non-destructive
      try {
        const est = AERO.estimate(C.S.params, flightCtx());
        const seed = est[STATUS_FIELD[key]];
        num.value = (+seed).toFixed(meta.dp); range.value = +seed;
      } catch { num.value = ((min + max) / 2).toFixed(meta.dp); }
    }
    requestSolve(true);
  });
  els[key] = { row: div, check, range, num };
  return div;
}

export function init(context) {
  C = context;
  const pane = document.getElementById('req-panel');
  pane.innerHTML = `<div class="group-title" style="margin-top:2px">Requirements
    <span class="badge instant" style="margin-left:4px">drives the geometry</span></div>`;

  const flightKeys = Object.entries(REQ_META).filter(([, m]) => m.kind === 'flight');
  const targetKeys = Object.entries(REQ_META).filter(([, m]) => m.kind === 'target');
  for (const [k, m] of flightKeys) pane.appendChild(buildRow(k, m));

  // per-method stall caps come from the server (aero_estimates.LAUNCH_VSTALL_MAX)
  const LAUNCH_LABELS = { hand: 'Hand launch', catapult: 'Catapult', runway: 'Runway' };
  const caps = (C.S.reqMeta && C.S.reqMeta.launch_vstall_max) || {};
  const opts = (Object.keys(caps).length ? Object.entries(caps) : [['hand'], ['catapult'], ['runway']])
    .map(([k, v]) => `<option value="${k}">${LAUNCH_LABELS[k] || k}${v != null ? ` (stall ≤ ${v} m/s)` : ''}</option>`)
    .join('');
  const launch = document.createElement('div');
  launch.className = 'req-row';
  launch.innerHTML = `
    <div class="req-head"><label>Launch method</label></div>
    <div class="ctl-row"><select id="req-launch" style="flex:1">${opts}</select></div>
    <div class="req-hint">hard stall tolerance — the tightest limit of this and your stall requirement wins</div>`;
  launch.querySelector('select').addEventListener('change', () => requestSolve(true));
  pane.appendChild(launch);

  const tt = document.createElement('div');
  tt.className = 'group-title';
  tt.textContent = 'Targets (enable what you care about)';
  pane.appendChild(tt);
  for (const [k, m] of targetKeys) pane.appendChild(buildRow(k, m));

  const status = document.createElement('div');
  status.innerHTML = `<div id="req-chips"></div><div id="binding-banner" class="ok"></div>
    <div id="req-notes" class="req-notes"></div>
    <button id="btn-resolve" class="secondary" title="Re-run the inverse solver from the saved requirements (discards manual geometry edits)">↺ Re-solve geometry from requirements</button>`;
  pane.appendChild(status);
  document.getElementById('btn-resolve').addEventListener('click', () => doSolve(true));

  renderFromState();
  initialStatus();
}

/* On boot, do NOT solve (that would replace the loaded design with a fresh
 * sizing): show how the CURRENT geometry measures against the saved
 * requirements, using the server-computed status from bootstrap. */
function initialStatus() {
  const rs = C.S.reqStatus;
  const banner = document.getElementById('binding-banner');
  if (rs && rs.statuses && rs.statuses.length) {
    renderStatus(rs.statuses, null, '', []);
    if (rs.unmet && rs.unmet.length) {
      banner.className = '';
      banner.innerHTML = `<div class="bhead">Current geometry misses ${rs.unmet.length} requirement(s)</div>
        Drag a requirement or press Re-solve to re-fit the wing; then Apply to rebuild the STL.`;
    } else {
      banner.className = 'ok';
      banner.innerHTML = `<div class="bhead">Requirements-first explorer</div>
        Set a target and the wing re-sizes live. The geometry is the consequence.`;
    }
  } else {
    banner.className = 'ok';
    banner.innerHTML = `<div class="bhead">Requirements-first explorer</div>
      Set a target and the wing re-sizes live. The geometry is the consequence.`;
  }
}
