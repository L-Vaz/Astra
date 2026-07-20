/* Why panel — first-principles drill-down.
 *
 * Every metric row is clickable: expanding it fetches /api/explain (cached,
 * debounced) and renders the SERVER-COMPUTED derivation graph — formula,
 * the user's actual numbers substituted, and a source badge on every node
 * (textbook method / declared engineering default / input) — plus finite-
 * difference sensitivities: what one notch of each control does to this
 * metric. No physics is computed here beyond the aero_mirror instant values
 * shown in the collapsed rows; the tree itself always comes from Python.
 */
import * as AERO from './aero_mirror.js?v=atm1';
import { flightCtx } from './req_panel.js?v=atm1';

const METRIC_ROWS = [
  { id: 'ld_cruise',        label: 'L/D at cruise',    get: e => e.ldCruise,    dp: 1 },
  { id: 'cd_cruise',        label: 'CD at cruise',     get: e => e.cd,          dp: 4 },
  { id: 'cd0',              label: 'Parasite CD₀',     get: e => e.cd0,         dp: 4 },
  { id: 'cl_cruise',        label: 'Cruise CL',        get: e => e.cl,          dp: 3 },
  { id: 'cl_star',          label: 'Best-L/D CL*',     get: e => e.clStar,      dp: 3 },
  { id: 'cl_max_3d',        label: 'CLmax (3D)',       get: e => e.clMax3d,     dp: 2 },
  { id: 'v_stall_ms',       label: 'Stall speed',      get: e => e.vStall,      dp: 1, unit: ' m/s' },
  { id: 'oswald_e',         label: 'Oswald e',         get: e => e.e,           dp: 3 },
  { id: 'x_np_m',           label: 'Neutral point x',  get: e => e.xNp,         dp: 3, unit: ' m' },
  { id: 'wing_loading_kgm2', label: 'Wing loading',    get: e => e.wingLoading, dp: 1, unit: ' kg/m²' },
];
const SENS_PARAM_LABELS = {
  span_m: 'span', root_chord_m: 'root chord', centre_body_fraction: 'centre-body fraction',
  mid_chord_frac: 'mid-chord fraction', taper_outer: 'outer taper',
  sweep_inner_deg: 'inner sweep', sweep_outer_deg: 'outer sweep',
  root_thickness_pct: 'root thickness', tip_thickness_pct: 'tip thickness',
  root_twist_deg: 'root twist', tip_twist_deg: 'tip twist (washout)', dihedral_deg: 'dihedral',
};
const REQ_PARAM_LABELS = {
  cruise_speed_ms: 'cruise speed', mtow_kg: 'takeoff mass', cruise_altitude_m: 'altitude',
};

let C = null;
let expanded = new Set();
let explainData = null;      // {derivation, sensitivity, req_sensitivity}
let explainKey = '';         // params+flight hash the data belongs to
let fetchTimer = null;
let fetching = false;

export function init(context) {
  C = context;
  refresh();
}

function stateKey() {
  const r = C.S.requirements || {};
  return JSON.stringify([C.S.params, r.mtow_kg, r.cruise_speed_ms,
                         r.cruise_altitude_m, r.launch]);
}

async function fetchExplain() {
  if (fetching) return;
  const key = stateKey();
  if (key === explainKey && explainData) { renderBodies(); return; }
  fetching = true;
  try {
    const r = C.S.requirements;
    const resp = await fetch('/api/explain', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        params: C.S.params,
        flight: { mtow_kg: r.mtow_kg, cruise_speed_ms: r.cruise_speed_ms,
                  cruise_altitude_m: r.cruise_altitude_m, launch: r.launch },
        include: ['derivation', 'sensitivity', 'req_sensitivity'] }) });
    if (resp.ok) {
      explainData = await resp.json();
      explainKey = key;
    } else {
      explainData = null;  // invalid intermediate geometry — trees unavailable
    }
  } catch { explainData = null; }
  finally { fetching = false; }
  renderBodies();
}

function scheduleExplain() {
  clearTimeout(fetchTimer);
  fetchTimer = setTimeout(fetchExplain, 250);
}

/* Rebuild the row list (headers always; bodies when expanded). Called from
 * cockpit.js syncAll on every edit. */
export function refresh() {
  const el = document.getElementById('why-list');
  if (!el || !C) return;
  let est = null, base = null;
  try { est = AERO.estimate(C.S.params, flightCtx()); } catch { /* invalid geometry */ }
  if (C.S.applied) {
    try { base = AERO.estimate(C.S.applied, flightCtx()); } catch { }
  }
  if (!est) {
    el.innerHTML = '<div class="dim">geometry invalid — fix validation issues to see the physics</div>';
    return;
  }
  if (!el.dataset.built) {
    el.dataset.built = '1';
    el.innerHTML = METRIC_ROWS.map(m => `
      <div class="why-row" id="why-${m.id}">
        <div class="why-head" title="click to expand the derivation">
          <span class="caret">▸</span><span class="wlabel">${m.label}</span>
          <span class="wdelta"></span><span class="wval"></span>
        </div>
        <div class="why-body" style="display:none"></div>
      </div>`).join('');
    for (const m of METRIC_ROWS) {
      document.querySelector(`#why-${m.id} .why-head`)
        .addEventListener('click', () => toggle(m.id));
    }
  }
  for (const m of METRIC_ROWS) {
    const row = document.getElementById(`why-${m.id}`);
    const v = m.get(est);
    row.querySelector('.wval').textContent = v.toFixed(m.dp) + (m.unit || '');
    const d = base ? v - m.get(base) : 0;
    row.querySelector('.wdelta').textContent =
      base && Math.abs(d) > Math.abs(m.get(base)) * 1e-4 + 1e-9
        ? `${d > 0 ? '+' : ''}${d.toFixed(m.dp)}` : '';
  }
  if (expanded.size) scheduleExplain();
}

