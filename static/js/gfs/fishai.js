import { postJsonSafe } from './api.js';

function finiteNumber(value, fallback = NaN) {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

function clamp(value, lo, hi) {
  const n = finiteNumber(value, lo);
  return Math.max(lo, Math.min(hi, n));
}

function normalizeLayerName(name) {
  const s = String(name || '').trim().toLowerCase().replace(/[_\s]+/g, '-');
  if (['boat', 'boats', 'boater'].includes(s)) return 'boater';
  if (['shark', 'sharks', 'shark-intel', 'sharkintel', 'fish-shark'].includes(s)) return 'shark-intel';
  if (['inland', 'water', 'inland-water', 'inlandwaters', 'lake'].includes(s)) return 'inland-water';
  if (['jet', 'jets', 'jetstream', 'jetstream-balloons'].includes(s)) return 'jetstream';
  if (['bait', 'baitfish', 'bait-boils'].includes(s)) return 'bait';
  if (['cloud', 'clouds'].includes(s)) return 'clouds';
  if (['rain', 'precip', 'precipitation'].includes(s)) return 'rain';
  if (['lightning', 'glm', 'storm'].includes(s)) return 'lightning';
  if (['locations', 'location', 'fish', 'beacons'].includes(s)) return 'locations';
  return s;
}

function boolFromAction(action) {
  const raw = String(action?.enabled ?? action?.state ?? action?.value ?? '').toLowerCase();
  if (['off', 'false', '0', 'hide', 'disable'].includes(raw)) return false;
  return true;
}

function setGlobeCamera(globeEl, camera = {}) {
  if (!globeEl) return;
  const lat = finiteNumber(camera.lat ?? camera.center?.lat, NaN);
  const lon = finiteNumber(camera.lon ?? camera.lng ?? camera.center?.lon ?? camera.center?.lng, NaN);
  const altitude = finiteNumber(camera.altitude, 120);
  const range = finiteNumber(camera.range, NaN);
  const tilt = finiteNumber(camera.tilt, NaN);
  const heading = finiteNumber(camera.heading, NaN);
  const roll = finiteNumber(camera.roll, NaN);

  try {
    if (Number.isFinite(lat) && Number.isFinite(lon)) {
      const centerText = `${lat.toFixed(6)},${lon.toFixed(6)},${Math.round(altitude)}`;
      globeEl.setAttribute('center', centerText);
      try { globeEl.center = { lat, lng: lon, altitude }; } catch (_) {}
    }
    if (Number.isFinite(range)) {
      const safeRange = clamp(range, 500, 20000000);
      globeEl.setAttribute('range', String(Math.round(safeRange)));
      try { globeEl.range = safeRange; } catch (_) {}
    }
    if (Number.isFinite(tilt)) {
      const safeTilt = clamp(tilt, 0, 89);
      globeEl.setAttribute('tilt', String(Math.round(safeTilt)));
      try { globeEl.tilt = safeTilt; } catch (_) {}
    }
    if (Number.isFinite(heading)) {
      const safeHeading = ((heading % 360) + 360) % 360;
      globeEl.setAttribute('heading', String(Math.round(safeHeading)));
      try { globeEl.heading = safeHeading; } catch (_) {}
    }
    if (Number.isFinite(roll)) {
      globeEl.setAttribute('roll', String(Math.round(clamp(roll, -60, 60))));
      try { globeEl.roll = clamp(roll, -60, 60); } catch (_) {}
    }
  } catch (err) {
    console.warn('[fishai] camera command failed', err);
  }
}

function findLocalLocation(locations, action) {
  const id = String(action?.location_id || action?.id || '').toLowerCase();
  const name = String(action?.location_name || action?.name || action?.query || '').toLowerCase();
  if (!Array.isArray(locations)) return null;
  if (id) {
    const exact = locations.find((loc) => String(loc?.id || loc?.location_id || loc?.location_key || '').toLowerCase() === id);
    if (exact) return exact;
  }
  if (name) {
    return locations.find((loc) => String(loc?.name || loc?.title || '').toLowerCase().includes(name)) || null;
  }
  return null;
}

function renderFishAiLines(outputEl, result = {}) {
  if (!outputEl) return;
  const lines = [];
  if (result.headline) lines.push(result.headline);
  if (Array.isArray(result.reply_lines)) lines.push(...result.reply_lines.slice(0, 5));
  if (Array.isArray(result.top_locations) && result.top_locations.length) {
    const locText = result.top_locations.slice(0, 3).map((loc, idx) => `${idx + 1}. ${loc.name || 'spot'} ${Math.round(finiteNumber(loc.score, 0))}%`).join(' • ');
    lines.push(`Top: ${locText}`);
  }
  if (!lines.length) lines.push('FISHAI ready — type “go to Newport”, “species shark”, “rig leopard”, “tilt 60 heading 240”, or “best halibut shore”.');
  outputEl.textContent = lines.join('  |  ');
  outputEl.title = lines.join('\n');
}

function inferQuickLocalActions(prompt) {
  const text = String(prompt || '').toLowerCase();
  const actions = [];
  const angleMatch = text.match(/(?:tilt|angle)\s*(\d{1,3})/);
  const headingMatch = text.match(/(?:heading|bearing)\s*(\d{1,3})/);
  const rangeMatch = text.match(/(?:range|zoom)\s*(\d+(?:\.\d+)?)\s*(km|mi|m|meters?|miles?)?/);
  if (angleMatch || headingMatch || rangeMatch) {
    const camera = {};
    if (angleMatch) camera.tilt = clamp(Number(angleMatch[1]), 0, 89);
    if (headingMatch) camera.heading = Number(headingMatch[1]);
    if (rangeMatch) {
      let value = Number(rangeMatch[1]);
      const unit = String(rangeMatch[2] || 'm').toLowerCase();
      if (unit.startsWith('km')) value *= 1000;
      else if (unit.startsWith('mi')) value *= 1609.344;
      camera.range = value;
    }
    actions.push({ type: 'set_camera', camera, source: 'local_angle_parse' });
  }
  return actions;
}

export function installFishAI(options = {}) {
  const input = document.getElementById('fishAiInput');
  const form = document.getElementById('fishAiBar');
  const output = document.getElementById('fishAiOutput');
  const runBtn = document.getElementById('fishAiRun');
  if (!input || !form) return null;

  const ctx = {
    globeEl: options.globeEl,
    hud: options.hud,
    getLocations: typeof options.getLocations === 'function' ? options.getLocations : () => [],
    getDataState: typeof options.getDataState === 'function' ? options.getDataState : () => ({}),
    showStatus: typeof options.showStatus === 'function' ? options.showStatus : () => {},
    debugPanelEvent: typeof options.debugPanelEvent === 'function' ? options.debugPanelEvent : () => {},
    refreshMainSceneCache: options.refreshMainSceneCache,
    nudgeMainSceneCacheRefresh: options.nudgeMainSceneCacheRefresh,
    getCanonicalViewport: options.getCanonicalViewport,
    setLayerEnabled: options.setLayerEnabled,
  };

  async function applyAction(action, result) {
    const type = String(action?.type || '').toLowerCase();
    if (!type) return;

    if (type === 'set_camera' || type === 'fly_to' || type === 'go_to') {
      const camera = action.camera || action;
      setGlobeCamera(ctx.globeEl, camera);
      return;
    }

    if (type === 'open_location') {
      const loc = findLocalLocation(ctx.getLocations(), action);
      if (loc && ctx.hud?.open) {
        await ctx.hud.open(loc);
      }
      return;
    }

    if (type === 'set_layer') {
      const layer = normalizeLayerName(action.layer || action.name);
      const enabled = boolFromAction(action);
      if (ctx.setLayerEnabled) await ctx.setLayerEnabled(layer, enabled, { reason: 'fishai_command' });
      return;
    }

    if (type === 'refresh_layers') {
      const viewport = typeof ctx.getCanonicalViewport === 'function' ? ctx.getCanonicalViewport() : null;
      const rawLayers = Array.isArray(action.layers) ? action.layers : [];
      const layers = rawLayers.map(normalizeLayerName).filter(Boolean);
      if (ctx.refreshMainSceneCache && viewport) {
        await ctx.refreshMainSceneCache(viewport, 'fishai_read', { mode: 'fast', fast: true, refresh: false, layers });
      }
      if (ctx.nudgeMainSceneCacheRefresh && viewport) {
        ctx.nudgeMainSceneCacheRefresh(viewport, 'fishai_refresh', { layers }).catch?.(() => {});
      }
      return;
    }

    if (type === 'set_preference') {
      const key = String(action.key || '').trim();
      if (key) {
        try { localStorage.setItem(`fishai.${key}`, String(action.value ?? '')); } catch (_) {}
      }
      return;
    }
  }

  async function runPrompt(prompt) {
    const text = String(prompt || '').trim();
    if (!text) return;
    input.value = text;
    form.classList.add('running');
    if (runBtn) runBtn.disabled = true;
    if (output) output.textContent = 'FISHAI running…';
    ctx.showStatus('FISHAI running command/search…');
    ctx.debugPanelEvent('fishai/request', { prompt: text });

    const selected = ctx.hud?.selected?.() || null;
    const latest = ctx.getDataState() || {};
    const body = {
      prompt: text,
      selected_location_id: selected?.id || selected?.location_id || selected?.location_key || '',
      selected_location: selected ? { id: selected.id || selected.location_id || '', name: selected.name || '', lat: selected.lat, lon: selected.lon } : null,
      camera: {
        center: ctx.globeEl?.center || null,
        range: ctx.globeEl?.range || ctx.globeEl?.getAttribute?.('range') || null,
        tilt: ctx.globeEl?.tilt || ctx.globeEl?.getAttribute?.('tilt') || null,
        heading: ctx.globeEl?.heading || ctx.globeEl?.getAttribute?.('heading') || null,
      },
      cache_layers: latest?.sceneCache?.cache?.layers || latest?.sceneCache?.cache_layers || {},
    };

    const result = await postJsonSafe('/gfs/api/fishai', body, null, { timeoutMs: 4500 });
    const finalResult = result || { ok: false, headline: 'FISHAI local command fallback', actions: inferQuickLocalActions(text), reply_lines: ['Server reply unavailable; applying local camera/layer parse only.'] };
    const actions = [...inferQuickLocalActions(text), ...(Array.isArray(finalResult.actions) ? finalResult.actions : [])];
    for (const action of actions) {
      try { await applyAction(action, finalResult); }
      catch (err) { console.warn('[fishai] action failed', action, err); }
    }
    renderFishAiLines(output, finalResult);
    ctx.showStatus(finalResult.headline || 'FISHAI command complete');
    ctx.debugPanelEvent('fishai/result', { prompt: text, result: finalResult, actionCount: actions.length });
    form.classList.remove('running');
    if (runBtn) runBtn.disabled = false;
  }

  form.addEventListener('submit', (ev) => {
    ev.preventDefault();
    runPrompt(input.value).catch((err) => {
      form.classList.remove('running');
      if (runBtn) runBtn.disabled = false;
      if (output) output.textContent = `FISHAI error: ${err?.message || err}`;
      ctx.showStatus('FISHAI command failed');
      console.warn('[fishai] run failed', err);
    });
  });

  input.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') {
      input.value = '';
      if (output) output.textContent = 'FISHAI ready';
    }
  });

  const api = { runPrompt };
  window.FISHAI = api;
  if (output) output.textContent = 'FISHAI: type a direction or fish-search command';
  return api;
}
