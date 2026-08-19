/* Stage 1 — Airfoil Sets.
 *
 * This module owns only the deterministic Requirements -> Airfoil sets -> Concepts
 * gate.  It deliberately exposes one navigation event and callback so another screen
 * can observe the transition without this module knowing or querying that screen's
 * DOM.
 */

export const AIRFOIL_CONTINUE_EVENT = 'astra:stage1-airfoil-continued';

const STRATEGIES = ['balanced', 'efficiency', 'low_speed_control'];
const STATIONS = ['root', 'mid', 'tip'];
const LABELS = {
  balanced: 'Balanced',
  efficiency: 'Efficiency',
  low_speed_control: 'Low-speed / control',
};

const byId = (id) => document.getElementById(id);
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (character) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[character]));

function storageKey(sessionId) {
  return `astra.airfoil-selection.${sessionId}`;
}

function compact(value, digits = 3) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(digits) : '—';
}

function chart(canvas, points, xKey, yKey, colour = '#4da3ff') {
  if (!canvas || !points?.length) return;
  const scale = window.devicePixelRatio || 1;
  const width = Math.max(240, canvas.clientWidth || 320);
  const height = Math.max(130, canvas.clientHeight || 160);
  canvas.width = width * scale;
  canvas.height = height * scale;
  const ctx = canvas.getContext('2d');
  ctx.scale(scale, scale);
  ctx.clearRect(0, 0, width, height);
  const margin = { left: 42, right: 12, top: 12, bottom: 30 };
  const xs = points.map((point) => Number(point[xKey]));
  const ys = points.map((point) => Number(point[yKey]));
  let xmin = Math.min(...xs); let xmax = Math.max(...xs);
  let ymin = Math.min(...ys); let ymax = Math.max(...ys);
  if (xmin === xmax) { xmin -= 1; xmax += 1; }
  if (ymin === ymax) { ymin -= 1; ymax += 1; }
  const px = (x) => margin.left + (x - xmin) / (xmax - xmin)
    * (width - margin.left - margin.right);
  const py = (y) => height - margin.bottom - (y - ymin) / (ymax - ymin)
    * (height - margin.top - margin.bottom);
  ctx.strokeStyle = '#405064'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(margin.left, margin.top);
  ctx.lineTo(margin.left, height - margin.bottom);
  ctx.lineTo(width - margin.right, height - margin.bottom); ctx.stroke();
  ctx.fillStyle = '#8fa0b3'; ctx.font = '11px Segoe UI';
  ctx.fillText(compact(ymax, 2), 3, margin.top + 4);
  ctx.fillText(compact(ymin, 2), 3, height - margin.bottom + 4);
  ctx.fillText(compact(xmin, 2), margin.left, height - 8);
  ctx.fillText(compact(xmax, 2), width - margin.right - 28, height - 8);
  ctx.strokeStyle = colour; ctx.lineWidth = 2; ctx.beginPath();
  points.forEach((point, index) => {
    const x = px(Number(point[xKey])); const y = py(Number(point[yKey]));
    if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function drawOutline(canvas, coordinates) {
  const points = (coordinates || []).map(([x, y]) => ({ x, y }));
  chart(canvas, points, 'x', 'y', '#57c07a');
}

function summary(card) {
  const metrics = card.metrics || {};
  return [
    `profile CD ${compact(metrics.cd_profile_sections, 4)}`,
    `stall ${compact(metrics.v_stall_ms, 1)} m/s`,
    `trim ${compact(metrics.cm_trim_residual, 4)}`,
  ].join(' · ');
}

export function createAirfoilSetsController({ THREE, api, sessionId, onContinue }) {
  const state = {
    sessionId,
    recommendation: null,
    selection: null,
    activeStrategy: 'balanced',
    activeStation: 'root',
    details: new Map(),
    loft: null,
  };

  function setBusy(isBusy, message = 'Computing coordinated schedules…') {
    const loading = byId('airfoil-loading');
    if (!loading) return;
    loading.classList.toggle('hidden', !isBusy);
    loading.textContent = message;
    byId('screen-airfoils')?.setAttribute('aria-busy', String(isBusy));
  }

  function showError(error) {
    const holder = byId('airfoil-error');
    if (!holder) return;
    holder.textContent = error ? String(error.message || error) : '';
    holder.classList.toggle('hidden', !error);
  }

  function selectedHash() {
    return state.selection?.schedule_hash
      || localStorage.getItem(storageKey(state.sessionId));
  }

  function renderCards() {
    const recommendation = state.recommendation;
    const cards = byId('airfoil-cards');
    if (!recommendation || !cards) return;
    const chosenHash = selectedHash();
    cards.innerHTML = recommendation.strategies.map((card) => {
      const selected = chosenHash === card.schedule_hash;
      const recommended = recommendation.mission_badge === card.strategy;
      const stationRows = STATIONS.map((station) => {
        const detail = card.stations?.[station] || {};
        return `<button class="airfoil-station" data-strategy="${card.strategy}"
          data-station="${station}" aria-label="Inspect ${station} ${esc(card.schedule[station])}">
          <span>${station}</span><strong>${esc(card.schedule[station])}</strong>
          <small>Re ${esc(detail.reynolds ? Number(detail.reynolds).toExponential(2) : '—')}
          · t/c ${compact(detail.tc, 3)}</small></button>`;
      }).join('');
      return `<article class="airfoil-card${selected ? ' selected' : ''}"
          data-strategy-card="${card.strategy}">
        <header><div><p class="microlabel">${esc(LABELS[card.strategy])}</p>
          <h2>${esc(LABELS[card.strategy])}</h2></div>
          ${recommended ? '<span class="airfoil-mission-badge">Mission match</span>' : ''}</header>
        <p class="airfoil-summary">${esc(summary(card))}</p>
        <div class="airfoil-stations">${stationRows}</div>
        <p class="airfoil-ranking">${esc(card.ranking?.method || '')}</p>
        <button class="secondary airfoil-select" data-select="${card.schedule_hash}">
          ${selected ? 'Selected' : 'Select this set'}</button>
      </article>`;
    }).join('');

    cards.querySelectorAll('.airfoil-select').forEach((button) => {
      button.addEventListener('click', () => selectSchedule(button.dataset.select));
    });
    cards.querySelectorAll('.airfoil-station').forEach((button) => {
      button.addEventListener('click', () => openDetail(
        button.dataset.strategy, button.dataset.station,
      ));
    });

    const stale = Boolean(recommendation.stale);
    byId('airfoil-stale')?.classList.toggle('hidden', !stale);
    const continueButton = byId('btn-airfoil-continue');
    if (continueButton) continueButton.disabled = stale || !chosenHash;
    const selectedCard = recommendation.strategies.find(
      (card) => card.schedule_hash === chosenHash,
    );
    if (selectedCard && !stale) void renderLoft(selectedCard);
  }

  async function load() {
    setBusy(true, 'Restoring Airfoil Sets…'); showError(null);
    try {
      let payload;
      try {
        payload = await api(`/api/stage1/airfoils?session=${encodeURIComponent(state.sessionId)}`);
      } catch (_missing) {
        payload = await api('/api/stage1/airfoils/recommend', {
          session_id: state.sessionId,
        });
      }
      state.recommendation = payload;
      state.selection = payload.selection || null;
      if (state.selection && !state.selection.stale) {
        localStorage.setItem(storageKey(state.sessionId), state.selection.schedule_hash);
      }
      renderCards();
    } catch (error) {
      showError(error);
    } finally {
      setBusy(false);
    }
  }

  async function recompute() {
    setBusy(true); showError(null);
    try {
      state.recommendation = await api('/api/stage1/airfoils/recommend', {
        session_id: state.sessionId,
      });
      state.selection = null;
      localStorage.removeItem(storageKey(state.sessionId));
      renderCards();
    } catch (error) { showError(error); } finally { setBusy(false); }
  }

  async function selectSchedule(scheduleHash) {
    setBusy(true, 'Approving exact root / mid / tip set…'); showError(null);
    try {
      state.selection = await api('/api/stage1/airfoils/select', {
        session_id: state.sessionId,
        schedule_hash: scheduleHash,
        recommendation_version: state.recommendation.version,
      });
      localStorage.setItem(storageKey(state.sessionId), scheduleHash);
      renderCards();
    } catch (error) { showError(error); } finally { setBusy(false); }
  }

  function renderDetail(detail) {
    drawOutline(byId('airfoil-outline'), detail.coordinates);
    chart(
      byId('airfoil-lift-graph'),
      detail.prestall_lift.map(([alpha, cl]) => ({ alpha, cl })),
      'alpha', 'cl', '#57c07a',
    );
    chart(byId('airfoil-drag-graph'), detail.cd_vs_cl, 'cl', 'cd', '#e0a83c');
    byId('airfoil-modal-title').textContent =
      `${detail.station} · ${detail.name}`;
    byId('airfoil-clmax').textContent =
      `CLmax ${compact(detail.clmax.value, 3)} at ${compact(detail.clmax.alpha_deg, 1)}°`;
    byId('airfoil-detail-metrics').innerHTML = [
      ['Cruise Re', Number(detail.reynolds).toExponential(3)],
      ['Low-speed Re', Number(detail.reynolds_low_speed).toExponential(3)],
      ['Thickness', compact(detail.thickness_ratio * 100, 2) + '%'],
      ['Cm0', compact(detail.cm0, 4)],
      ['Confidence', detail.confidence],
      ['Polar valid', detail.validity.polar_in_envelope ? 'yes' : 'no'],
    ].map(([label, value]) => `<dt>${esc(label)}</dt><dd>${esc(value)}</dd>`).join('');
    const experimental = detail.references?.experimental;
    byId('airfoil-evidence').innerHTML = `
      <p><b>Polar:</b> ${esc(detail.evidence.polar)}</p>
      <p><b>Coordinates:</b> ${esc(detail.evidence.coordinates)}</p>
      <p><b>Coordinate SHA-256:</b> <code>${esc(detail.coordinate_sha256)}</code></p>
      <p><b>Experimental reference:</b> ${experimental
        ? `<a href="${esc(experimental)}" target="_blank" rel="noreferrer">${esc(experimental)}</a>`
        : 'not located — confidence is weakened, not gated'}</p>`;
  }

  async function detailFor(strategy, station) {
    const key = `${strategy}:${station}:${state.recommendation.version}`;
    if (!state.details.has(key)) {
      state.details.set(key, await api(
        `/api/stage1/airfoils/detail?session=${encodeURIComponent(state.sessionId)}`
        + `&strategy=${encodeURIComponent(strategy)}&station=${station}`,
      ));
    }
    return state.details.get(key);
  }

  async function openDetail(strategy, station) {
    state.activeStrategy = strategy; state.activeStation = station;
    setBusy(true, `Loading ${station} section evidence…`); showError(null);
    try {
      const detail = await detailFor(strategy, station);
      renderDetail(detail);
      const modal = byId('airfoil-modal');
      modal.classList.remove('hidden');
      modal.setAttribute('aria-hidden', 'false');
      byId('btn-airfoil-modal-close').focus();
    } catch (error) { showError(error); } finally { setBusy(false); }
  }

  function closeDetail() {
    const modal = byId('airfoil-modal');
    modal?.classList.add('hidden');
    modal?.setAttribute('aria-hidden', 'true');
  }

  async function renderLoft(card) {
    if (!THREE || !byId('airfoil-loft-canvas')) return;
    try {
      const details = await Promise.all(STATIONS.map((station) =>
        detailFor(card.strategy, station)));
      const canvas = byId('airfoil-loft-canvas');
      const renderer = state.loft?.renderer || new THREE.WebGLRenderer({
        canvas, antialias: true, alpha: true,
      });
      renderer.setSize(canvas.clientWidth || 460, canvas.clientHeight || 220, false);
      const scene = new THREE.Scene();
      const camera = new THREE.PerspectiveCamera(32, (canvas.clientWidth || 460)
        / (canvas.clientHeight || 220), 0.01, 100);
      camera.position.set(1.4, 2.7, 1.5); camera.lookAt(0.35, 0, 0);
      const geometry = new THREE.BufferGeometry();
      const count = 72;
      const chords = [1.0, 0.64, 0.28];
      const spans = [0, 0.58, 1.18];
      const vertices = [];
      details.forEach((detail, stationIndex) => {
        const coordinates = detail.coordinates;
        for (let point = 0; point < count; point += 1) {
          const source = coordinates[Math.round(point * (coordinates.length - 1)
            / (count - 1))];
          vertices.push(
            Number(source[0]) * chords[stationIndex],
            spans[stationIndex],
            Number(source[1]) * chords[stationIndex],
          );
        }
      });
      const indices = [];
      for (let station = 0; station < 2; station += 1) {
        for (let point = 0; point < count - 1; point += 1) {
          const a = station * count + point;
          const b = a + 1; const c = a + count; const d = c + 1;
          indices.push(a, c, b, b, c, d);
        }
      }
      geometry.setAttribute('position', new THREE.Float32BufferAttribute(vertices, 3));
      geometry.setIndex(indices); geometry.computeVertexNormals();
      const mesh = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({
        color: 0x4da3ff, metalness: 0.05, roughness: 0.55, side: THREE.DoubleSide,
      }));
      scene.add(mesh, new THREE.HemisphereLight(0xcce8ff, 0x202730, 2.2));
      renderer.render(scene, camera);
      state.loft = { renderer, scene, camera };
    } catch (error) { showError(error); }
  }

  function continueToConcepts() {
    if (!state.selection || state.recommendation?.stale) return;
    const detail = {
      sessionId: state.sessionId,
      selectionVersion: state.selection.version,
      selectionHash: state.selection.selection_hash,
      scheduleHash: state.selection.schedule_hash,
      schedule: { ...state.selection.schedule },
    };
    window.dispatchEvent(new CustomEvent(AIRFOIL_CONTINUE_EVENT, { detail }));
    if (onContinue) onContinue(detail);
  }

  function bind() {
    byId('btn-airfoil-recompute')?.addEventListener('click', recompute);
    byId('btn-airfoil-continue')?.addEventListener('click', continueToConcepts);
    byId('btn-airfoil-modal-close')?.addEventListener('click', closeDetail);
    byId('airfoil-modal')?.addEventListener('click', (event) => {
      if (event.target.id === 'airfoil-modal') closeDetail();
    });
    document.addEventListener('keydown', (event) => {
      if (byId('airfoil-modal')?.classList.contains('hidden')) return;
      if (event.key === 'Escape') closeDetail();
      const index = STATIONS.indexOf(state.activeStation);
      if (event.key === 'ArrowLeft') {
        void openDetail(state.activeStrategy, STATIONS[(index + 2) % 3]);
      }
      if (event.key === 'ArrowRight') {
        void openDetail(state.activeStrategy, STATIONS[(index + 1) % 3]);
      }
    });
    window.addEventListener('popstate', () => {
      if (!byId('screen-airfoils')?.classList.contains('hidden')) void load();
    });
  }

  bind();
  return { load, recompute, closeDetail, state };
}