function toggle(id) {
  const row = document.getElementById(`why-${id}`);
  const body = row.querySelector('.why-body');
  const open = expanded.has(id);
  if (open) {
    expanded.delete(id);
    row.classList.remove('open');
    row.querySelector('.caret').textContent = '▸';
    body.style.display = 'none';
  } else {
    expanded.add(id);
    row.classList.add('open');
    row.querySelector('.caret').textContent = '▾';
    body.style.display = '';
    body.innerHTML = '<div class="dim">deriving…</div>';
    fetchExplain();
  }
}

function renderBodies() {
  for (const id of expanded) {
    const body = document.querySelector(`#why-${id} .why-body`);
    if (!body) continue;
    if (!explainData || !explainData.derivation) {
      body.innerHTML = '<div class="dim">derivation unavailable for this (invalid) geometry</div>';
      continue;
    }
    body.innerHTML = nodeHtml(id, 0) + sensHtml(id);
  }
}

function fmtVal(n) {
  const a = Math.abs(n.value);
  const s = (a !== 0 && (a >= 1e5 || a < 1e-3)) ? n.value.toExponential(2)
    : n.value.toFixed(a >= 100 ? 0 : a >= 10 ? 1 : a >= 1 ? 2 : 4);
  return s + (n.unit ? ' ' + n.unit : '');
}

function nodeHtml(id, depth) {
  const n = explainData.derivation.nodes[id];
  if (!n) return '';
  if (depth > 8) return '';
  const leaf = !n.children.length;
  const kids = leaf ? '' : n.children.map(c => nodeHtml(c, depth + 1)).join('');
  return `
    <details class="dnode ${leaf ? 'leaf' : ''}" ${depth === 0 ? 'open' : ''}>
      <summary><span class="dsym">${n.symbol}</span> ${n.label}
        <span class="dval">${fmtVal(n)}</span></summary>
      ${n.formula ? `<div class="dformula">${n.formula}</div>` : ''}
      ${n.substitution ? `<div class="dsubs">${n.substitution}</div>` : ''}
      <div class="dsource ${n.kind}">${n.kind === 'constant' ? 'constant — ' : n.kind === 'input' ? 'input — ' : ''}${n.source}</div>
      ${kids}
    </details>`;
}

function sensHtml(metric) {
  if (!explainData.sensitivity) return '';
  const fmt = (d) => {
    const a = Math.abs(d);
    if (a === 0) return '0';
    const s = a >= 100 ? d.toFixed(0) : a >= 1 ? d.toFixed(2) : a >= 0.001 ? d.toFixed(4) : d.toExponential(1);
    return (d > 0 ? '+' : '') + s;
  };
  const rows = explainData.sensitivity
    .map(r => ({ r, d: r.deltas[metric] ?? 0 }))
    .filter(x => Math.abs(x.d) > 0)
    .sort((a, b) => Math.abs(b.d) - Math.abs(a.d))
    .slice(0, 6)
    .map(({ r, d }) => `<div class="sens-line">
      <span class="sk">+${r.step}${r.step_unit ? ' ' + r.step_unit : ''} ${SENS_PARAM_LABELS[r.param] || r.param}${r.clamped ? ' *' : ''}</span>
      <span class="sv">${fmt(d)}</span></div>`).join('');
  const reqRows = (explainData.req_sensitivity || [])
    .map(r => ({ r, d: r.deltas[metric] ?? 0 }))
    .filter(x => Math.abs(x.d) > 0)
    .map(({ r, d }) => `<div class="sens-line req">
      <span class="sk">+${r.step}${r.step_unit ? ' ' + r.step_unit : ''} ${REQ_PARAM_LABELS[r.param] || r.param}</span>
      <span class="sv">${fmt(d)}</span></div>`).join('');
  return `
    <div class="sens-block">
      <div class="sens-title">what moves this number (per control notch)</div>
      ${rows || '<div class="dim">no geometric lever affects it</div>'}
      ${reqRows ? `<div class="sens-title" style="margin-top:6px">…and the flight condition</div>${reqRows}` : ''}
      <div class="dim" style="font-size:10px;margin-top:4px">central finite differences on the closed-form panel; * = one-sided at a schema bound</div>
    </div>`;
}
