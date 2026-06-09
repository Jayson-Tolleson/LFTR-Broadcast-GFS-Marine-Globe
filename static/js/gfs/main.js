import { getJsonSafe, getSceneFrame, uploadSafe } from './api.js';
import { ensureMaps3D, libs } from './globe.js';
import { renderMarkers } from './markers.js';
import { createHud } from './hud.js';
import { startLive, stopLive } from './live.js';
import { LayerEngine } from './layer_engine.js';
import { WorldSubscriptionRenderer, normalizeWorldLayerName, sceneLayersForWorldPill } from './world_subscription_renderer.js';
import { RendererLayer } from './layers/renderer_layer.js';
import { setCloudShellOpacityScale, renderCloudZones, cloudPayloadHasDrawableContent } from './cloud-zones.js';
import { renderRainZones } from './rain-zones.js';
import { renderLightningLayer } from './lightning-zones.js';
import { startSkySystem } from './sky.js';

const CLOUD_SHELL_REFRESH_MS = 60000;
const MAIN_SCENE_CACHE_REFRESH_MS = 120000; // one main subscription/cache call every 2 minutes
const EARTH_FIRST_BOOT_CACHE_DELAY_MS = 1800;
const EARTH_FIRST_INLAND_DELAY_MS = 4200;
const EARTH_FIRST_WARM_DELAY_MS = 7000;
const LIVE_REFRESH_MIN_GAP_MS = Number(window.GFS_LIVE_REFRESH_MIN_GAP_MS || 90000);
const CAMERA_STEADY_DEBOUNCE_MS = Number(window.GFS_CAMERA_STEADY_DEBOUNCE_MS || 800);
const LAYER_REFRESH_TTL_MS = Object.freeze({
  clouds: Number(window.GFS_CLOUD_RENDER_TTL_MS || 600000),
  rain: Number(window.GFS_RAIN_RENDER_TTL_MS || 300000),
  bait: Number(window.GFS_BAIT_RENDER_TTL_MS || 900000),
  boater: Number(window.GFS_BOATS_RENDER_TTL_MS || 300000),
  boats: Number(window.GFS_BOATS_RENDER_TTL_MS || 300000),
  current: Number(window.GFS_CURRENT_RENDER_TTL_MS || 600000),
  jetstream: Number(window.GFS_JETSTREAM_DATA_TTL_MS || 900000),
  fish: Number(window.GFS_FISH_RENDER_TTL_MS || 1800000),
});
const liveRefreshLastByKey = new Map();
import { installFishAI } from './fishai.js';

// Layer modules are lazy-loaded so a broken overlay cannot block the base globe.
// The globe should boot first; bait/boats/inland/shark can fail independently.
const __gfsLazyModules = new Map();
async function loadGfsLayerModule(name, path) {
  if (!__gfsLazyModules.has(name)) {
    __gfsLazyModules.set(name, import(path).catch((err) => {
      console.error(`[gfs boot] lazy layer import failed ${name}`, err);
      try { debugPanelEvent('layer/import-failed', { name, path, message: err?.message || String(err), policy: 'globe_boot_continues' }); } catch (_) {}
      throw err;
    }));
  }
  return __gfsLazyModules.get(name);
}

function failedLayerDisposer(name, err) {
  const disposer = () => {};
  disposer.__gfsKeepExisting = true;
  disposer.__gfsDidRender = false;
  try { debugPanelEvent('layer/render-skipped', { name, message: err?.message || String(err), policy: 'preserve_last_good_and_keep_globe' }); } catch (_) {}
  return disposer;
}

async function renderBaitZonesSafe(args) {
  try {
    const mod = await loadGfsLayerModule('bait-zones', './bait-zones.js');
    return mod.renderBaitZones(args);
  } catch (err) {
    console.warn('[gfs bait] renderer unavailable; globe continues', err);
    return failedLayerDisposer('bait', err);
  }
}

async function renderCurrentZonesLayerSafe(args) {
  try {
    const mod = await loadGfsLayerModule('current-zones', './current-zones.js');
    return mod.renderCurrentZonesLayer(args);
  } catch (err) {
    console.warn('[gfs current-zones] renderer unavailable; globe continues', err);
    return failedLayerDisposer('current-zones', err);
  }
}

async function renderBoatsLayerSafe(args) {
  try {
    const mod = await loadGfsLayerModule('boats', './boats.js');
    return mod.renderBoatsLayer(args);
  } catch (err) {
    console.warn('[gfs boater] renderer unavailable; globe continues', err);
    return failedLayerDisposer('boater', err);
  }
}

async function clearBoatRenderStateSafe() {
  try {
    const mod = await loadGfsLayerModule('boats', './boats.js');
    return mod.clearBoatRenderStateSafe?.();
  } catch (_) {
    try { window.clearGfsBoatsLayer?.(); } catch (__) {}
    return undefined;
  }
}

async function renderInlandWaterLayerSafe(args) {
  try {
    const mod = await loadGfsLayerModule('inland-water', './inland-water.js');
    return mod.renderInlandWaterLayer(args);
  } catch (err) {
    console.warn('[gfs inland-water] renderer unavailable; globe continues', err);
    return failedLayerDisposer('inland-water', err);
  }
}

async function renderSharkIntelLayerSafe(args) {
  try {
    const mod = await loadGfsLayerModule('shark-intel', './shark-intel.js');
    return mod.renderSharkIntelLayer(args);
  } catch (err) {
    console.warn('[gfs shark-intel] renderer unavailable; globe continues', err);
    return failedLayerDisposer('shark-intel', err);
  }
}

async function clearSharkIntelLayerSafe() {
  try {
    const mod = await loadGfsLayerModule('shark-intel', './shark-intel.js');
    return mod.clearSharkIntelLayerSafe?.();
  } catch (_) {
    try { window.clearGfsSharkIntelLayer?.(); } catch (__) {}
    return undefined;
  }
}


const statusEl = document.getElementById('status');
const globeEl = document.getElementById('globe');
const fallbackEl = document.getElementById('globeFallback');

function setEarthFirstPriority(reason = 'boot') {
  try { globeEl?.setAttribute?.('data-earth-priority', 'high-res-first'); } catch (_) {}
  try { globeEl?.setAttribute?.('data-overlay-priority', 'defer-until-earth-paint'); } catch (_) {}
  try { window.__gfsEarthPriority = { mode: 'high_res_first', reason, ts: Date.now() }; } catch (_) {}
  try { debugPanelEvent('earth/priority', { reason, mode: 'high_res_first', overlayPolicy: 'cache_reads_deferred' }); } catch (_) {}
}

function afterEarthPaint(callback, delayMs = EARTH_FIRST_BOOT_CACHE_DELAY_MS) {
  const run = () => window.setTimeout(callback, Math.max(0, Number(delayMs) || 0));
  try {
    window.requestAnimationFrame?.(() => window.requestAnimationFrame?.(run) || run());
  } catch (_) { run(); }
}

const CLOUD_GLOBAL_VIEWPORT_BBOX = Object.freeze({ west: -179.5, south: -72.0, east: 179.5, north: 72.0, quality: 'coarse', scene_tier: 'world', source: 'cloud_global_full_bounds' });

function cloudGlobalViewportBbox() {
  return { ...CLOUD_GLOBAL_VIEWPORT_BBOX };
}

function initCloudShellOpacityControl() {
  const slider = document.getElementById('cloudShellAlphaSlider');
  const valueEl = document.getElementById('cloudShellAlphaValue');
  if (!slider) return;

  // Cloud transparency is literal now: 0% invisible → 100% solid cloud shells/particles.
  try { slider.min = '0'; slider.max = '100'; slider.step = '5'; } catch (_) {}

  const clamp = (v, min, max) => Math.max(min, Math.min(max, v));
  const readSavedScale = () => {
    const savedRaw = localStorage.getItem('gfs.cloudShellOpacity');
    const saved = savedRaw == null ? NaN : Number(savedRaw);
    const current = Number(window.__gfsCloudShellOpacityScale ?? window.GFS_CLOUD_SHELL_OPACITY);
    const rawScale = Number.isFinite(saved) ? saved : (Number.isFinite(current) ? current : 0.92);
    return clamp(rawScale > 2 ? rawScale / 100 : rawScale, 0.0, 1.0);
  };
  const paint = (scale) => {
    const percent = Math.round(clamp(Number(scale), 0.0, 1.0) * 100);
    try { slider.value = String(percent); } catch (_) {}
    if (valueEl) valueEl.textContent = `${percent}%`;
    return percent;
  };
  const commitValue = (value, source = 'slider') => {
    const percent = clamp(Number(value), 0, 100);
    const scale = setCloudShellOpacityScale(percent / 100);
    paint(scale);
    try {
      window.refreshCloudShellOpacity?.();
      requestAnimationFrame(() => {
        window.refreshCloudShellOpacity?.();
        setTimeout(() => window.refreshCloudShellOpacity?.(), 60);
      });
    } catch (_) {}
    debugPanelEvent('clouds/opacity-ui', { source, percent: Math.round(scale * 100), scale });
  };

  paint(readSavedScale());
  commitValue(slider.value, 'init');

  ['pointerdown', 'mousedown', 'touchstart', 'click', 'dblclick'].forEach((evt) => {
    slider.addEventListener(evt, (ev) => ev.stopPropagation(), { passive: true });
  });
  slider.addEventListener('input', () => commitValue(slider.value, 'input'));
  slider.addEventListener('change', () => commitValue(slider.value, 'change'));
}



const sceneProgressEls = {
  sceneRow: document.querySelector('[data-progress-kind="scene"]'),
  ttlRow: document.querySelector('[data-progress-kind="ttl"]'),
  layerRow: document.querySelector('[data-progress-kind="layers"]'),
  sceneFill: document.getElementById('sceneProgressFill'),
  ttlFill: document.getElementById('ttlProgressFill'),
  layerFill: document.getElementById('layerProgressFill'),
  sceneText: document.getElementById('sceneProgressText'),
  ttlText: document.getElementById('ttlProgressText'),
  layerText: document.getElementById('layerProgressText'),
};
const sceneProgressState = {
  intervalMs: MAIN_SCENE_CACHE_REFRESH_MS,
  lastSceneStartAt: 0,
  lastSceneCompleteAt: 0,
  nextSceneAt: 0,
  activeLayers: [],
  latestCacheLayers: {},
};

function clampProgress(value, fallback = 0) {
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.max(0, Math.min(100, n));
}

function setProgressBar(kind, pct, text, state = '') {
  const row = sceneProgressEls[`${kind}Row`];
  const fill = sceneProgressEls[`${kind}Fill`];
  const txt = sceneProgressEls[`${kind}Text`];
  const safePct = clampProgress(pct);
  if (fill) fill.style.width = `${safePct.toFixed(0)}%`;
  if (txt && text != null) txt.textContent = String(text);
  if (row) {
    if (state) row.dataset.state = state;
    else row.removeAttribute('data-state');
    row.title = `${kind}: ${safePct.toFixed(0)}%${text ? ` — ${text}` : ''}`;
  }
}

function summarizeLayerProgress(cacheLayers = {}) {
  const entries = Object.entries(cacheLayers || {});
  if (!entries.length) return { pct: 0, text: 'waiting', state: 'busy' };
  let score = 0;
  let fresh = 0;
  let warming = 0;
  let stale = 0;
  for (const [, meta] of entries) {
    const m = meta && typeof meta === 'object' ? meta : {};
    const age = Number(m.age_sec);
    const hit = Boolean(m.cache_hit);
    const scheduled = Boolean(m.refresh_scheduled);
    const status = String(m.status || '').toLowerCase();
    if (hit && Number.isFinite(age) && age <= 120) { score += 1; fresh += 1; }
    else if (hit) { score += 0.68; stale += 1; }
    else if (scheduled || /warm|build|queue/.test(status)) { score += 0.36; warming += 1; }
    else { score += 0.18; warming += 1; }
  }
  const pct = (score / Math.max(1, entries.length)) * 100;
  const text = fresh
    ? `${fresh}/${entries.length} fresh${warming ? ` • ${warming} warm` : ''}`
    : `${warming || entries.length}/${entries.length} warming`;
  const state = warming ? 'busy' : (stale ? 'stale' : '');
  return { pct, text, state };
}

function updateSceneProgressTick() {
  const now = Date.now();
  const interval = Number(sceneProgressState.intervalMs || MAIN_SCENE_CACHE_REFRESH_MS);
  const nextAt = Number(sceneProgressState.nextSceneAt || 0);
  if (nextAt > 0 && interval > 0) {
    const remainingMs = Math.max(0, nextAt - now);
    const elapsedPct = clampProgress(((interval - remainingMs) / interval) * 100);
    const remainingSec = Math.ceil(remainingMs / 1000);
    setProgressBar('ttl', elapsedPct, remainingSec > 0 ? `refresh in ${remainingSec}s` : 'refresh now', remainingSec <= 10 ? 'busy' : '');
  } else {
    setProgressBar('ttl', 0, 'TTL ready', 'busy');
  }
  const layerSummary = summarizeLayerProgress(sceneProgressState.latestCacheLayers);
  setProgressBar('layer', layerSummary.pct, layerSummary.text, layerSummary.state);
}

function markSceneProgressRequest(reason = 'scene-cache') {
  sceneProgressState.lastSceneStartAt = Date.now();
  sceneProgressState.activeLayers = subscribedSceneLayers();
  sceneProgressState.nextSceneAt = sceneProgressState.lastSceneStartAt + Number(sceneProgressState.intervalMs || MAIN_SCENE_CACHE_REFRESH_MS);
  setProgressBar('scene', 28, `reading ${sceneProgressState.activeLayers.length} layers`, 'busy');
  setProgressBar('ttl', 0, 'TTL reset', 'busy');
  debugPanelEvent('scene-progress/request', { reason, layers: sceneProgressState.activeLayers, intervalMs: sceneProgressState.intervalMs });
}

function markSceneProgressPayload(payload, reason = 'scene-cache') {
  const cache = payload?.cache || {};
  const cacheLayers = cache.layers || {};
  sceneProgressState.latestCacheLayers = cacheLayers;
  sceneProgressState.intervalMs = Number(payload?.refresh_interval_ms || sceneProgressState.intervalMs || MAIN_SCENE_CACHE_REFRESH_MS);
  sceneProgressState.lastSceneCompleteAt = Date.now();
  sceneProgressState.nextSceneAt = sceneProgressState.lastSceneCompleteAt + sceneProgressState.intervalMs;
  const layerCount = Object.keys(cacheLayers || {}).length || sceneProgressState.activeLayers.length || 0;
  setProgressBar('scene', 100, `rendered ${layerCount} layers`, '');
  updateSceneProgressTick();
  debugPanelEvent('scene-progress/payload', { reason, layerCount, cache: cacheLayers, intervalMs: sceneProgressState.intervalMs });
}

function markSceneProgressError(err, reason = 'scene-cache') {
  setProgressBar('scene', 100, 'cache error', 'error');
  debugPanelEvent('scene-progress/error', { reason, message: err?.message || String(err || 'unknown') });
}

let activeLocation = null;
let liveManuallyDismissed = false;
let selectedLocation = null;
let locationsTeardown = null;
let latestLocations = [];
let liveStatePollId = null;
const missingLiveLocationIds = new Set();
const GFS_DEBUG = Boolean(window.__GFS_DEBUG);
let skyRuntime = null;

const layerRuntime = {
  engine: null,
  world: null,
  rafId: 0,
};

// Per-layer render registry for request -> animation speed. A layer can reuse
// objects and skip work when the backend says the tile/payload version did not
// change. This keeps pill toggles fast and prevents the 2-minute loop from
// blindly rebuilding existing animation.
const GFS_RENDER_REGISTRY = window.GFS_RENDER_REGISTRY = window.GFS_RENDER_REGISTRY || {};
for (const name of ['locations','clouds','rain','lightning','jetstream','bait','shark-intel','boater','inland-water','inland_water_temp']) {
  if (!GFS_RENDER_REGISTRY[name]) GFS_RENDER_REGISTRY[name] = { version: '', tiles: new Map(), lastPaintAt: 0 };
}

function layerPayloadVersion(layer, payload) {
  return String(payload?.version || payload?.cache?.version || payload?.cache_quality?.version || payload?.cache?.key || payload?.valid_time || payload?.resolved_time || payload?.ts || '');
}

function markLayerFirstPaint(layer, payload) {
  const rec = GFS_RENDER_REGISTRY[layer] || (GFS_RENDER_REGISTRY[layer] = { version: '', tiles: new Map(), lastPaintAt: 0 });
  const version = layerPayloadVersion(layer, payload);
  if (version && version === rec.version) {
    debugPanelEvent('render/noop-same-layer-version', { layer, version });
    return false;
  }
  rec.version = version || `${Date.now()}`;
  rec.lastPaintAt = Date.now();
  return true;
}

// One pill = one topic. All topics start active per LFTR operator preference.
// Missing real sources do NOT trigger fallback geometry; the pill stays visibly
// active/marked source-missing while the route returns empty strict payloads.
const DEFAULT_LAYER_ENABLED = Object.freeze({
  locations: true,
  clouds: true,
  rain: true,
  lightning: true,
  jetstream: true,
  bait: true,
  boater: true,
  'inland-water': true,
});

// Static/semi-static layers should not be reloaded by the 2-minute TTL loop.
// Locations are loaded once from /gfs/api/locations and only re-rendered locally.
// Inland water geometry is tile/cache based and should not churn every TTL; its
// live temp labels are refreshed through the separate inland_water_temp sublayer.
const STATIC_SCENE_LAYERS = Object.freeze(new Set(['locations', 'inland-water']));
const LIVE_COMPANION_SCENE_LAYERS = Object.freeze({
  'inland-water': ['inland_water_temp'],
});

const LAYER_CLEAR_SELECTORS = Object.freeze({
  locations: '[data-gfs-layer="locations"]',
  clouds: '[data-gfs-layer="clouds"],[data-cloud-shell],[data-cloud-particle],[data-gfs-layer="ocean-fx"],[data-ocean-fx-kind]',
  rain: '[data-gfs-layer="rain"]',
  lightning: '[data-gfs-layer="lightning"]',
  jetstream: '[data-gfs-layer="jetstream"],[data-jetstream-balloon],gmp-marker-3d[data-jetstream-balloon]',
  bait: '[data-gfs-layer="bait"]',
  'shark-intel': '[data-gfs-layer="shark-intel"]',
  boater: '[data-gfs-layer="boater"],[data-gfs-layer="current-zones"]',
  'inland-water': '[data-gfs-layer="inland-water"]',
});

const DEBUG_EVENT_THROTTLE_MS = Number(window.__GFS_DEBUG_EVENT_THROTTLE_MS || 12000);
const DEBUG_EVENT_LIGHT_KEYS = new Set(['render/noop-same-layer-version', 'render/noop-same-version', 'scene-cache/apply', 'scene-progress/payload', 'STATE']);
const debugEventLast = new Map();
function debugPanelEvent(topic, detail = {}) {
  try {
    const key = `${topic}|${detail?.reason || ''}|${detail?.layer || ''}`;
    const now = Date.now();
    const last = debugEventLast.get(key) || 0;
    if (DEBUG_EVENT_LIGHT_KEYS.has(topic) && (now - last) < DEBUG_EVENT_THROTTLE_MS) return;
    debugEventLast.set(key, now);
    if ((topic === 'scene-cache/apply' || topic === 'scene-progress/payload') && detail && typeof detail === 'object') {
      const slim = {
        reason: detail.reason,
        layerCount: detail.layerCount,
        intervalMs: detail.intervalMs,
        refreshIntervalMs: detail.refreshIntervalMs,
        layers: detail.layers ? Object.fromEntries(Object.entries(detail.layers).map(([k, v]) => [k, {
          status: v?.status,
          source: v?.source,
          polygons: v?.polygons,
          points: v?.points,
          boats: v?.boats,
          clouds: v?.clouds,
          rain: v?.rain,
          version: v?.cache?.version || v?.version,
          cache_hit: v?.cache?.hit ?? v?.cache_hit,
        }])) : undefined,
        cache: detail.cache ? { mode: detail.cache.mode, ttl_policy: detail.cache.ttl_policy } : undefined,
      };
      window.__gfsDebugEvent?.(topic, slim);
      return;
    }
    window.__gfsDebugEvent?.(topic, detail);
  } catch (_) {}
}

function logMissionControlState(reason = 'manual') {
  try {
    const layers = Object.fromEntries(Object.entries(layerRuntime.engine?.layers || {}).map(([k, v]) => [k, Boolean(v?.enabled)]));
    const frame = dataState.latest.frame || {};
    debugPanelEvent('mission-control/state', {
      reason,
      layers,
      frame: summarizePayloadForDebug(frame),
      cache: frame?.cache || frame?.cache_state || frame?.meta?.cache || null,
      renderReason: frame?.render_reason || frame?.meta?.render_reason || null,
    });
  } catch (_) {}
}

function summarizePayloadForDebug(payload) {
  if (!payload || typeof payload !== 'object') return { type: typeof payload };
  const summary = {
    status: payload.status || payload.payload_state || payload.source_state || payload.mode || undefined,
    source: payload.source || payload.data_source || payload.provider || undefined,
    sceneTier: payload.scene_tier || payload.scene_plan?.tier || undefined,
    cache: payload.cache || payload.cache_state || payload.cache_hit || undefined,
  };
  const addCount = (key, value) => { if (Array.isArray(value)) summary[key] = value.length; };
  addCount('tiles', payload.tiles);
  addCount('features', payload.features);
  addCount('polygons', payload.polygons || payload.bait?.polygons);
  addCount('boats', payload.boats);
  addCount('flashes', payload.flashes);
  addCount('regions', payload.regions);
  addCount('clouds', payload.clouds?.items || payload.clouds?.scene?.clouds || payload.items);
  addCount('rain', payload.rain?.items || payload.precip_columns || payload.weather?.precip_columns);
  addCount('points', payload.points || payload.ocean_points || payload.samples);
  if (payload.bbox) summary.bbox = payload.bbox;
  const resolution = resolutionContractFromPayload(payload);
  if (resolution) summary.resolution = resolution;
  return Object.fromEntries(Object.entries(summary).filter(([, v]) => v !== undefined && v !== null && v !== ''));
}



function resolutionContractFromPayload(payload) {
  if (!payload || typeof payload !== 'object') return null;
  const sourceResolutionDeg = Number(payload.source_resolution_deg ?? payload.sourceResolutionDeg ?? payload?.meta?.source_resolution_deg ?? payload?.resolution?.source_resolution_deg);
  const stride = Number(payload.stride ?? payload.provider_stride ?? payload?.scene_plan?.provider_stride ?? payload?.resolution?.stride ?? 1);
  const derivedResolutionDeg = Number(payload.derived_resolution_deg ?? payload?.meta?.derived_resolution_deg ?? payload?.resolution?.derived_resolution_deg);
  const sourceGridShape = payload.source_grid_shape || payload?.resolution?.source_grid_shape || payload?.meta?.source_grid_shape || undefined;
  const derivedGridShape = payload.derived_grid_shape || payload?.resolution?.derived_grid_shape || payload?.meta?.derived_grid_shape || undefined;
  const subgridUsed = payload.subgrid_used ?? payload?.resolution?.subgrid_used;
  const contract = {
    source_resolution_deg: Number.isFinite(sourceResolutionDeg) ? sourceResolutionDeg : undefined,
    stride: Number.isFinite(stride) ? stride : undefined,
    derived_resolution_deg: Number.isFinite(derivedResolutionDeg) ? derivedResolutionDeg : undefined,
    source_grid_shape: Array.isArray(sourceGridShape) ? sourceGridShape : undefined,
    derived_grid_shape: Array.isArray(derivedGridShape) ? derivedGridShape : undefined,
    subgrid_used: Number.isFinite(Number(subgridUsed)) ? Number(subgridUsed) : undefined,
  };
  return Object.keys(contract).some((k) => contract[k] !== undefined) ? contract : null;
}

function countArray(value) {
  return Array.isArray(value) ? value.length : 0;
}
function isFallbackBoatPayload(payload) {
  const source = String(payload?.source || payload?.ocean?.source || payload?.oceanPoints?.source || '').toLowerCase();
  const mode = String(payload?.mode || payload?.ocean?.mode || '').toLowerCase();
  return source.includes('fallback') || source.includes('marker_ocean_solve') || mode.includes('fallback') || mode.includes('proxy');
}

function boatCounts(payload) {
  const raw = countArray(payload?.boats);
  const fallbackRejected = isFallbackBoatPayload(payload) ? raw : 0;
  return {
    raw,
    renderableHint: fallbackRejected ? 0 : raw,
    fallbackRejected,
    source: payload?.source || payload?.mode || 'unknown',
  };
}


function isPartyTimeEnabled() {
  return window.__gfsPartyTime === true;
}

function syncPartyTimePill(enabled = isPartyTimeEnabled()) {
  const btn = document.getElementById('partyTimePill');
  if (!btn) return;
  const on = Boolean(enabled);
  btn.classList.toggle('active', on);
  btn.setAttribute('aria-pressed', on ? 'true' : 'false');
  btn.setAttribute('data-party-time', on ? 'true' : 'false');
  btn.title = on
    ? 'Party Time ON — cloud edges use multicolor rope-light glow'
    : 'Party Time OFF — cloud edges use normal neon glow';
}

function togglePartyTimePill() {
  const nextEnabled = !isPartyTimeEnabled();
  window.__gfsPartyTime = nextEnabled;
  try { window.setCloudPartyMode?.(nextEnabled); } catch (_) {}
  syncPartyTimePill(nextEnabled);
  console.info('[gfs clouds] party time toggle', { enabled: nextEnabled });
  debugPanelEvent('pill/party-time', { layer: 'clouds', enabled: nextEnabled, mode: nextEnabled ? 'multicolor_rope_light_edges' : 'normal_neon_edges' });
  return nextEnabled;
}

function syncPillState(name, enabled) {
  const btn = document.querySelector(`.overlay-pill[data-layer="${name}"]`);
  if (!btn) return;
  const unavailable = isLayerUnavailable(name);
  const on = Boolean(enabled);
  const isInland = name === 'inland-water';
  btn.classList.toggle('active', on);
  // Inland Waters must look ON while data is building.  A missing first tile is
  // a warming/downloading state, not an off/unavailable pill state.
  btn.classList.toggle('unavailable', unavailable && !isInland);
  btn.classList.toggle('building', unavailable && isInland && on);
  btn.setAttribute('aria-pressed', on ? 'true' : 'false');
  btn.setAttribute('data-layer-enabled', on ? 'true' : 'false');
  if (unavailable) btn.setAttribute('data-source-status', isInland ? 'building' : 'missing');
  else {
    btn.removeAttribute('data-source-status');
    btn.classList.remove('building');
  }
}

const PILL_DOUBLE_TAP_MS = 0;
const pillTapState = new Map();

function layerUserPrefs() {
  if (!window.__gfsLayerUserPrefs || typeof window.__gfsLayerUserPrefs !== 'object') {
    window.__gfsLayerUserPrefs = { ...DEFAULT_LAYER_ENABLED };
  } else {
    for (const [name, enabled] of Object.entries(DEFAULT_LAYER_ENABLED)) {
      if (window.__gfsLayerUserPrefs[name] === undefined) window.__gfsLayerUserPrefs[name] = enabled;
    }
  }
  return window.__gfsLayerUserPrefs;
}

function layerDefaultEnabled(name) {
  return DEFAULT_LAYER_ENABLED[name] !== false;
}

function isLayerUnavailable(name) {
  return Boolean(window.__gfsLayerUnavailable?.[name]);
}

function isLayerUserDisabled(name) {
  return layerUserPrefs()[name] === false;
}

function isLayerOn(name) {
  const layer = layerRuntime.engine?.layers?.[name];
  return Boolean(layer && layer.enabled && !isLayerUserDisabled(name));
}

function clearLayerVisuals(name) {
  const selector = LAYER_CLEAR_SELECTORS[name] || `[data-gfs-layer="${name}"]`;
  try { globeEl?.querySelectorAll?.(selector)?.forEach((el) => el.remove()); } catch (_) {}
  if (name === 'clouds') {
    try { window.clearGfsCloudLayer?.(); } catch (_) {}
  }
  if (name === 'jetstream') {
    try { window.pauseJetBalloons?.(); } catch (_) {}
    try { window.clearJetBalloons?.(); } catch (_) {}
  }
  if (name === 'rain') {
    try { window.clearGfsRainLayer?.(); } catch (_) {}
  }
  if (name === 'shark-intel') {
    try { clearSharkIntelLayerSafe?.(); } catch (_) {}
  }
  if (name === 'boater') {
    try { clearBoatRenderStateSafe?.(); } catch (_) {}
    try { window.clearGfsBoatsLayer?.(); } catch (_) {}
    try { globeEl?.querySelectorAll?.('[data-gfs-layer="boater"],[data-gfs-sub-layer="boater"],.gfs-boater-node,[data-gfs-layer="current-zones"],[data-gfs-sub-layer="current-marching-squares"]')?.forEach((el) => el.remove()); } catch (_) {}
    try { window.__gfsDebugEvent?.('boats/clear', { reason: 'boater_pill_off' }); } catch (_) {}
  }
}function rememberLayerChoice(name, enabled) {
  const prefs = layerUserPrefs();
  prefs[name] = enabled !== false;
  window.__gfsLayerState = { ...(window.__gfsLayerState || {}) };
  if (name === 'jetstream') {
    window.__gfsLayerState.jetstreamBalloons = enabled !== false;
    window.__gfsJetstreamDisabled = enabled === false;
    window.__gfsJetstreamPillOn = enabled !== false;
  }
  if (name === 'clouds') {
    window.__gfsCloudsDisabled = enabled === false;
  }
}

function requestSceneCacheForLayer(name, reason = 'pill_on') {
  if (layerRuntime.world && typeof layerRuntime.world.requestLayer === 'function') {
    return layerRuntime.world.requestLayer(name, reason);
  }
  const layers = sceneLayersForPill(name);
  if (!layers.length) return null;
  const vp = getCanonicalViewport();
  debugPanelEvent('pill/subscribe', { layer: name, layers, reason, contract: 'pill_subscribes_scene_cache_only' });
  window.setTimeout(() => refreshMainSceneCache(vp, `${reason}_first_paint`, { mode: 'fast', fast: true, refresh: false, layers, includeStatic: true }).catch(() => {}), 20);
  window.setTimeout(() => nudgeMainSceneCacheRefresh(vp, `${reason}_background_refresh`, { layers, includeStatic: true }).catch(() => {}), 160);
  if (name === 'inland-water') window.setTimeout(() => refreshDeferredInlandWater(vp, `${reason}_inland_cache_read`).catch(() => {}), 220);
  if (name === 'jetstream') window.setTimeout(() => ensureJetstreamVisual(reason).catch(() => {}), 240);
  return { ok: true, layer: name, layers, reason };
}

function togglePillLayer(name, btn, engine) {
  const layerName = normalizeSceneLayerName(name);
  if (layerRuntime.world) {
    const nextEnabled = layerName === 'locations'
      ? !(layerUserPrefs().locations !== false)
      : !isLayerOn(layerName);
    layerRuntime.world.setLayerEnabled(layerName, nextEnabled, { reason: `pill_${layerName}_toggle`, subscribe: true }).catch((err) => {
      console.warn('[gfs pills] world toggle failed', { layer: layerName, message: err?.message || String(err) });
    });
    return;
  }
  if (layerName === 'locations') {
    const nextEnabled = !isLayerOn('locations') && layerUserPrefs().locations === false ? true : !(layerUserPrefs().locations !== false);
    rememberLayerChoice('locations', nextEnabled);
    if (nextEnabled === false) clearLayerVisuals('locations');
    else renderLocationsSubscription(latestLocations);
    syncPillState('locations', nextEnabled);
    debugPanelEvent('pill/toggle', { layer: 'locations', enabled: nextEnabled, staticLayer: true, reloadPolicy: 'local_rerender_only' });
    return;
  }
  const layer = engine.layers[layerName];
  if (!layer) {
    console.warn('[gfs pills] missing layer', { name: layerName });
    debugPanelEvent('pill/missing-layer', { layer: layerName });
    return;
  }
  const nextEnabled = !isLayerOn(layerName);
  rememberLayerChoice(layerName, nextEnabled);
  engine.setEnabled(layerName, nextEnabled);
  if (nextEnabled === false) clearLayerVisuals(layerName);
  syncPillState(layerName, nextEnabled);
  console.info('[gfs pills] toggle', { layer: layerName, enabled: nextEnabled });
  debugPanelEvent('pill/toggle', { layer: layerName, enabled: nextEnabled, activeLayers: Object.fromEntries(Object.entries(engine.layers || {}).map(([k, v]) => [k, Boolean(v?.enabled && !isLayerUserDisabled(k))])) });
  if (nextEnabled) requestSceneCacheForLayer(layerName, `pill_${layerName}_on`);
  else clearLayerVisuals(layerName);
}



function renderLocationsSubscription(locations = latestLocations) {
  latestLocations = Array.isArray(locations) ? locations : [];
  try { locationsTeardown?.(); } catch (_) {}
  locationsTeardown = null;
  if (isLayerUserDisabled('locations')) {
    clearLayerVisuals('locations');
    return null;
  }
  try {
    locationsTeardown = renderMarkers({ locations: latestLocations, globeEl, maps3d: libs.maps3d, onSelect: (loc) => { gfsSocket.connect(); hud.open(loc); } });
  } catch (err) {
    console.warn('[gfs locations] render failed', { message: err?.message || String(err) });
  }
  return locationsTeardown;
}

function baitAdvancedReady(payload) {
  const bait = payload?.bait || {};
  const source = String(bait?.source || payload?.source || '').toLowerCase();
  const mode = String(payload?.mode || '').toLowerCase();
  const hasPolygons = Boolean(
    (Array.isArray(bait.polygons) && bait.polygons.length > 0)
    || (Array.isArray(bait.outer_polygons) && bait.outer_polygons.length > 0)
    || (Array.isArray(bait.inner_polygons) && bait.inner_polygons.length > 0)
    || (Array.isArray(bait.core_polygons) && bait.core_polygons.length > 0)
  );
  const liveReady = source === 'full_stack' || source.includes('live_hycom') || source.includes('coastwatch') || mode.includes('marching_squares');
  return Boolean(payload && bait && bait.status === 'ready' && liveReady && hasPolygons);
}

function nonEmptyFields(...sets) {
  for (const fields of sets) {
    if (fields && typeof fields === 'object' && Object.keys(fields).length > 0) return fields;
  }
  return {};
}
function hasBaitRenderablePayload(payload) {
  const bait = payload?.bait || {};
  // Bait-score rows are intentionally not renderable by themselves. They feed HUD
  // intel, but the visible bait layer must start from polygons or a solved ocean
  // field so it never flashes point/proxy bait before the marching-school solve.
  return Boolean(
    payload
    && (
      (Array.isArray(bait?.polygons) && bait.polygons.length > 0)
      || (Array.isArray(bait?.outer_polygons) && bait.outer_polygons.length > 0)
      || (Array.isArray(bait?.inner_polygons) && bait.inner_polygons.length > 0)
      || (Array.isArray(bait?.core_polygons) && bait.core_polygons.length > 0)
      || (Array.isArray(payload?.oceanPoints?.points) && payload.oceanPoints.points.length >= 4)
      || (Array.isArray(payload?.points) && payload.points.length >= 4)
      || (Array.isArray(payload?.bait_score) && payload.bait_score.length >= 4)
      || (Array.isArray(bait?.bait_score) && bait.bait_score.length >= 4)
    )
  );
}

function preferRenderableBaitAdvanced(nextPayload, fallbackPayload) {
  if (baitAdvancedReady(nextPayload)) return nextPayload;
  if (hasBaitRenderablePayload(nextPayload)) return nextPayload;
  if (baitAdvancedReady(fallbackPayload)) return fallbackPayload;
  if (hasBaitRenderablePayload(fallbackPayload)) return fallbackPayload;
  return nextPayload || fallbackPayload || null;
}


function hasBoaterRenderablePayload(payload) {
  if (!payload || typeof payload !== 'object') return false;
  const boats = Array.isArray(payload.boats) ? payload.boats : [];
  const points = Array.isArray(payload.points) ? payload.points : [];
  const oceanPoints = Array.isArray(payload.ocean_points) ? payload.ocean_points : (Array.isArray(payload.oceanPoints) ? payload.oceanPoints : []);
  const gridPoints = Array.isArray(payload?.grid?.points) ? payload.grid.points : [];
  const directPolygons = Array.isArray(payload.polygons) ? payload.polygons : [];
  const hint = Number(payload.renderable_count_hint ?? payload.renderableHint ?? payload.count ?? 0);
  const source = String(payload.source || '').toLowerCase();
  const state = String(payload.status || payload.payload_state || '').toLowerCase();
  const fallback = Boolean(payload?.fallback?.used) || source.includes('fallback') || source.includes('empty_boater');
  const hasSamples = boats.length > 0 || points.length >= 4 || oceanPoints.length >= 4 || gridPoints.length >= 4 || directPolygons.length > 0 || hint > 0;
  return hasSamples && !fallback && !state.includes('warming');
}

function preferRenderableBoater(nextPayload, fallbackPayload) {
  if (hasBoaterRenderablePayload(nextPayload)) return nextPayload;
  if (hasBoaterRenderablePayload(fallbackPayload)) return fallbackPayload;
  return nextPayload || fallbackPayload || null;
}


function serverBaitPolygonCount(payload) {
  const bait = payload?.bait || {};
  const root = payload || {};
  return [
    bait.polygons, bait.outer_polygons, bait.inner_polygons, bait.core_polygons,
    root.polygons, root.outer_polygons, root.inner_polygons, root.core_polygons,
  ].reduce((sum, value) => sum + (Array.isArray(value) ? value.length : 0), 0);
}

function hasStrictBaitPolygons(payload) {
  return Boolean(payload && serverBaitPolygonCount(payload) > 0);
}
function initLayerSystem() {
  if (layerRuntime.engine) return layerRuntime.engine;
  const engine = new LayerEngine();
  const clouds = new RendererLayer(globeEl, {
    name: 'clouds',
    selector: (frame) => {
      if (window.__gfsCloudsDisabled === true || isLayerUserDisabled('clouds')) return null;
      const clouds = frame?.clouds || null;
      // Strict source policy: draw only live/server cloud payloads. No client fallback.
      if (clouds && cloudPayloadHasDrawableContent(clouds)) {
        return {
          ...clouds,
          source_state: clouds.source_state || clouds.payload_state || clouds.status || 'unknown',
          fields: clouds.fields || frame?.weather?.fields || {},
          bbox: clouds.bbox || clouds.bbox_used || frame?.bbox || frame?.meta?.bbox,
          bbox_used: clouds.bbox_used || clouds.bbox || frame?.bbox || frame?.meta?.bbox_used,
          strict_render_contract: 'split_cloud_payload_primary',
        };
      }
      return null;
    },
    signature: (_frame, payload) => {
      const bbox = Array.isArray(payload?.bbox) ? payload.bbox.map((v) => Number(v).toFixed(1)).join(',') : '';
      const features = payload?.features || payload?.items || payload?.tiles || payload?.scene?.clouds || [];
      const count = Array.isArray(features) ? features.length : 0;
      const pick = (f) => {
        const v = f?.center || f?.properties?.center || f?.properties || f || {};
        const lat = Number(v.lat ?? v.latitude ?? v.center_lat ?? v.y ?? 0);
        const lon = Number(v.lon ?? v.lng ?? v.longitude ?? v.center_lon ?? v.x ?? 0);
        const cover = Number(v.cloud_total ?? v.total ?? v.level ?? v.opacity ?? v.cloudFraction ?? 0);
        return `${Math.round(lat * 10)}:${Math.round(lon * 10)}:${Math.round(cover / 5)}`;
      };
      const first = count ? pick(features[0]) : '';
      const mid = count > 2 ? pick(features[Math.floor(count / 2)]) : '';
      const last = count ? pick(features[count - 1]) : '';
      const layers = Array.isArray(payload?.cloud_layers) ? payload.cloud_layers.map((l) => `${l?.name || ''}:${Array.isArray(l?.density) ? l.density.length : 0}:${Array.isArray(l?.density?.[0]) ? l.density[0].length : 0}`).join('|') : '';
      // Ignore valid_time/source_time/resolved_time/cache version churn. The cloud shell layer is persistent:
      // redraw only when coarse drawable geometry changes, otherwise hold the existing map shells.
      return [bbox, count, payload?.cloud_region_count || payload?.sea_feature_count || '', layers, first, mid, last].join('|');
    },
    renderer: ({ payload, map3DElement }) => renderCloudZones({ payload, map3DElement }),
    clearBeforeRender: false,
    preserveOnEmpty: true,
    minRenderIntervalMs: Number(window.__GFS_CLOUD_MIN_RENDER_INTERVAL_MS || 600000),
  });
  const baseCloudHide = clouds.hide.bind(clouds);
  clouds.hide = () => {
    try { baseCloudHide(); } catch (err) { console.warn('[gfs clouds] base hide failed', err); }
    try { window.clearGfsCloudLayer?.(); } catch (_) {}
    try { globeEl?.querySelectorAll?.('[data-gfs-layer="clouds"],[data-cloud-shell],[data-cloud-particle],[data-gfs-layer="ocean-fx"],[data-ocean-fx-kind]')?.forEach((el) => el.remove()); } catch (_) {}
  };

  const rain = new RendererLayer(globeEl, {
    name: 'rain',
    selector: (frame) => {
      const weather = frame?.weather || null;
      const cloudsPayload = frame?.clouds || null;
      if (!weather && !cloudsPayload) return null;
      return {
        ...(weather || {}),
        bbox: weather?.bbox || weather?.bbox_used || cloudsPayload?.bbox || cloudsPayload?.bbox_used || frame?.bbox || frame?.meta?.bbox,
        bbox_used: weather?.bbox_used || cloudsPayload?.bbox_used || cloudsPayload?.bbox || frame?.bbox || frame?.meta?.bbox_used,
        fields: nonEmptyFields(weather?.fields, cloudsPayload?.fields, frame?.fields),
        cloud_layers: cloudsPayload?.cloud_layers || weather?.cloud_layers || [],
        items: cloudsPayload?.items || cloudsPayload?.tiles || weather?.items || [],
        precip_columns: cloudsPayload?.precip_columns || cloudsPayload?.scene?.precip || weather?.precip_columns || [],
      };
    },
    renderer: ({ payload, map3DElement, viewportReason }) => renderRainZones({ payload, map3DElement, viewportReason: viewportReason === 'boot' ? 'steady' : viewportReason }),
  });
  const lightning = new RendererLayer(globeEl, {
    name: 'lightning',
    selector: (frame) => {
      if (isLayerUserDisabled('lightning')) return null;
      const payload = frame?.lightning || null;
      if (!payload) return null;
      if (payload?.heuristic === true || payload?.mock === true || payload?.proxy === true || payload?.fallback_used === true) return null;
      return payload;
    },
    renderer: ({ payload, map3DElement }) => renderLightningLayer({ payload, map3DElement }),
    signature: (_frame, payload) => [
      Array.isArray(payload?.bbox) ? payload.bbox.join(',') : '',
      payload?.generated_at || payload?.valid_time || '',
      payload?.payload_state || '',
      Array.isArray(payload?.flashes) ? payload.flashes.length : 0,
      Array.isArray(payload?.regions) ? payload.regions.length : 0,
    ].join('|'),
  });

  const bait = new RendererLayer(globeEl, {
    name: 'bait',
    clearBeforeRender: false,
    preserveOnEmpty: true,
    selector: (frame) => {
      const advanced = frame?.baitAdvanced || null;
      if (hasStrictBaitPolygons(advanced)) return advanced;
      return null;
    },
    renderer: ({ payload, map3DElement, viewportReason }) => renderBaitZonesSafe({ payload, map3DElement, viewportReason: viewportReason === 'boot' ? 'steady' : viewportReason }),
  });

  const sharkIntel = new RendererLayer(globeEl, {
    name: 'shark-intel',
    clearBeforeRender: false,
    preserveOnEmpty: true,
    selector: (frame) => {
      if (isLayerUserDisabled('shark-intel')) return null;
      const payload = frame?.sharkIntel || dataState.latest.sharkIntel || null;
      if (!payload) return null;
      const contours = Array.isArray(payload?.contours) ? payload.contours : (Array.isArray(payload?.polygons) ? payload.polygons : []);
      const points = Array.isArray(payload?.score_points) ? payload.score_points : [];
      if (!contours.length && !points.length) return null;
      return payload;
    },
    renderer: ({ payload, map3DElement }) => renderSharkIntelLayerSafe({ payload, map3DElement }),
    signature: (_frame, payload) => [
      payload?.cache?.version || payload?.cache_quality?.version || payload?.version || '',
      payload?.resolved_time || '',
      Array.isArray(payload?.contours) ? payload.contours.length : 0,
      Array.isArray(payload?.score_points) ? payload.score_points.length : 0,
    ].join('|'),
  });


  const boater = new RendererLayer(globeEl, {
    name: 'boater',
    clearBeforeRender: false,
    preserveOnEmpty: true,
    selector: (frame) => {
      const boatsPayload = frame?.boats || null;
      const oceanPayload = frame?.ocean || null;
      const oceanPoints = frame?.oceanPoints || null;
      const boats = Array.isArray(boatsPayload?.boats) ? boatsPayload.boats : (Array.isArray(oceanPayload?.boats) ? oceanPayload.boats : []);
      const basePayload = { ...(oceanPayload || {}), ...(boatsPayload || {}), boats, ocean: oceanPayload, oceanPoints, boatsPayload };
      const officialBoatsPayload = boatsPayload || oceanPayload || oceanPoints;
      if (officialBoatsPayload) {
        // Prefer the official scene-cache boater layer and ocean samples for boater.
        return {
          ...basePayload,
          source: boatsPayload?.source || oceanPayload?.source || 'boater_payload',
          mode: boatsPayload?.mode || oceanPayload?.mode || 'boater_layer_official_boats_first',
        };
      }
      return null;
    },
    renderer: ({ payload, map3DElement, viewportReason }) => {
      const reason = viewportReason === 'boot' ? 'steady' : viewportReason;
      const disposers = [];
      const counts = boatCounts(payload);
      console.info('[gfs boater] render request', {
        reason,
        boatsRaw: counts.raw,
        boatsRenderableHint: counts.renderableHint,
        boatsFallbackRejected: counts.fallbackRejected,
        source: counts.source,
        hasModelRenderer: true,
        hasPolygonRenderer: true,
      });
      const currentZonesDispose = renderCurrentZonesLayerSafe({ payload, map3DElement, viewportReason: reason });
      if (typeof currentZonesDispose === 'function') disposers.push(currentZonesDispose);
      // Boater now renders as interpolated HYCOM/RTOFS current contour zones plus boat models.
      // Point-orb rendering is intentionally disabled here; current structure is shown as
      // marching-squares zones so the pill is not a sparse point grid.
      const boatsDispose = renderBoatsLayerSafe({ payload, map3DElement });
      if (typeof boatsDispose === 'function') disposers.push(boatsDispose);
      const disposer = () => {
        disposers.reverse().forEach((dispose) => {
          try { dispose(); } catch (err) { console.warn('[gfs boater] dispose failed', err); }
        });
      };
      // Boater owns two internal reconcilers: current polygons and boat models.
      // The layer engine must keep the same clear disposer across payloads so a
      // scene-cache heartbeat updates objects instead of duplicating/clearing them.
      disposer.__gfsKeepExisting = true;
      disposer.__gfsDidRender = true;
      return disposer;
    },
  });

  const inlandWater = new RendererLayer(globeEl, {
    name: 'inland-water',
    selector: (frame) => {
      if (isLayerUserDisabled('inland-water')) return null;
      if (!isInlandOverviewViewport(getCanonicalViewport())) return null;
      const water = frame?.inlandWater || dataState.latest.inlandWater || null;
      if (!water) return null;
      return {
        ...water,
        conditions: frame?.inlandConditions || dataState.latest.inlandConditions || null,
        bait: frame?.inlandBait || dataState.latest.inlandBait || null,
      };
    },
    renderer: ({ payload, map3DElement }) => renderInlandWaterLayerSafe({ payload, map3DElement }),
    // Inland lakes are semi-static. Do not include camera range, bait circle counts,
    // or warming/enrichment churn in the signature; that was causing California lakes
    // to flash/disappear/remap during normal cache updates. Temperature labels and
    // marching-square thermal bait still refresh when real NCSS point count/version changes.
    clearBeforeRender: false,
    preserveOnEmpty: true,
    minRenderIntervalMs: 30000,
    signature: (_frame, payload) => [
      Array.isArray(payload?.bbox) ? payload.bbox.join(',') : '',
      payload?.cache?.disk_signature || payload?.cache?.version || payload?.cache_quality?.version || payload?.tile_version || '',
      Array.isArray(payload?.polygons) ? payload.polygons.length : 0,
      Array.isArray(payload?.lines) ? payload.lines.length : 0,
      payload?.world_quantity_filter?.policy || payload?.diagnostics?.quantity_filter?.policy || '',
      payload?.world_quantity_filter?.output ?? payload?.diagnostics?.quantity_filter?.output ?? '',
      payload?.contract || payload?.source || '',
    ].join('|'),
  });

  // Visual order matters on Maps 3D: fish CSV markers are rendered before
  // this layer system boots, then Jetstream, Bait, Boats, Clouds, and Rain.
  engine.register('jetstream', {
    async show(){
      if (layerUserPrefs().jetstream === false) return;
      window.__gfsJetstreamPillOn = true;
      if (typeof window.setJetBalloonsEnabled === 'function') await window.setJetBalloonsEnabled(true);
      else window.__gfsLayerState = { ...(window.__gfsLayerState || {}), jetstreamBalloons: true };
    },
    async hide(){
      window.__gfsLayerState = { ...(window.__gfsLayerState || {}), jetstreamBalloons: false };
      window.__gfsJetstreamDisabled = true;
      window.__gfsJetstreamPillOn = false;
      jetstreamVisualReady = false;
      lastJetstreamEnsureAt = 0;
      if (typeof window.setJetBalloonsEnabled === 'function') await window.setJetBalloonsEnabled(false);
      if (typeof window.clearJetBalloons === 'function') window.clearJetBalloons();
    },
    update(){}
  });
  engine.register('bait', bait);
  engine.register('shark-intel', sharkIntel);
  engine.register('boater', boater);
  engine.register('inland-water', inlandWater);
  engine.register('clouds', clouds);
  engine.register('rain', rain);
  engine.register('lightning', lightning);
  layerRuntime.engine = engine;
  layerRuntime.world = new WorldSubscriptionRenderer({
    engine,
    getViewport: () => getCanonicalViewport(),
    readSceneCache: (...args) => refreshMainSceneCache(...args),
    nudgeRefresh: (...args) => nudgeMainSceneCacheRefresh(...args),
    clearVisuals: (name) => clearLayerVisuals(name),
    syncPill: (name, enabled) => syncPillState(name, enabled),
    rememberChoice: (name, enabled) => rememberLayerChoice(name, enabled),
    renderLocations: (items) => renderLocationsSubscription(items),
    latestLocations: () => latestLocations,
    layerPrefs: () => layerUserPrefs(),
    defaultEnabled: (name) => layerDefaultEnabled(name),
    isUnavailable: (name) => isLayerUnavailable(name),
    refreshInland: (...args) => refreshDeferredInlandWater(...args),
    ensureJetstream: (...args) => ensureJetstreamVisual(...args),
    debug: (topic, detail) => debugPanelEvent(topic, detail),
  });
  document.querySelectorAll('.overlay-pill[data-layer]').forEach((btn) => {
    btn.addEventListener('click', (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const name = btn.dataset.layer;
      if (name === 'inland-water' && (ev.detail || 1) < 2) {
        // Inland Waters is diagnostic/first-run critical: keep it ON by default.
        // A single click reasserts the subscription and queues read/build progress;
        // a double click remains the intentional off switch.
        layerRuntime.world?.setLayerEnabled?.(name, true, { reason: 'inland_single_click_keep_on', subscribe: true })
          ?.catch?.(() => {});
        return;
      }
      togglePillLayer(name, btn, engine);
    });
  });
  const partyBtn = document.getElementById('partyTimePill');
  if (partyBtn && !partyBtn.__gfsPartyBound) {
    partyBtn.__gfsPartyBound = true;
    partyBtn.addEventListener('click', (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      togglePartyTimePill();
    });
  }
  syncPartyTimePill(isPartyTimeEnabled());
  try { window.setCloudPartyMode?.(isPartyTimeEnabled()); } catch (_) {}
  ['jetstream','bait','shark-intel','boater','inland-water','clouds','rain','lightning'].forEach((name) => {
    const enabled = name === 'inland-water' ? (layerUserPrefs()[name] !== false && isInlandOverviewViewport(getCanonicalViewport())) : (layerUserPrefs()[name] !== false && !isLayerUnavailable(name));
    layerRuntime.world?.setLayerEnabled?.(name, enabled, { reason: 'init_layer_system', subscribe: false })
      ?.catch?.(() => {});
  });
  syncPillState('locations', layerUserPrefs().locations !== false);
  enforceInlandZoomGate(getCanonicalViewport(), 'init_layer_system');
  const tick = () => {
    layerRuntime.rafId = window.requestAnimationFrame(tick);
    engine.update();
  };
  tick();
  return engine;
}

// frontend contract: stale response discarded when seq !== overlayState.requestSeq
const dataState = {
  inFlight: false,
  requestSeq: 0,
  activeAbort: null,
  lastSignature: '',
  lastHeavySignature: '',
  lastCloudSignature: '',
  lastBoatSignature: '',
  latest: {
    bbox: null,
    weather: null,
    clouds: null,
    baitBase: null,
    baitAdvanced: null,
    boats: null,
    oceanPoints: null,
    inlandWater: null,
    inlandConditions: null,
    inlandBait: null,
  },
};

window.__gfsDataInduction = {
  get latest() {
    return dataState.latest;
  },
  get signature() {
    return dataState.lastSignature;
  },
  refresh(reason = 'manual') {
    return refreshData(reason);
  },
};

function showStatus(text) {
  const skyLine = statusEl?.dataset?.skyLine || '';
  statusEl.textContent = skyLine ? `${text} • ${skyLine}` : text;
}

function hideLiveOverlay() {
  const overlay = document.getElementById('liveOverlay');
  if (overlay) overlay.remove();
}

function showLiveOverlay() {
  if (liveManuallyDismissed) return;
  let overlay = document.getElementById('liveOverlay');
  if (overlay) return;

  overlay = document.createElement('div');
  overlay.id = 'liveOverlay';
  overlay.className = 'live-overlay';
  overlay.innerHTML = `
    <div class="live-card glass">
      <div class="live-header">
        <div>
          <div class="eyebrow">Live</div>
          <strong>Broadcast preview</strong>
        </div>
        <button id="liveOverlayClose" type="button">Close</button>
      </div>
      <video id="livePreview" autoplay playsinline muted></video>
    </div>`;
  document.body.appendChild(overlay);

  overlay.querySelector('#liveOverlayClose')?.addEventListener('click', async () => {
    liveManuallyDismissed = true;
    if (activeLocation) await stopLive({ locationId: activeLocation.id });
    hideLiveOverlay();
  });
}

function currentLiveOverlayRefs() {
  return {
    overlayEl: document.getElementById('liveOverlay'),
    videoEl: document.getElementById('livePreview'),
  };
}

async function refreshSelectedLiveState() {
  if (!selectedLocation) return;
  const locationId = encodeURIComponent(selectedLocation.id);
  if (missingLiveLocationIds.has(selectedLocation.id)) return;
  const payload = await getJsonSafe(`/gfs/api/location/${locationId}/live`, null);
  if (!payload) return;
  if (payload?.error === 'location_not_found' || payload?.ok === false) {
    missingLiveLocationIds.add(selectedLocation.id);
    console.info('[gfs live] stop polling missing location', { locationId: selectedLocation.id });
    return;
  }
  if (payload?.live?.active) showLiveOverlay();
  else hideLiveOverlay();
}

function startLivePolling() {
  if (liveStatePollId) return;
  liveStatePollId = setInterval(refreshSelectedLiveState, 12000);
}

function stopLivePolling() {
  if (!liveStatePollId) return;
  clearInterval(liveStatePollId);
  liveStatePollId = null;
}

function parseCenter() {
  const prop = globeEl.center || globeEl.camera?.center || null;
  const plat = Number(prop?.lat);
  const plon = Number(prop?.lng ?? prop?.lon);
  if (Number.isFinite(plat) && Number.isFinite(plon)) return { lat: plat, lon: plon };

  const raw = globeEl.getAttribute('center') || '';
  const parts = raw.split(',').map((x) => Number(x.trim()));
  if (parts.length >= 2 && Number.isFinite(parts[0]) && Number.isFinite(parts[1])) {
    return { lat: parts[0], lon: parts[1] };
  }
  return { lat: 34.2, lon: -120 };
}

function viewportCenterSafe(viewport = null) {
  const cam = viewport?.camera?.center || viewport?.center || null;
  const camLat = Number(cam?.lat);
  const camLon = Number(cam?.lon ?? cam?.lng);
  if (Number.isFinite(camLat) && Number.isFinite(camLon)) return { lat: camLat, lon: camLon };
  const south = Number(viewport?.south);
  const north = Number(viewport?.north);
  const west = Number(viewport?.west);
  const east = Number(viewport?.east);
  if ([south, north, west, east].every(Number.isFinite)) {
    return { lat: (south + north) / 2, lon: normalizeLongitude((west + east) / 2) };
  }
  const fetch = viewport?.fetch_bbox || viewport?.fetchBbox || null;
  if (fetch) return viewportCenterSafe(fetch);
  return parseCenter();
}

function parseRangeMeters() {
  const prop = Number(globeEl.range ?? globeEl.camera?.range);
  if (Number.isFinite(prop) && prop > 0) return prop;
  const attr = Number(globeEl.getAttribute('range'));
  if (Number.isFinite(attr) && attr > 0) return attr;
  return 1800000;
}

function parseCameraAngleSignature() {
  const heading = Number(globeEl.heading ?? globeEl.camera?.heading ?? globeEl.getAttribute('heading') ?? 0);
  const tilt = Number(globeEl.tilt ?? globeEl.camera?.tilt ?? globeEl.getAttribute('tilt') ?? 0);
  const roll = Number(globeEl.roll ?? globeEl.camera?.roll ?? globeEl.getAttribute('roll') ?? 0);
  return {
    heading: Number.isFinite(heading) ? heading : 0,
    tilt: Number.isFinite(tilt) ? tilt : 0,
    roll: Number.isFinite(roll) ? roll : 0,
  };
}

function parseBoundsAttr(raw) {
  if (!raw || typeof raw !== 'string') return null;
  const parts = raw.split(',').map((x) => Number(x.trim()));
  if (parts.length < 4 || parts.some((n) => !Number.isFinite(n))) return null;
  return { west: parts[0], south: parts[1], east: parts[2], north: parts[3] };
}

function trueViewportFromGlobeBounds() {
  const attrCandidates = [
    globeEl.getAttribute('bounds'),
    globeEl.getAttribute('view-bounds'),
    globeEl.getAttribute('visible-bounds'),
  ];
  for (const raw of attrCandidates) {
    const parsed = parseBoundsAttr(raw);
    if (parsed) return parsed;
  }
  if (typeof globeEl.getBounds === 'function') {
    try {
      const b = globeEl.getBounds();
      if (b && Number.isFinite(b.west) && Number.isFinite(b.south) && Number.isFinite(b.east) && Number.isFinite(b.north)) {
        return { west: b.west, south: b.south, east: b.east, north: b.north };
      }
    } catch (_) {}
  }
  return null;
}

function viewportAspectRatioSafe() {
  try {
    const rect = globeEl?.getBoundingClientRect?.();
    const w = Number(rect?.width || window.innerWidth || 1);
    const h = Number(rect?.height || window.innerHeight || 1);
    if (Number.isFinite(w) && Number.isFinite(h) && w > 0 && h > 0) return Math.max(0.5, Math.min(3.5, w / h));
  } catch (_) {}
  return 16 / 9;
}


function clampNumber(value, minValue, maxValue) {
  const n = Number(value);
  if (!Number.isFinite(n)) return minValue;
  return Math.max(minValue, Math.min(maxValue, n));
}

function normalizeLongitude(lon) {
  const n = Number(lon);
  if (!Number.isFinite(n)) return 0;
  const wrapped = ((n + 180) % 360 + 360) % 360 - 180;
  return Object.is(wrapped, -180) ? 180 : wrapped;
}

function destinationPointDegrees(lat, lon, bearingDeg, distanceKm) {
  const R = 6371.0088;
  const d = Number(distanceKm) / R;
  const brng = (Number(bearingDeg) || 0) * Math.PI / 180;
  const φ1 = clampNumber(lat, -89.8, 89.8) * Math.PI / 180;
  const λ1 = normalizeLongitude(lon) * Math.PI / 180;
  const sinφ1 = Math.sin(φ1);
  const cosφ1 = Math.cos(φ1);
  const sinD = Math.sin(d);
  const cosD = Math.cos(d);
  const φ2 = Math.asin((sinφ1 * cosD) + (cosφ1 * sinD * Math.cos(brng)));
  const λ2 = λ1 + Math.atan2(Math.sin(brng) * sinD * cosφ1, cosD - sinφ1 * Math.sin(φ2));
  return {
    lat: clampNumber(φ2 * 180 / Math.PI, -89.8, 89.8),
    lon: normalizeLongitude(λ2 * 180 / Math.PI),
  };
}

function bboxFromPoints(points = []) {
  const clean = points.filter((p) => p && Number.isFinite(Number(p.lat)) && Number.isFinite(Number(p.lon)));
  if (!clean.length) return null;
  const lats = clean.map((p) => Number(p.lat));
  // Keep the current app's normal non-dateline western-US use case simple.
  // If a future dateline viewport appears, the request path already clamps to
  // -179.9/179.9 and can split server-side.
  const lons = clean.map((p) => normalizeLongitude(Number(p.lon)));
  return {
    west: clampNumber(Math.min(...lons), -179.9, 179.9),
    south: clampNumber(Math.min(...lats), -89.9, 89.9),
    east: clampNumber(Math.max(...lons), -179.9, 179.9),
    north: clampNumber(Math.max(...lats), -89.9, 89.9),
  };
}

function projectedCameraViewport() {
  const c = parseCenter();
  const range = parseRangeMeters();
  const aspect = viewportAspectRatioSafe();
  const angles = parseCameraAngleSignature();
  const heading = Number.isFinite(Number(angles.heading)) ? Number(angles.heading) : 0;
  const tilt = clampNumber(angles.tilt, 0, 80);
  const rangeKm = clampNumber(range / 1000, 0.5, 6500);
  const tilt01 = Math.max(0, Math.min(1, Math.sin((tilt * Math.PI) / 180)));

  // Tilted 3D cameras see forward along heading, not centered directly under
  // the camera center.  This shoots the data footprint toward the horizon while
  // keeping the near camera center in the bbox so bottom-screen data remains.
  const forwardKm = rangeKm * (0.18 + 0.58 * tilt01);
  const depthKm = rangeKm * (0.72 + 0.72 * tilt01);
  const widthKm = rangeKm * (0.78 + 0.25 * Math.max(0.75, Math.min(2.4, aspect)));
  const target = destinationPointDegrees(c.lat, c.lon, heading, forwardKm);
  const nearCenter = destinationPointDegrees(c.lat, c.lon, heading, Math.max(0, forwardKm - depthKm * 0.52));
  const farCenter = destinationPointDegrees(c.lat, c.lon, heading, forwardKm + depthKm * 0.58);
  const rightBearing = heading + 90;
  const leftBearing = heading - 90;
  const points = [
    c,
    target,
    destinationPointDegrees(nearCenter.lat, nearCenter.lon, leftBearing, widthKm * 0.46),
    destinationPointDegrees(nearCenter.lat, nearCenter.lon, rightBearing, widthKm * 0.46),
    destinationPointDegrees(farCenter.lat, farCenter.lon, leftBearing, widthKm * 0.58),
    destinationPointDegrees(farCenter.lat, farCenter.lon, rightBearing, widthKm * 0.58),
  ];
  const box = bboxFromPoints(points);
  if (!box) return null;
  // Keep world-start requests useful.  Giant 140-degree boxes were over-broad,
  // caused HYCOM to serve a coarse middle slice, and made the visible footprint
  // feel offset.  Projected bboxes are intentionally screen/frustum-sized.
  const maxLonSpan = tilt >= 30 ? 72 : 56;
  const maxLatSpan = tilt >= 30 ? 44 : 36;
  let west = box.west, east = box.east, south = box.south, north = box.north;
  const cx = normalizeLongitude((west + east) / 2);
  const cy = (south + north) / 2;
  const lonSpan = Math.abs(east - west);
  const latSpan = Math.abs(north - south);
  if (lonSpan > maxLonSpan) {
    west = cx - maxLonSpan / 2;
    east = cx + maxLonSpan / 2;
  }
  if (latSpan > maxLatSpan) {
    south = cy - maxLatSpan / 2;
    north = cy + maxLatSpan / 2;
  }
  return snapViewport({
    west: clampNumber(west, -179.9, 179.9),
    south: clampNumber(south, -89.9, 89.9),
    east: clampNumber(east, -179.9, 179.9),
    north: clampNumber(north, -89.9, 89.9),
    quality: 'high',
    earth_priority: 'high_res_first',
    camera: { center: c, range, ...angles, source: 'camera_heading_tilt_projected_viewport' },
    projectedCenter: target,
    projectionContract: 'bbox_contract_v4_camera_heading_tilt_frustum_shot',
    viewportHeuristic: 'camera_heading_tilt_projection_v4',
    viewportAspect: Number(aspect.toFixed(3)),
    forwardKm: Number(forwardKm.toFixed(1)),
    depthKm: Number(depthKm.toFixed(1)),
    widthKm: Number(widthKm.toFixed(1)),
  });
}

function getCanonicalViewport() {
  const c = parseCenter();
  const range = parseRangeMeters();
  const angles = parseCameraAngleSignature();
  const fromBounds = trueViewportFromGlobeBounds();
  if (fromBounds) {
    const rawLatSpan = Math.max(0.1, Math.abs(fromBounds.north - fromBounds.south));
    const rawLonSpan = Math.max(0.1, Math.abs(fromBounds.east - fromBounds.west));
    if (rawLatSpan <= 170 && rawLonSpan <= 360) {
      const out = snapViewport({
        west: Math.max(-179.9, Math.min(179.9, fromBounds.west)),
        south: Math.max(-89.9, Math.min(89.9, fromBounds.south)),
        east: Math.max(-179.9, Math.min(179.9, fromBounds.east)),
        north: Math.max(-89.9, Math.min(89.9, fromBounds.north)),
        quality: 'high',
        earth_priority: 'high_res_first',
        camera: { center: c, range, ...angles, source: 'visible_bounds_full_viewport' },
        projectionContract: 'bbox_contract_v4_real_visible_bounds',
      });
      publishViewportGlobals(out, 'canonical_visible_bounds_full_viewport');
      return out;
    }
  }

  const projected = projectedCameraViewport();
  if (projected) {
    publishViewportGlobals(projected, 'canonical_camera_heading_tilt_projected_viewport');
    return projected;
  }

  const aspect = viewportAspectRatioSafe();
  const tilt = Number(angles.tilt || 0);
  const tiltBoost = 1 + Math.max(0, Math.min(0.75, tilt / 80));
  const latSpan = Math.max(1.8, Math.min(56, (range / 52000) * tiltBoost));
  const lonSpan = Math.max(2.4, Math.min(72, latSpan * aspect / Math.max(Math.cos((c.lat * Math.PI) / 180), 0.30)));
  const out = snapViewport({
    west: Math.max(-179.9, c.lon - lonSpan / 2),
    south: Math.max(-89.9, c.lat - latSpan / 2),
    east: Math.min(179.9, c.lon + lonSpan / 2),
    north: Math.min(89.9, c.lat + latSpan / 2),
    quality: 'high',
    earth_priority: 'high_res_first',
    camera: { center: c, range, ...angles, source: 'camera_full_viewport_heuristic_fallback' },
    viewportHeuristic: 'full_screen_bbox_v4_fallback',
    viewportAspect: Number(aspect.toFixed(3)),
  });
  publishViewportGlobals(out, 'canonical_camera_full_viewport_heuristic_fallback');
  return out;
}

function effectiveViewportStride(v) {
  const span = Math.max(
    Math.max(0.0001, v.east - v.west),
    Math.max(0.0001, v.north - v.south),
  );
  if (span > 14) return 4;
  if (span > 6) return 2;
  return 1;
}

function snapToGrid(value, step) {
  return Math.round(value / step) * step;
}

function snapViewport(v) {
  const stride = effectiveViewportStride(v);
  const step = 0.25 * stride;
  return {
    ...v,
    west: Math.max(-179.9, snapToGrid(v.west, step)),
    south: Math.max(-89.9, snapToGrid(v.south, step)),
    east: Math.min(179.9, snapToGrid(v.east, step)),
    north: Math.min(89.9, snapToGrid(v.north, step)),
    sourceStride: stride,
  };
}

function bboxToQuery(b) {
  const box = Array.isArray(b)
    ? { west: Number(b[0]), south: Number(b[1]), east: Number(b[2]), north: Number(b[3]) }
    : (b || {});
  const west = Number(box.west);
  const south = Number(box.south);
  const east = Number(box.east);
  const north = Number(box.north);
  if (![west, south, east, north].every(Number.isFinite)) return '-126.0000,29.0000,-114.0000,39.0000';
  return `${west.toFixed(4)},${south.toFixed(4)},${east.toFixed(4)},${north.toFixed(4)}`;
}

function bboxString(b, decimals = 4) {
  const box = Array.isArray(b)
    ? { west: Number(b[0]), south: Number(b[1]), east: Number(b[2]), north: Number(b[3]) }
    : (b || {});
  const digits = Math.max(0, Math.min(6, Number(decimals) || 4));
  const west = Number(box.west);
  const south = Number(box.south);
  const east = Number(box.east);
  const north = Number(box.north);
  if (![west, south, east, north].every(Number.isFinite)) return bboxToQuery(box);
  return [west, south, east, north].map((v) => v.toFixed(digits)).join(',');
}


function sceneTierForViewport(viewport) {
  const v = viewport || getCanonicalViewport();
  const width = Math.abs(Number(v.east) - Number(v.west));
  const height = Math.abs(Number(v.north) - Number(v.south));
  const span = Math.max(width || 0, height || 0);
  const area = Math.max(0, width * height);
  if (span <= 1.6 && area <= 2.6) return 'harbor';
  if (span <= 4.0 && area <= 14.0) return 'coastal';
  if (span <= 12.0 && area <= 90.0) return 'regional';
  return 'world';
}

function sceneQueryParams(fetchViewport, visibleViewport) {
  const visible = visibleViewport || fetchViewport || getCanonicalViewport();
  const tier = sceneTierForViewport(visible);
  const visibleQ = encodeURIComponent(bboxToQuery(visible));
  return `visible_bbox=${visibleQ}&scene_tier=${encodeURIComponent(tier)}`;
}


// Inland/lake detail is intentionally zoom-gated for graphics load and readability.
// Global/world views should stay light: earth + weather + fish/temp summary only.
// Lake geometry, inland-water temperature labels, and inland bait/targets are allowed
// only when the visible camera is harbor/coastal(local)/regional detail.
const INLAND_DETAIL_TIERS = Object.freeze(new Set(['harbor', 'local', 'coastal', 'regional']));
const INLAND_OVERVIEW_TIERS = Object.freeze(new Set(['world', 'overview', 'regional', 'coastal', 'local', 'harbor']));
function isInlandDetailTier(tier) {
  return INLAND_DETAIL_TIERS.has(String(tier || '').toLowerCase());
}
function isInlandOverviewTier(tier) {
  return INLAND_OVERVIEW_TIERS.has(String(tier || '').toLowerCase());
}
function isInlandDetailViewport(viewport = null) {
  return isInlandDetailTier(sceneTierForViewport(viewport || getCanonicalViewport()));
}
function isInlandOverviewViewport(viewport = null) {
  return isInlandOverviewTier(sceneTierForViewport(viewport || getCanonicalViewport()));
}
function inlandZoomGateDetail(viewport = null) {
  const vp = viewport || getCanonicalViewport();
  const tier = sceneTierForViewport(vp);
  return {
    allowed: isInlandOverviewTier(tier),
    baitAllowed: isInlandDetailTier(tier),
    overviewOnly: !isInlandDetailTier(tier),
    tier,
    bbox: bboxToQuery(vp),
    policy: 'world/regional overview renders lake outlines + one lake-temp label per tile; inland bait contours wait for regional/coastal/local/harbor zoom',
  };
}
function clearInlandBaitForZoomGate(reason = 'world_overview_bait_gate') {
  try { clearLayerVisuals('inland-bait'); } catch (_) {}
  try {
    dataState.latest.inlandBait = null;
    dataState.latest.inlandConditions = null;
    if (dataState.latest.frame) {
      delete dataState.latest.frame.inlandBait;
      delete dataState.latest.frame.inlandConditions;
      dataState.latest.frame.meta = { ...(dataState.latest.frame.meta || {}), inland_bait_zoom_gate: reason, inland_bait_zoom_gate_ts: Date.now() };
    }
  } catch (_) {}
  try { debugPanelEvent('inland-water/bait-zoom-gated-clear', { reason, ...inlandZoomGateDetail() }); } catch (_) {}
}
function clearInlandForZoomGate(reason = 'world_zoom_gate') {
  // Compatibility alias: world zoom now preserves inland outlines/temp overview; only bait/conditions are cleared.
  return clearInlandBaitForZoomGate(reason);
}
function enforceInlandZoomGate(viewport = null, reason = 'viewport') {
  const gate = inlandZoomGateDetail(viewport);
  try { window.__gfsInlandZoomGate = gate; } catch (_) {}
  if (!gate.baitAllowed) {
    clearInlandBaitForZoomGate(reason);
    try { syncPillState('inland-water', layerUserPrefs()['inland-water'] !== false); } catch (_) {}
  }
  return gate.allowed;
}

function inlandWaterGeometryForTier(tier) {
  // World is a discovery/overview layer: fast real USGS coarse geometry.
  // Regional/coastal/local/harbor keep vector detail for lake vertices and bait.
  return String(tier || 'world').toLowerCase() === 'world' ? 'coarse' : 'vector';
}


function clampInlandViewportSpan(v, reason = 'inland') {
  if (!v) return v;
  const west = Number(v.west), south = Number(v.south), east = Number(v.east), north = Number(v.north);
  if (![west, south, east, north].every(Number.isFinite)) return v;
  // Keep inland water viewport-first and full-screen.  The previous centroid
  // clamp intentionally shrank large views to a 2/4/8/14 degree center window,
  // which made shoreline/water/temp requests look like about a quarter of the
  // visible screen.  World-scale noise is now controlled server-side by the
  // largest-lake-per-tile filter, so client bbox shrinking is no longer needed.
  return {
    ...v,
    west: Math.max(-179.9, Math.min(179.9, west)),
    east: Math.max(-179.9, Math.min(179.9, east)),
    south: Math.max(-89.9, Math.min(89.9, south)),
    north: Math.max(-89.9, Math.min(89.9, north)),
    inlandClamped: false,
    inlandClampReason: reason,
    inlandFullViewportBbox: true,
  };
}

function compactViewportForShoreline(viewport, reason = 'shoreline') {
  // Shoreline/water geometry now uses the exact full visible viewport bbox.
  // We avoid both weather expansion and center-window shrinking here; tile
  // quantity is handled by active LOD + largest-lake-per-tile filtering.
  const nestedVisible = viewport?.fetch_bbox?.visibleBbox || viewport?.fetch_bbox?.visible_bbox || viewport?.fetch_bbox?.visible_bbox;
  const raw = viewport?.visibleBbox || viewport?.visible_bbox || nestedVisible || viewport || getCanonicalViewport();
  const v = clampInlandViewportSpan(raw, reason);
  return snapViewport({
    ...v,
    shorelineBbox: true,
    shorelinePadFactor: 1.0,
    shorelineFullViewportBbox: true,
  });
}

function expandedViewportForFetch(viewport, reason = 'fetch') {
  const v = viewport || getCanonicalViewport();
  const west = Number(v.west);
  const south = Number(v.south);
  const east = Number(v.east);
  const north = Number(v.north);
  if (![west, south, east, north].every(Number.isFinite)) return v;
  const width = Math.max(0.02, Math.abs(east - west));
  const height = Math.max(0.02, Math.abs(north - south));
  const cam = v.camera || {};
  const tilt = Number(cam.tilt ?? cam.headingTilt ?? 0);
  const range = Number(cam.range ?? parseRangeMeters?.() ?? 0);
  let factor = 2.15;
  if (Number.isFinite(tilt) && tilt > 35) factor += Math.min(1.4, (tilt - 35) / 28);
  if (Number.isFinite(range) && range > 2500000) factor += 0.35;
  if (String(reason || '').includes('cache')) factor += 0.35;
  factor = Math.max(1.8, Math.min(3.9, factor));
  const cx = (west + east) / 2;
  const cy = (south + north) / 2;
  const next = {
    ...v,
    west: Math.max(-179.9, cx - (width * factor / 2)),
    east: Math.min(179.9, cx + (width * factor / 2)),
    south: Math.max(-89.9, cy - (height * factor / 2)),
    north: Math.min(89.9, cy + (height * factor / 2)),
    fetchExpanded: true,
    visibleBbox: { west, south, east, north },
    fetchPadFactor: Number(factor.toFixed(2)),
  };
  return snapViewport(next);
}


function viewportClone(v) {
  if (!v || typeof v !== 'object') return null;
  const out = {
    west: Number(v.west),
    south: Number(v.south),
    east: Number(v.east),
    north: Number(v.north),
    quality: v.quality || 'coarse',
    sourceStride: v.sourceStride,
    scene_tier: v.scene_tier || v.sceneTier,
  };
  if (![out.west, out.south, out.east, out.north].every(Number.isFinite)) return null;
  if (v.camera) out.camera = v.camera;
  if (v.visibleBbox) out.visibleBbox = v.visibleBbox;
  if (v.fetchPadFactor) out.fetchPadFactor = v.fetchPadFactor;
  if (v.fetchExpanded) out.fetchExpanded = true;
  if (v.shorelineBbox) out.shorelineBbox = true;
  return out;
}

function layerSetHasAny(layers = [], names = []) {
  const normalized = new Set((layers || []).map(normalizeSceneLayerName));
  return names.some((name) => normalized.has(name));
}

function filterSceneLayersForInlandZoomGate(layers = [], viewport = null, reason = 'scene_layers') {
  const input = [...new Set((layers || []).map(normalizeSceneLayerName).filter(Boolean))];
  const gate = inlandZoomGateDetail(viewport || getCanonicalViewport());
  try {
    window.__gfsInlandZoomGate = gate;
    if (!gate.baitAllowed) clearInlandBaitForZoomGate(`${reason}_bait_gate`);
    debugPanelEvent('inland-water/world-overview-layer-policy', { reason, kept: input, ...gate });
  } catch (_) {}
  // Do not strip inland-water/inland_water_temp at world zoom: they are now cheap overview/priming layers.
  return input;
}

function buildLayerBboxContract(viewport = null, reason = 'scene', layers = []) {
  const visible = viewportClone(viewport || getCanonicalViewport()) || getCanonicalViewport();
  const tier = sceneTierForViewport(visible);
  const layerList = (layers || []).map(normalizeSceneLayerName).filter(Boolean);
  const hasClouds = layerSetHasAny(layerList, ['clouds']);
  const globalCloudBbox = cloudGlobalViewportBbox();
  const weatherFetch = hasClouds ? globalCloudBbox : (viewportClone(expandedViewportForFetch(visible, `weather_${reason}`)) || visible);
  const shoreline = viewportClone(compactViewportForShoreline(visible, `shoreline_${reason}`)) || visible;
  const ocean = visible;
  const jetFetch = hasClouds ? globalCloudBbox : weatherFetch;
  const hasWeather = layerSetHasAny(layerList, ['clouds', 'rain', 'lightning', 'jetstream', 'inland_water_temp']);
  const hasOcean = layerSetHasAny(layerList, ['bait', 'boater', 'shark-intel']);
  const hasInland = layerSetHasAny(layerList, ['inland-water', 'inland_water_temp']);
  const cloudOnlyWeatherRead = hasClouds && !hasOcean && !hasInland;
  const sceneRead = cloudOnlyWeatherRead ? globalCloudBbox : ((hasWeather && !hasOcean && !hasInland) ? weatherFetch : visible);
  const contract = {
    version: 'bbox_contract_v4_projected_frustum',
    policy: 'camera_heading_tilt_projected_visible_footprint_no_centroid_crop_ocean_and_inland_use_projected_bounds_clouds_may_use_global_weather_only',
    reason,
    scene_tier: tier,
    visible_bbox: visible,
    render_bbox: visible,
    scene_read_bbox: sceneRead,
    weather_fetch_bbox: weatherFetch,
    ocean_fetch_bbox: ocean,
    shoreline_bbox: shoreline,
    jetstream_bbox: visible,
    jetstream_fetch_bbox: jetFetch,
    layers: layerList,
  };
  publishViewportGlobals(visible, reason, contract);
  return contract;
}

function publishViewportGlobals(viewport, reason = 'viewport', contract = null) {
  try {
    const visible = viewportClone(viewport || getCanonicalViewport());
    if (!visible) return;
    const nextContract = contract || {
      version: 'bbox_contract_v4_visible_or_projected_only',
      policy: 'visible_bbox_is_full_screen_viewport_until_layer_specific_contract_is_built',
      reason,
      scene_tier: sceneTierForViewport(visible),
      visible_bbox: visible,
      render_bbox: visible,
      scene_read_bbox: visible,
      weather_fetch_bbox: null,
      ocean_fetch_bbox: visible,
      shoreline_bbox: null,
      jetstream_bbox: visible,
      jetstream_fetch_bbox: visible,
      layers: [],
    };
    window.__gfsBboxContract = nextContract;
    window.__gfsLastBbox = bboxToQuery(visible);
    window.currentBBox = window.__gfsLastBbox;
    window.__gfsJetViewportBbox = bboxToQuery(nextContract.jetstream_bbox || visible);
    window.__gfsJetFetchBbox = bboxToQuery(nextContract.jetstream_fetch_bbox || nextContract.jetstream_bbox || visible);
    window.__gfsJetCameraRange = parseRangeMeters?.();
  } catch (_) {}
}

function sanitizeViewportForQuery(viewport) {
  if (!viewport || !Number.isFinite(viewport.west) || !Number.isFinite(viewport.south) || !Number.isFinite(viewport.east) || !Number.isFinite(viewport.north)) {
    throw new Error('Invalid viewport object before frame request');
  }
  const camera = viewport.camera && viewport.camera.center
    ? {
        center: {
          lat: Number(viewport.camera.center.lat),
          lon: Number(viewport.camera.center.lon),
        },
        range: Number(viewport.camera.range),
        tilt: Number(viewport.camera.tilt ?? 0),
        heading: Number(viewport.camera.heading ?? 0),
        roll: Number(viewport.camera.roll ?? 0),
        source: String(viewport.camera.source || ''),
      }
    : null;
  return {
    west: Number(viewport.west),
    south: Number(viewport.south),
    east: Number(viewport.east),
    north: Number(viewport.north),
    quality: String(viewport.quality || 'coarse'),
    visibleBbox: viewport.visibleBbox || viewport.visible_bbox || viewport.visibleBbox || viewport.visible_bbox || null,
    fetch_bbox: viewport.fetch_bbox || viewport.fetchBbox || null,
    scene_tier: viewport.scene_tier || viewport.sceneTier || sceneTierForViewport(viewport),
    camera,
  };
}
function sendViewportPriority(reason = 'steady') {
  try { gfsSocket?.sendViewport?.(getCanonicalViewport(), reason); } catch (_) {}
}

function bboxSignature(b) {
  const range = parseRangeMeters();
  const cam = parseCameraAngleSignature();
  return `${b.west.toFixed(2)}:${b.south.toFixed(2)}:${b.east.toFixed(2)}:${b.north.toFixed(2)}:${Math.round(range / 25000)}:${Math.round(cam.heading / 2)}:${Math.round(cam.tilt / 2)}:${b.sourceStride || 1}`;
}


const CACHE_POP_DELAYS_MS = [800, 7000, 25000];
const OPEN_FAST_MODE = true;
const OPEN_FAST_BOOT_WARM_MAX_TILES = 32;
const OPEN_FAST_STEADY_WARM_MAX_TILES = 64;
const OPEN_FAST_TILE_LOAD_LIMIT = 48;
const OPEN_FAST_FRAME_DELAY_MS = 18000;
// Startup and camera-settle use scene-cache as the only browser data contract.
// Inland waters are a first-class always-on layer.  Do not rely on the
// heavier visual pipeline to start the NHD/ArcGIS downloader: fire a small
// mandatory bootstrap loop that keeps asking until the first partial tile
// is drawable.
const INLAND_MANDATORY_BOOT_DELAYS_MS = [1200, 8000, 18000, 36000, 65000, 120000];
// Track build-cache requests by compact bbox/tier/geometry, not as one global
// boolean. The old single flag could block a later viewport build after an
// empty boot miss.
const inlandBootBuildQueued = new Set();
let inlandBuildPollGeneration = 0;
const VISUAL_PIPELINE_MIN_MS = 2000;
let cachePopGeneration = 0;
let lastCachePopSchedule = { signature: '', at: 0 };

function scheduleCachePopRefreshes(viewport, reason = 'cache_pop') {
  const vp = viewport || getCanonicalViewport();
  const sig = bboxSignature(vp);
  const now = Date.now();
  if (lastCachePopSchedule.signature === sig && (now - lastCachePopSchedule.at) < 2500) {
    console.info('[gfs cache] pop schedule deduped', { reason, signature: sig });
    return;
  }
  lastCachePopSchedule = { signature: sig, at: now };
  const generation = ++cachePopGeneration;
  CACHE_POP_DELAYS_MS.forEach((delayMs) => {
    window.setTimeout(() => {
      if (generation !== cachePopGeneration) return;
      const latestVp = getCanonicalViewport();
      const sigA = bboxSignature(vp);
      const sigB = bboxSignature(latestVp);
      const useVp = sigA === sigB ? latestVp : vp;
      const runner = layerRuntime.world
        ? layerRuntime.world.refreshViewport(useVp, `${reason}_cache_pop_${delayMs}_fast`, { includeStatic: false })
        : refreshMainSceneCache(useVp, `${reason}_cache_pop_${delayMs}_fast`, { mode: 'fast', fast: true, refresh: false });
      runner.catch((err) => console.info('[gfs cache] scene-cache pop skipped', { delayMs, message: err?.message || String(err) }));
    }, delayMs);
  });
}


function forceInlandPillOn(reason = 'mandatory') {
  try { rememberLayerChoice('inland-water', true); } catch (_) {}
  try { window.__gfsLayerUserPrefs = { ...(window.__gfsLayerUserPrefs || {}), 'inland-water': true }; } catch (_) {}
  try { layerRuntime.engine?.setEnabled?.('inland-water', true); } catch (_) {}
  try { syncPillState('inland-water', true); } catch (_) {}
  try {
    document.querySelectorAll?.('.overlay-pill[data-layer="inland-water"]')?.forEach((btn) => {
      btn.classList.add('active');
      btn.classList.remove('unavailable');
      btn.setAttribute('aria-pressed', 'true');
      btn.setAttribute('data-layer-enabled', 'true');
      btn.dataset.enabled = 'true';
      btn.dataset.forceOnReason = reason;
      if (isLayerUnavailable('inland-water')) {
        btn.classList.add('building');
        btn.setAttribute('data-source-status', 'building');
      }
    });
  } catch (_) {}
}


function inlandWaterDrawableCount(payload) {
  try {
    if (!payload || typeof payload !== 'object') return 0;
    const countArray = (v) => Array.isArray(v) ? v.length : 0;
    const countGeoFeatures = (v) => Array.isArray(v?.features) ? v.features.length : 0;
    let total = 0;
    total += countArray(payload.polygons);
    total += countArray(payload.lines);
    total += countArray(payload.features);
    total += countArray(payload.items);
    total += countGeoFeatures(payload.geojson);
    total += countGeoFeatures(payload.geometry);
    total += countArray(payload.waterbodies);
    total += countArray(payload.lakes);
    const numericCount = Number(payload.count ?? payload.rendered ?? payload.drawable_count ?? 0);
    if (Number.isFinite(numericCount) && numericCount > total) total = numericCount;
    return Math.max(0, Math.floor(total));
  } catch (_) {
    return 0;
  }
}

function inlandWaterNeedsBuild(payload) {
  try {
    if (inlandWaterDrawableCount(payload) > 0) return false;
    if (!payload || typeof payload !== 'object') return true;
    const status = String(payload.status || payload.read_status || payload.source || payload.message || '').toLowerCase();
    if (/cache_miss|missing_index|missing_tile|selected_tiles\":0|no matching inland|empty_inland|runtime_nhd/.test(status)) return true;
    const selected = Number(payload?.cache?.selected_tiles ?? payload?.selected_tiles ?? NaN);
    if (Number.isFinite(selected) && selected <= 0) return true;
    return /miss|missing|empty|warming/.test(status);
  } catch (_) {
    return true;
  }
}

function inlandBuildKeyForViewport(vp, geom = null) {
  try {
    const tier = sceneTierForViewport(vp);
    const geometry = geom || inlandWaterGeometryForTier(tier);
    return `${bboxToQuery(vp)}|${tier}|${geometry}`;
  } catch (_) {
    return `${Date.now()}`;
  }
}

function scheduleInlandWaterPostBuildPolls(vp, reason = 'build_poll') {
  const generation = ++inlandBuildPollGeneration;
  [9000, 22000, 45000, 90000].forEach((delayMs) => {
    window.setTimeout(() => {
      if (generation !== inlandBuildPollGeneration && delayMs > 22000) return;
      refreshDeferredInlandWater(vp, `${reason}_poll_${delayMs}`)
        .catch((err) => console.info('[gfs inland-water] post-build poll skipped', { reason, delayMs, message: err?.message || String(err) }));
    }, delayMs);
  });
}

function queueInlandWaterBuild(vp, reason = 'build_cache', payload = null) {
  try {
    if (!isInlandOverviewViewport(vp || getCanonicalViewport())) {
      debugPanelEvent('inland-water/overview-build-skip', { reason, ...inlandZoomGateDetail(vp || getCanonicalViewport()) });
      return false;
    }
    if (payload && !inlandWaterNeedsBuild(payload)) return false;
    const tier = sceneTierForViewport(vp);
    const geom = inlandWaterGeometryForTier(tier);
    const key = inlandBuildKeyForViewport(vp, geom);
    if (inlandBootBuildQueued.has(key)) {
      console.info('[gfs inland-water] build-cache already queued for viewport', { reason, key, bbox: bboxToQuery(vp), tier, geometry: geom });
      return false;
    }
    inlandBootBuildQueued.add(key);
    const bboxQ = encodeURIComponent(bboxToQuery(vp));
    const sceneQ = sceneQueryParams(vp, vp);
    const cacheBust = `&_=${Date.now()}`;
    const buildUrl = `/gfs/api/inland-water/build-cache?bbox=${bboxQ}&${sceneQ}&geometry=${encodeURIComponent(geom)}&reason=${encodeURIComponent(reason)}${cacheBust}`;
    debugPanelEvent('inland-water/build-cache-start', { reason, bbox: bboxToQuery(vp), tier, geometry: geom, key, url: buildUrl });
    getJsonSafe(buildUrl, null, { timeoutMs: 15000, abortPrevious: false })
      .then((job) => {
        if (job) {
          console.info('[gfs inland-water] background build-cache response', { reason, job });
          debugPanelEvent('inland-water/build-cache-response', { reason, status: job.status, running: job.running, deduped: job.deduped, pid: job.pid, log: job.log, bbox: job.bbox });
          scheduleInlandWaterPostBuildPolls(vp, reason);
        }
      })
      .catch((err) => {
        inlandBootBuildQueued.delete(key);
        console.info('[gfs inland-water] background build-cache skipped', { reason, message: err?.message || String(err) });
        debugPanelEvent('inland-water/build-cache-error', { reason, message: err?.message || String(err), key });
      });
    return true;
  } catch (err) {
    console.info('[gfs inland-water] build-cache queue failed', { reason, message: err?.message || String(err) });
    debugPanelEvent('inland-water/build-cache-error', { reason, message: err?.message || String(err) });
    return false;
  }
}


async function refreshDeferredInlandWater(viewport = null, reason = 'inland_cache_read') {
  const gate = inlandZoomGateDetail(viewport || getCanonicalViewport());
  if (!gate.allowed) {
    debugPanelEvent('inland-water/overview-fetch-skip', { reason, ...gate });
    clearInlandBaitForZoomGate(reason);
    return null;
  }
  if (!gate.baitAllowed) clearInlandBaitForZoomGate(`${reason}_overview_only`);
  const vp = compactViewportForShoreline(viewport || getCanonicalViewport(), reason);
  const bboxQ = encodeURIComponent(bboxToQuery(vp));
  const sceneQ = sceneQueryParams(vp, vp);
  const tier = sceneTierForViewport(vp);
  const geom = inlandWaterGeometryForTier(tier);
  const url = `/gfs/api/inland-water?bbox=${bboxQ}&${sceneQ}&source=auto&geometry=${encodeURIComponent(geom)}&lod=${encodeURIComponent(gate.baitAllowed ? 'auto' : 'overview')}&overview=${gate.baitAllowed ? '0' : '1'}&cache=1&tile_cache=1&parallel=1&auto_build=0&max_tiles=96&reason=${encodeURIComponent(reason)}`;
  debugPanelEvent('inland-water/cache-read', { reason, bbox: bboxToQuery(vp), tier, geometry: geom, url });
  const payload = await getJsonSafe(url, null, { abortPrevious: false, timeoutMs: 12000 });
  if (!payload) return null;
  const mergedCandidate = mergeInlandWaterTemp(payload, dataState.latest?.sceneCache?.layers?.inland_water_temp || dataState.latest?.inlandWaterTemp || null);
  const previous = dataState.latest.inlandWater || null;
  const candidateDrawable = inlandWaterDrawableCount(mergedCandidate);
  const previousDrawable = inlandWaterDrawableCount(previous);
  const merged = (candidateDrawable <= 0 && previousDrawable > 0)
    ? mergeInlandWaterTemp(previous, dataState.latest?.sceneCache?.layers?.inland_water_temp || dataState.latest?.inlandWaterTemp || null)
    : mergedCandidate;
  dataState.latest.inlandWater = merged;
  const frame = dataState.latest.frame || { ok: true, payload_state: 'inland_water_partial', bbox: vp, bbox_used: vp, visible_bbox: vp, weather: {}, meta: {} };
  frame.inlandWater = merged;
  frame.render_reason = 'steady';
  frame.meta = { ...(frame.meta || {}), render_reason: 'steady', inland_refresh_reason: reason, inland_refresh_ts: Date.now(), inland_preserved_last_good: candidateDrawable <= 0 && previousDrawable > 0 };
  dataState.latest.frame = frame;
  try { layerRuntime.engine?.setData?.(frame); } catch (err) { console.warn('[gfs inland-water] layer push failed', err); }
  const drawable = inlandWaterDrawableCount(merged);
  debugPanelEvent('inland-water/cache-read-end', {
    reason,
    polygons: Array.isArray(merged?.polygons) ? merged.polygons.length : 0,
    lines: Array.isArray(merged?.lines) ? merged.lines.length : 0,
    tempLabels: Array.isArray(merged?.tempLabels) ? merged.tempLabels.length : 0,
    drawable,
    source: mergedCandidate?.source || merged?.source || payload?.source || 'unknown',
    preservedLastGood: candidateDrawable <= 0 && previousDrawable > 0,
  });
  if (candidateDrawable <= 0 && inlandWaterNeedsBuild(mergedCandidate)) {
    queueInlandWaterBuild(vp, `${reason}_cache_miss_build`, mergedCandidate);
  }
  return merged;
}

async function mandatoryInlandWaterKick(viewport = null, reason = 'mandatory_boot') {
  if (!isInlandOverviewViewport(viewport || getCanonicalViewport())) {
    debugPanelEvent('inland-water/overview-unavailable-mandatory-skip', { reason, ...inlandZoomGateDetail(viewport || getCanonicalViewport()) });
    clearInlandBaitForZoomGate(reason);
    return null;
  }
  const vp = compactViewportForShoreline(viewport || getCanonicalViewport(), reason);
  forceInlandPillOn(reason);
  console.info('[gfs inland-water] mandatory cache-first kick', { reason, bbox: bboxToQuery(vp), tier: sceneTierForViewport(vp), zoomGate: 'allowed' });

  // Cache-first rule: always try the drawable cache before launching a source
  // builder.  A boot should never wait for ArcGIS/NHD; it should draw last-good
  // json.gz tiles and queue one background build only if still missing.
  let payload = null;
  try {
    payload = await refreshDeferredInlandWater(vp, reason);
    if (inlandWaterDrawableCount(payload || dataState.latest.inlandWater) > 0) {
      console.info('[gfs inland-water] cache-first draw satisfied', { reason, drawable: inlandWaterDrawableCount(payload || dataState.latest.inlandWater) });
      return payload;
    }
  } catch (err) {
    console.info('[gfs inland-water] cache-first read skipped', { reason, message: err?.message || String(err) });
  }

  queueInlandWaterBuild(vp, reason, payload);
  return payload;
}

function scheduleMandatoryInlandBootstrap(viewport = null, reason = 'boot') {
  const baseVp = viewport || getCanonicalViewport();
  if (!isInlandOverviewViewport(baseVp)) {
    debugPanelEvent('inland-water/overview-unavailable-bootstrap-skip', { reason, ...inlandZoomGateDetail(baseVp) });
    clearInlandBaitForZoomGate(reason);
    return;
  }
  forceInlandPillOn(`${reason}_schedule`);
  let stopped = false;
  INLAND_MANDATORY_BOOT_DELAYS_MS.forEach((delayMs, idx) => {
    window.setTimeout(async () => {
      try {
        if (stopped || isLayerUserDisabled('inland-water')) return;
        const currentCount = inlandWaterDrawableCount(dataState.latest.inlandWater);
        if (currentCount > 0 && idx > 1) {
          stopped = true;
          console.info('[gfs inland-water] mandatory bootstrap satisfied', { reason, delayMs, drawable: currentCount });
          return;
        }
        // Keep the same first visible viewport until the first tile renders. Do not
        // chase transient globe bounds / weather fetch bboxes during first-run build.
        const vp = baseVp;
        const payload = await mandatoryInlandWaterKick(vp, `${reason}_mandatory_${idx + 1}_${delayMs}`);
        const nextCount = inlandWaterDrawableCount(payload || dataState.latest.inlandWater);
        if (nextCount > 0) {
          // Keep one more enrichment pass coming from refreshDeferredInlandWater's
          // progressive poll, but stop the mandatory boot watchdog.
          stopped = true;
          console.info('[gfs inland-water] mandatory bootstrap got first drawables', { reason, delayMs, drawable: nextCount });
        }
      } catch (err) {
        console.info('[gfs inland-water] mandatory bootstrap skipped', { reason, delayMs, message: err?.message || String(err) });
        debugPanelEvent('inland-water/bootstrap-error', { reason, delayMs, message: err?.message || String(err) });
      }
    }, delayMs);
  });
}

function activeSceneLayerMask(options = {}) {
  if (layerRuntime.world && typeof layerRuntime.world.activeMask === 'function') {
    return layerRuntime.world.activeMask(options);
  }
  const includeStatic = Boolean(options.includeStatic);
  const openingFast = /boot_open_fast|website_load|initial|opening/i.test(String(options.reason || ''));
  const prefs = layerUserPrefs();
  const engineLayers = layerRuntime.engine?.layers || {};
  const inlandAllowed = isInlandOverviewViewport(options.viewport || getCanonicalViewport());
  const layerActive = (name) => {
    if (prefs[name] === false) return false;
    const layer = engineLayers[name];
    // Before the WorldSubscriptionRenderer is registered, trust user prefs/defaults
    // so boot can read last-good cache immediately. After registration, the world
    // subscription state is authoritative.
    return layer ? Boolean(layer.enabled) : layerDefaultEnabled(name);
  };
  return {
    locations: includeStatic && layerActive('locations'),
    clouds: layerActive('clouds'),
    rain: layerActive('rain'),
    lightning: layerActive('lightning'),
    jetstream: layerActive('jetstream'),
    bait: !openingFast && layerActive('bait'),
    boater: !openingFast && layerActive('boater'),
    inlandWater: includeStatic && inlandAllowed && layerActive('inland-water'),
    inlandWaterTemp: inlandAllowed && layerActive('inland-water'),
  };
}

function sceneLayersFromMask(mask = {}, options = {}) {
  const includeStatic = Boolean(options.includeStatic);
  const includeCompanions = options.includeCompanions !== false;
  const layers = [];
  if (includeStatic && mask.locations) layers.push('locations');
  if (mask.clouds) layers.push('clouds');
  if (mask.rain) layers.push('rain');
  if (mask.lightning) layers.push('lightning');
  if (mask.jetstream) layers.push('jetstream');
  if (mask.bait) layers.push('bait');
  if (mask.boater) layers.push('boater');
  if (includeStatic && mask.inlandWater) layers.push('inland-water');
  if (includeCompanions && mask.inlandWaterTemp) layers.push('inland_water_temp');
  return [...new Set(layers)];
}

function normalizeSceneLayerName(name) {
  return normalizeWorldLayerName(name);
}

function sceneLayersForPill(name) {
  return sceneLayersForWorldPill(name);
}
function scheduleGlobeCacheWarm(viewport, reason = 'website_load') {
  // Warm requests now use the same scene-cache refresh route used by pills and TTL.
  try {
    const vp = viewport || getCanonicalViewport();
    console.info('[gfs cache] scene-cache warm nudge', { reason });
    const layers = layerRuntime.world?.activeSceneLayers?.({ includeStatic: false }) || subscribedSceneLayers({ includeStatic: false });
    nudgeMainSceneCacheRefresh(vp, `cache_warm_replaced_${reason}`, { layers })
      .catch((err) => console.info('[gfs cache] background refresh nudge skipped', { reason, message: err?.message || String(err) }));
  } catch (err) {
    console.info('[gfs cache] warm setup skipped', { message: err?.message || String(err) });
  }
}


function subscribedSceneLayers(options = {}) {
  const vp = options.viewport || getCanonicalViewport();
  let layers;
  if (layerRuntime.world && typeof layerRuntime.world.activeSceneLayers === 'function') {
    layers = layerRuntime.world.activeSceneLayers({ ...options, viewport: vp });
  } else if (Array.isArray(options.layers) && options.layers.length) {
    layers = [...new Set(options.layers.flatMap(sceneLayersForPill).map(normalizeSceneLayerName).filter(Boolean))];
  } else {
    layers = sceneLayersFromMask(activeSceneLayerMask({ ...options, viewport: vp }), { ...options, viewport: vp });
  }
  return filterSceneLayersForInlandZoomGate(layers, vp, options.reason || 'subscribed_scene_layers');
}

function mergeInlandWaterTemp(inlandPayload, tempPayload) {
  if (!inlandPayload || typeof inlandPayload !== 'object') return inlandPayload;
  if (!tempPayload || typeof tempPayload !== 'object') return inlandPayload;
  const points = tempPayload.temperature_points || tempPayload.tempLabels || [];
  const bait = tempPayload.bait || tempPayload.inland_bait || null;
  const baitScore = bait?.bait_score || tempPayload.bait_score || [];
  const baitTargets = bait?.targets || tempPayload.bait_targets || [];
  const hasPoints = Array.isArray(points) && points.length > 0;
  const hasBait = bait && typeof bait === 'object' && ((Array.isArray(baitScore) && baitScore.length > 0) || (Array.isArray(baitTargets) && baitTargets.length > 0));
  if (!hasPoints && !hasBait) return inlandPayload;
  return {
    ...inlandPayload,
    temperature_points: hasPoints ? points : (inlandPayload.temperature_points || inlandPayload.tempLabels || []),
    tempLabels: hasPoints ? points : (inlandPayload.tempLabels || inlandPayload.temperature_points || []),
    temperature_point_count: hasPoints ? points.length : (inlandPayload.temperature_point_count || 0),
    bait: hasBait ? bait : inlandPayload.bait,
    inland_bait: hasBait ? bait : inlandPayload.inland_bait,
    bait_score: hasBait ? baitScore : inlandPayload.bait_score,
    bait_targets: hasBait ? baitTargets : inlandPayload.bait_targets,
    bait_score_count: hasBait ? baitScore.length : (inlandPayload.bait_score_count || 0),
    temp_source: tempPayload.source || inlandPayload.temp_source,
    inland_bait_contract: bait?.contract || inlandPayload.inland_bait_contract,
  };
}


function annotateSceneLayerPayload(layerName, layerPayload, scenePayload) {
  if (!layerPayload || typeof layerPayload !== 'object') return layerPayload;
  const meta = scenePayload?.cache?.layers?.[layerName] || scenePayload?.cache?.layers?.[normalizeSceneLayerName(layerName)] || null;
  const version = meta?.version || meta?.cache_key || meta?.key || layerPayload.version || layerPayload?.cache?.version || layerPayload.valid_time || scenePayload?.resolved_time || scenePayload?.cache?.version || '';
  try {
    Object.defineProperty(layerPayload, '__gfsLayerName', { value: layerName, configurable: true, enumerable: false });
    Object.defineProperty(layerPayload, '__gfsCacheMeta', { value: meta, configurable: true, enumerable: false });
    Object.defineProperty(layerPayload, '__gfsRenderVersion', { value: String(version || ''), configurable: true, enumerable: false });
  } catch (_) {
    layerPayload.__gfsLayerName = layerName;
    layerPayload.__gfsCacheMeta = meta;
    layerPayload.__gfsRenderVersion = String(version || '');
  }
  return layerPayload;
}

function applySceneCachePayload(payload, reason = 'scene-cache') {
  if (!payload || typeof payload !== 'object') return null;
  const layers = payload.layers || {};
  for (const [layerName, layerPayload] of Object.entries(layers)) annotateSceneLayerPayload(layerName, layerPayload, payload);
  dataState.latest.sceneCache = payload;
  const frame = dataState.latest.frame || {
    ok: true,
    payload_state: 'scene_cache_partial',
    bbox: payload.bbox,
    bbox_used: payload.bbox,
    visible_bbox: payload.visible_bbox,
    weather: {},
    meta: {},
  };
  dataState.latest.frame = frame;
  frame.sceneCache = payload;
  frame.cache = payload.cache || frame.cache || {};
  frame.bbox = payload.bbox || frame.bbox;
  frame.bbox_used = payload.bbox || frame.bbox_used;
  frame.visible_bbox = payload.visible_bbox || frame.visible_bbox;
  frame.render_reason = 'steady';
  frame.meta = {
    ...(frame.meta || {}),
    render_reason: 'steady',
    scene_cache_reason: reason,
    scene_cache_ts: Date.now(),
    scene_cache_refresh_interval_ms: payload.refresh_interval_ms || MAIN_SCENE_CACHE_REFRESH_MS,
  };

  if (layers.locations) {
    // Compatibility only. Normal operation loads locations once at boot and does
    // not reload them through scene-cache or the 2-minute TTL loop.
    dataState.latest.locations = layers.locations;
    const locItems = Array.isArray(layers.locations.locations) ? layers.locations.locations : (Array.isArray(layers.locations.items) ? layers.locations.items : null);
    if (locItems && layerUserPrefs().locations !== false && !latestLocations.length) renderLocationsSubscription(locItems);
  }
  if (layers.clouds) {
    markLayerFirstPaint('clouds', layers.clouds);
    dataState.latest.clouds = layers.clouds;
    frame.clouds = layers.clouds;
    frame.weather = frame.weather || {};
    frame.weather.fields = frame.weather.fields || layers.clouds.fields || {};
  }
  if (layers.rain) {
    markLayerFirstPaint('rain', layers.rain);
    dataState.latest.rain = layers.rain;
    frame.rain = layers.rain;
    frame.weather = frame.weather || {};
    frame.weather.precip_columns = layers.rain.precip_columns || layers.rain.features || frame.weather.precip_columns || [];
  }
  if (layers.lightning) {
    markLayerFirstPaint('lightning', layers.lightning);
    dataState.latest.lightning = layers.lightning;
    frame.lightning = layers.lightning;
  }
  if (layers.jetstream) {
    markLayerFirstPaint('jetstream', layers.jetstream);
    dataState.latest.jetstream = layers.jetstream;
    frame.jetstream = layers.jetstream;
    try {
      const jetItems = Array.isArray(layers.jetstream.jet_orbs) ? layers.jetstream.jet_orbs : (Array.isArray(layers.jetstream.items) ? layers.jetstream.items : []);
      if (jetItems.length) {
        window.__gfsGlobalJetWind = {
          ...(window.__gfsGlobalJetWind || {}),
          vectors: jetItems,
          windU: null,
          windV: null,
          bbox: layers.jetstream.bbox || payload.visible_bbox || payload.bbox,
          validTime: layers.jetstream.valid_time || layers.jetstream.resolved_time || null,
          source: layers.jetstream.source || 'gfs_uv_direction_scene_cache',
          quality: layers.jetstream.jetstream || { ok: true, source: layers.jetstream.source || 'gfs_uv_direction_scene_cache', count: jetItems.length, fallback_used: false, mock: false, proxy: false },
          error: null,
          fetchedAt: Date.now(),
        };
        window.__gfsGlobalJetWindField = { fetchedAt: Date.now(), ready: true, source: window.__gfsGlobalJetWind.source, quality: window.__gfsGlobalJetWind.quality };
      }
    } catch (_) {}
  }
  if (layers.boater || layers.boats) {
    const boaterNext = layers.boater || layers.boats;
    markLayerFirstPaint('boater', boaterNext);
    dataState.latest.boats = preferRenderableBoater(boaterNext, dataState.latest.boats || null);
    frame.boats = dataState.latest.boats;
  }
  if (layers.bait) {
    markLayerFirstPaint('bait', layers.bait);
    dataState.latest.baitAdvanced = preferRenderableBaitAdvanced(layers.bait, dataState.latest.baitAdvanced || null);
    frame.baitAdvanced = dataState.latest.baitAdvanced;
    frame.baitBase = frame.baitBase || dataState.latest.baitAdvanced;
  }
  if (layers['shark-intel'] || layers.shark_intel || layers.sharkIntel) {
    const shark = layers['shark-intel'] || layers.shark_intel || layers.sharkIntel;
    markLayerFirstPaint('shark-intel', shark);
    dataState.latest.sharkIntel = shark;
    frame.sharkIntel = shark;
  }
  if (layers['inland-water'] || layers.inland_waterways || layers.inland_water) {
    const inland = layers['inland-water'] || layers.inland_waterways || layers.inland_water;
    markLayerFirstPaint('inland-water', inland);
    const mergedCandidate = mergeInlandWaterTemp(inland, layers.inland_water_temp || layers.inlandTemp);
    const previous = dataState.latest.inlandWater || frame.inlandWater || null;
    if (inlandWaterDrawableCount(mergedCandidate) > 0 || inlandWaterDrawableCount(previous) <= 0) {
      dataState.latest.inlandWater = mergedCandidate;
    } else {
      dataState.latest.inlandWater = mergeInlandWaterTemp(previous, layers.inland_water_temp || layers.inlandTemp);
      try { debugPanelEvent('inland-water/preserve-scene-cache-empty', { reason, source: inland?.source || inland?.status || 'scene-cache', policy: 'keep_last_good_geometry_on_static_cache_miss' }); } catch (_) {}
    }
    frame.inlandWater = dataState.latest.inlandWater;
  } else if (layers.inland_water_temp || layers.inlandTemp) {
    // Live temp enrichment is a companion sublayer. It must not clear/rebuild
    // semi-static shoreline geometry; it only merges labels into the latest draw.
    dataState.latest.inlandWater = mergeInlandWaterTemp(dataState.latest.inlandWater || frame.inlandWater || {}, layers.inland_water_temp || layers.inlandTemp);
    frame.inlandWater = dataState.latest.inlandWater;
  }
  try { layerRuntime.engine?.setData?.(frame); } catch (err) { console.warn('[gfs scene-cache] layer push failed', err); }
  try {
    debugPanelEvent('scene-cache/apply', {
      reason,
      layers: Object.fromEntries(Object.entries(layers).map(([k, v]) => [k, summarizePayloadForDebug(v)])),
      cache: payload.cache,
      refreshIntervalMs: payload.refresh_interval_ms || MAIN_SCENE_CACHE_REFRESH_MS,
    });
  } catch (_) {}
  return frame;
}


function quantizedRefreshKey(viewport, layers) {
  const v = viewport || getCanonicalViewport();
  const q = (x, step = 0.5) => Math.round(Number(x || 0) / step) * step;
  const bbox = [q(v?.west), q(v?.south), q(v?.east), q(v?.north)].map((x) => Number(x).toFixed(2)).join(',');
  return `${bbox}|${(layers || []).slice().sort().join(',')}`;
}

function isMovementRefreshReason(reason = '') {
  const r = String(reason || '').toLowerCase();
  return r.includes('camera_move') || r.includes('cache_pop') || r.includes('mousemove') || r.includes('viewport');
}

function latestSceneCacheLayerPayload(layer) {
  const l = normalizeSceneLayerName(layer);
  const sceneLayers = dataState.latest?.sceneCache?.layers || dataState.latest?.sceneCacheLayers || null;
  if (sceneLayers && typeof sceneLayers === 'object') {
    return sceneLayers[l] || sceneLayers[l?.replace?.('-', '_')] || sceneLayers[l?.replace?.('_', '-')];
  }
  if (l === 'bait') return dataState.latest?.baitAdvanced;
  if (l === 'boater') return dataState.latest?.boats;
  if (l === 'shark-intel') return dataState.latest?.sharkIntel;
  return null;
}

function hasEmptyPlaceholderLayerForRefresh(layers = []) {
  return (layers || []).some((layer) => {
    const l = normalizeSceneLayerName(layer);
    const p = latestSceneCacheLayerPayload(l);
    if (!p || typeof p !== 'object') return true;
    if (p?.cache?.placeholder === true || p?.cache?.hit === false && String(p?.status || p?.payload_state || '').toLowerCase().includes('warming')) return true;
    if (l === 'bait' || l === 'boater') return sceneCacheLayerEmpty(p, l);
    if (l === 'shark-intel') return (Array.isArray(p.contours) ? p.contours.length : 0) <= 0 && (Array.isArray(p.polygons) ? p.polygons.length : 0) <= 0;
    return false;
  });
}

function layerRefreshTtlMs(layers = []) {
  const vals = (layers || []).map((layer) => {
    const l = normalizeSceneLayerName(layer);
    return Number(LAYER_REFRESH_TTL_MS[l] || 0);
  }).filter((v) => Number.isFinite(v) && v > 0);
  return vals.length ? Math.max(...vals) : LIVE_REFRESH_MIN_GAP_MS;
}

function shouldNudgeLiveRefresh(viewport, layers, reason = '') {
  const key = quantizedRefreshKey(viewport, layers);
  const now = Date.now();
  const last = Number(liveRefreshLastByKey.get(key) || 0);
  const layerGap = layerRefreshTtlMs(layers);
  const baseGap = isMovementRefreshReason(reason) ? LIVE_REFRESH_MIN_GAP_MS * 1.5 : LIVE_REFRESH_MIN_GAP_MS;
  const gap = Math.max(baseGap, layerGap);
  const placeholderBypass = hasEmptyPlaceholderLayerForRefresh(layers) || /empty_placeholder|cache_miss|first_boot|mandatory/i.test(String(reason || ''));
  if (last && (now - last) < gap && !placeholderBypass) {
    debugPanelEvent('cache-refresh/throttled', { reason, key, ageMs: now - last, minGapMs: gap, layerTtlMs: layerGap, policy: 'viewport_tile_ttl_dedupe_keep_existing_render' });
    return false;
  }
  if (last && (now - last) < gap && placeholderBypass) {
    debugPanelEvent('cache-refresh/throttle-bypass-empty', { reason, key, ageMs: now - last, minGapMs: gap, layerTtlMs: layerGap, policy: 'empty_placeholder_may_queue_first_real_warm' });
  }
  liveRefreshLastByKey.set(key, now);
  return true;
}

function isOceanIntelLayer(layer) {
  const l = normalizeSceneLayerName(layer);
  return ['bait', 'shark-intel', 'boater'].includes(l);
}
async function refreshMainSceneCache(viewport = getCanonicalViewport(), reason = 'browser_2min_subscription_refresh', options = {}) {
  const visibleViewport = viewport || getCanonicalViewport();
  const layers = subscribedSceneLayers(options);
  if (!layers.length) {
    debugPanelEvent('scene-cache/skip', { reason, policy: 'no_live_layers_subscribed_static_layers_local_only' });
    return null;
  }
  const contract = buildLayerBboxContract(visibleViewport, `scene_cache_${reason}`, layers);
  const fetchViewport = contract.scene_read_bbox || visibleViewport;
  const mode = options.mode || 'read';
  const refreshFlag = options.refresh === true ? 1 : 0;
  const fastFlag = options.fast === true || mode === 'fast' || mode === 'first_paint' || mode === 'cache_only';
  const requestVisibleViewport = (layerSetHasAny(layers, ['clouds']) && !layerSetHasAny(layers, ['bait', 'boater', 'shark-intel', 'inland-water']))
    ? fetchViewport
    : (contract.visible_bbox || visibleViewport);
  debugPanelEvent('scene-cache/request', { reason, mode, refresh: refreshFlag, fast: fastFlag, bbox: bboxToQuery(fetchViewport), visible_bbox: bboxToQuery(requestVisibleViewport), bbox_contract: contract, layers, intervalMs: MAIN_SCENE_CACHE_REFRESH_MS });
  markSceneProgressRequest(reason);
  try {
    const payload = await getSceneFrame({
      bbox: bboxToQuery(fetchViewport),
      visibleBbox: bboxToQuery(requestVisibleViewport),
      layers,
      mode,
      refresh: Boolean(refreshFlag),
      providerJobs: true,
      reason,
    }, null, { timeoutMs: fastFlag ? 2500 : 9000 });
    if (!payload) {
      setProgressBar('scene', 100, 'cache empty', 'stale');
      return null;
    }
    applySceneCachePayload(payload, reason);
    try { scheduleSceneCachePlaceholderRepair(payload?.layers || {}, viewport, `${reason}_empty_placeholder_repair`); } catch (_) {}
    markSceneProgressPayload(payload, reason);
    return payload;
  } catch (err) {
    markSceneProgressError(err, reason);
    throw err;
  }
}

async function nudgeMainSceneCacheRefresh(viewport = getCanonicalViewport(), reason = 'browser_2min_subscription_refresh', options = {}) {
  const requestedLayers = subscribedSceneLayers(options);
  if (!requestedLayers.length) {
    setProgressBar('layer', 100, 'static only', '');
    debugPanelEvent('cache-refresh/skip', { reason, policy: 'no_live_layers_subscribed_static_layers_local_only' });
    return null;
  }

  const contract = buildLayerBboxContract(viewport, `scene_frame_nudge_${reason}`, requestedLayers);
  const fetchViewport = contract.scene_read_bbox || contract.visible_bbox || viewport;
  if (!shouldNudgeLiveRefresh(fetchViewport, requestedLayers, reason)) {
    return { ok: true, status: 'throttled', reason, policy: 'client_live_refresh_min_gap', min_gap_ms: LIVE_REFRESH_MIN_GAP_MS };
  }

  setProgressBar('layer', 42, `warming ${requestedLayers.length} live layers`, 'busy');
  const requestVisibleViewport = (layerSetHasAny(requestedLayers, ['clouds']) && !layerSetHasAny(requestedLayers, ['bait', 'boater', 'shark-intel', 'inland-water']))
    ? fetchViewport
    : (contract.visible_bbox || viewport);
  debugPanelEvent('scene-frame/refresh-request', {
    reason,
    bbox: bboxToQuery(fetchViewport),
    visible: bboxToQuery(requestVisibleViewport),
    layers: requestedLayers,
    provider_grid: '24x24_per_selected_provider',
    contract,
  });

  const payload = await getSceneFrame({
    bbox: bboxToQuery(fetchViewport),
    visibleBbox: bboxToQuery(requestVisibleViewport),
    layers: requestedLayers,
    mode: 'refresh',
    refresh: true,
    providerJobs: true,
    reason,
  }, null, { timeoutMs: 9000 });

  const refreshJobs = payload?.refresh?.jobs && typeof payload.refresh.jobs === 'object' ? Object.values(payload.refresh.jobs) : [];
  const providerJobs = Number(payload?.provider_tiles?.job_count || 0);
  const scheduled = refreshJobs.filter((v) => Boolean(v?.scheduled)).length;
  setProgressBar('layer', scheduled ? 58 : 76, scheduled ? `${scheduled}/${refreshJobs.length} queued` : `${providerJobs} provider tiles planned`, scheduled ? 'busy' : '');
  return { ok: true, status: 'background', reason, frame: payload, provider_job_count: providerJobs };
}
async function refreshData(reason = 'manual') {
  const viewport = getCanonicalViewport();
  const signature = bboxSignature(viewport);

  if (dataState.lastSignature === signature && reason !== 'boot' && reason !== 'steady' && reason !== 'force') {
    if (GFS_DEBUG) console.debug('[gfs data] skipped refresh; signature unchanged', { reason, signature });
    return dataState.latest;
  }

  if (dataState.inFlight && reason !== 'manual' && reason !== 'force') {
    if (GFS_DEBUG) console.debug('[gfs data] refresh already in flight; keeping prior request alive', { reason, signature });
    return dataState.latest;
  }

  if (dataState.inFlight && dataState.activeAbort) {
    try { dataState.activeAbort.abort(); } catch (_) {}
  }

  const controller = new AbortController();
  const seq = dataState.requestSeq + 1;
  dataState.requestSeq = seq;
  dataState.inFlight = true;
  dataState.activeAbort = controller;
  dataState.latest.bbox = viewport;
  dataState.lastSignature = signature;

  try {
    const includeStatic = /boot|initial|manual|force/i.test(String(reason || ''));
    await refreshMainSceneCache(viewport, `refreshData_${reason}_scene_cache`, { mode: 'fast', fast: true, refresh: false, includeStatic });
    if (/boot|steady|manual|force/i.test(String(reason || ''))) {
      nudgeMainSceneCacheRefresh(viewport, `refreshData_${reason}_background_refresh`, { includeStatic }).catch(() => {});
    }
    if (isLayerOn('inland-water') && isInlandOverviewViewport(viewport)) {
      refreshDeferredInlandWater(viewport, `refreshData_${reason}_inland_fast`).catch(() => {});
    } else if (isLayerOn('inland-water')) {
      clearInlandBaitForZoomGate(`refreshData_${reason}_overview_unavailable`);
    }
    if (isLayerOn('jetstream')) {
      ensureJetstreamVisual(`refreshData_${reason}`).catch(() => {});
    }
    logMissionControlState(reason);
    return dataState.latest;
  } catch (err) {
    console.info('[gfs data] scene-cache refresh skipped', { reason, message: err?.message || String(err) });
    return dataState.latest;
  } finally {
    if (dataState.activeAbort === controller) dataState.activeAbort = null;
    if (seq === dataState.requestSeq) dataState.inFlight = false;
  }
}


let visualPipelineSeq = 0;
let visualPipelineInFlight = false;
let visualPipelineQueued = null;
let lastVisualPipelineStartAt = 0;
let jetstreamVisualReady = false;
let lastJetstreamEnsureAt = 0;

function sceneCacheLayerEmpty(layerPayload, layerName = '') {
  if (!layerPayload || typeof layerPayload !== 'object') return true;
  const status = String(layerPayload.status || layerPayload.payload_state || '').toLowerCase();
  const source = String(layerPayload.source || '').toLowerCase();
  if (layerName === 'bait') {
    const bait = layerPayload.bait || {};
    const count = (Array.isArray(bait.polygons) ? bait.polygons.length : 0)
      + (Array.isArray(layerPayload.bait_score) ? layerPayload.bait_score.length : 0)
      + (Array.isArray(layerPayload.ocean_points) ? layerPayload.ocean_points.length : 0)
      + (Array.isArray(layerPayload.oceanPoints) ? layerPayload.oceanPoints.length : 0);
    return count <= 0 && (status.includes('warming') || source.includes('deferred') || source.includes('cache'));
  }
  if (layerName === 'boater') {
    const count = (Array.isArray(layerPayload.boats) ? layerPayload.boats.length : 0)
      + (Array.isArray(layerPayload.points) ? layerPayload.points.length : 0)
      + (Array.isArray(layerPayload.ocean_points) ? layerPayload.ocean_points.length : 0)
      + (Array.isArray(layerPayload.oceanPoints) ? layerPayload.oceanPoints.length : 0);
    return count <= 0 && (status.includes('warming') || source.includes('deferred') || source.includes('cache'));
  }
  return false;
}

function scheduleSceneCachePlaceholderRepair(layers, viewport, reason = 'empty_placeholder_repair') {
  const wanted = [];
  if (layers?.bait && sceneCacheLayerEmpty(layers.bait, 'bait')) wanted.push('bait');
  if (!isLayerUserDisabled('boater') && layers?.boater && sceneCacheLayerEmpty(layers.boater, 'boater')) wanted.push('boater');
  if (!wanted.length) return;
  const now = Date.now();
  const repairKey = wanted.join(',');
  window.__gfsEmptyLayerRepair = window.__gfsEmptyLayerRepair || {};
  if (window.__gfsEmptyLayerRepair[repairKey] && (now - window.__gfsEmptyLayerRepair[repairKey]) < 45000) {
    debugPanelEvent('scene-cache/empty-repair-throttled', { layers: wanted, reason });
    return;
  }
  window.__gfsEmptyLayerRepair[repairKey] = now;
  const box = bboxString(viewport || getCanonicalViewport(), 4);
  const visible = bboxString(getCanonicalViewport(), 4);
  const url = `/gfs/api/cache/refresh?bbox=${encodeURIComponent(box)}&visible_bbox=${encodeURIComponent(visible)}&scene_tier=${encodeURIComponent(sceneTierForViewport(viewport || getCanonicalViewport()))}&layers=${encodeURIComponent(wanted.join(','))}&force=1&reason=${encodeURIComponent(reason)}`;
  debugPanelEvent('scene-cache/empty-repair-start', { layers: wanted, url });
  getJsonSafe(url, null, { abortPrevious: false, timeoutMs: 8000 })
    .then((payload) => debugPanelEvent('scene-cache/empty-repair-end', { layers: wanted, jobs: payload?.jobs || null }))
    .catch((err) => console.info('[gfs scene-cache] empty layer repair skipped', { layers: wanted, message: err?.message || String(err) }));
}


async function ensureJetstreamVisual(reason = 'steady') {
  try {
    if (!isLayerOn('jetstream')) {
      if (GFS_DEBUG) console.debug('[gfs visual] jetstream held disabled', { reason });
      return;
    }
    if (window.__gfsJetstreamDisabled === true || layerUserPrefs().jetstream === false || window.__gfsJetstreamPillOn === false) {
      if (GFS_DEBUG) console.debug('[gfs visual] jetstream hard-disabled by pill', { reason });
      return;
    }
    const now = Date.now();
    if (jetstreamVisualReady && window.__gfsLayerState?.jetstreamBalloons && window.__gfsJetstreamDisabled !== true && (now - lastJetstreamEnsureAt) < 120000) {
      if (GFS_DEBUG) console.debug('[gfs visual] jetstream already ready', { reason });
      return;
    }
    lastJetstreamEnsureAt = now;
    window.__gfsLayerState = { ...(window.__gfsLayerState || {}), jetstreamBalloons: true };
    window.__gfsJetstreamDisabled = false;
    window.__gfsJetstreamPillOn = true;
    if (typeof window.setJetBalloonsEnabled === 'function') {
      await window.setJetBalloonsEnabled(true);
    }
    jetstreamVisualReady = true;
    console.info('[gfs visual] jetstream ready', { reason });
  } catch (err) {
    console.info('[gfs visual] jetstream enable skipped', { message: err?.message || String(err) });
  }
}

async function refreshVisualPipeline(viewport, reason = 'steady') {
  const seq = ++visualPipelineSeq;
  if (visualPipelineInFlight) {
    visualPipelineQueued = { viewport, reason };
    console.info('[gfs visual] refresh coalesced behind active world subscription pipeline', { reason });
    return null;
  }
  visualPipelineInFlight = true;
  lastVisualPipelineStartAt = Date.now();
  try {
    const vpForGlobals = viewport || dataState.latest?.bbox || getCanonicalViewport();
    publishViewportGlobals(vpForGlobals, reason);
  } catch (_) {}
  enforceInlandZoomGate(vpForGlobals, reason);
  sendViewportPriority(reason);
  console.info('[gfs visual] world subscription refresh start', {
    reason,
    active: layerRuntime.world?.enabledMap?.() || activeSceneLayerMask({ includeStatic: true }),
  });
  try {
    const payload = layerRuntime.world
      ? await layerRuntime.world.refreshViewport(viewport || getCanonicalViewport(), reason, { includeStatic: isLayerOn('inland-water') && isInlandOverviewViewport(viewport || getCanonicalViewport()), viewport: viewport || getCanonicalViewport() })
      : await refreshMainSceneCache(viewport, `${reason}_scene_cache_fast`, { mode: 'fast', fast: true, refresh: false, includeStatic: isLayerOn('inland-water') && isInlandOverviewViewport(viewport || getCanonicalViewport()) });
    if (seq !== visualPipelineSeq) return null;
    console.info('[gfs visual] world subscription refresh complete', { reason, seq, legacyDirectRoutes: false });
    return payload || true;
  } finally {
    visualPipelineInFlight = false;
    const queued = visualPipelineQueued;
    visualPipelineQueued = null;
    if (queued && bboxSignature(queued.viewport || getCanonicalViewport()) !== bboxSignature(viewport)) {
      window.setTimeout(() => refreshVisualPipeline(queued.viewport, `${queued.reason}_coalesced`).catch(() => {}), 1200);
    }
  }
}

function installSteadyRefresh() {
  let dirty = true;
  let settleTimer = null;
  let lastRefreshAt = 0;

  const runSettledRefresh = (reason = 'camera_settled') => {
    if (!dirty) return;
    dirty = false;
    lastRefreshAt = Date.now();
    const vp = getCanonicalViewport();
    sendViewportPriority(reason);
    scheduleGlobeCacheWarm(vp, `viewport_${reason}`);
    scheduleCachePopRefreshes(vp, `viewport_${reason}`);
    // Camera settles use one scene-cache visual pass: fast read first, one
    // background refresh nudge second. No direct layer repair endpoints here.
    refreshVisualPipeline(vp, reason).catch(() => {});
  };

  const scheduleSettledRefresh = (reason = 'camera_settled') => {
    dirty = true;
    if (settleTimer) clearTimeout(settleTimer);
    // Some Maps 3D builds do not emit gmp-steadystate reliably during orbit.
    // This timer keeps boats/clouds/bait tied to the visible camera after pan,
    // orbit, tilt, heading, and range changes.
    settleTimer = setTimeout(() => runSettledRefresh(reason), CAMERA_STEADY_DEBOUNCE_MS);
  };

  const onMove = () => scheduleSettledRefresh('camera_move');

  const onSteady = (ev) => {
    const isSteady = ev?.isSteady;
    if (typeof isSteady === 'boolean' && !isSteady) return;
    if (settleTimer) { clearTimeout(settleTimer); settleTimer = null; }
    runSettledRefresh('steady');
  };

  const events = ['gmp-centerchange', 'gmp-headingchange', 'gmp-rangechange', 'gmp-rollchange', 'gmp-tiltchange', 'gmp-camerapositionchange', 'centerchange', 'headingchange', 'rangechange', 'rollchange', 'tiltchange'];
  events.forEach((evt) => globeEl.addEventListener(evt, onMove));
  globeEl.addEventListener('gmp-steadystate', onSteady);
  globeEl.addEventListener('gmp-steadychange', onSteady);

  // First camera properties can land after the element upgrades; force a second
  // settled pass shortly after boot so the initial angled view and range are used.
  setTimeout(() => { if (Date.now() - lastRefreshAt > 500) scheduleSettledRefresh('initial_camera_sync'); }, 900);

  return () => {
    if (settleTimer) clearTimeout(settleTimer);
    events.forEach((evt) => globeEl.removeEventListener(evt, onMove));
    globeEl.removeEventListener('gmp-steadystate', onSteady);
    globeEl.removeEventListener('gmp-steadychange', onSteady);
  };
}

function createGfsSocket() {
  let ws = null;
  let reconnectTimer = null;
  let pingTimer = null;
  let watchdogTimer = null;
  let backoffMs = 1000;
  let manualClose = false;
  let connecting = false;
  let lastMessageAt = 0;

  const clearTimers = () => {
    if (reconnectTimer) clearTimeout(reconnectTimer);
    if (pingTimer) clearInterval(pingTimer);
    if (watchdogTimer) clearInterval(watchdogTimer);
    reconnectTimer = null;
    pingTimer = null;
    watchdogTimer = null;
  };

  const scheduleReconnect = () => {
    if (manualClose || reconnectTimer) return;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, backoffMs);
    backoffMs = Math.min(15000, backoffMs * 2);
  };

  const startHeartbeat = () => {
    lastMessageAt = Date.now();
    pingTimer = setInterval(() => {
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      try { ws.send(JSON.stringify({ type: 'ping' })); } catch (_) {}
    }, 20000);

    watchdogTimer = setInterval(() => {
      const stale = Date.now() - lastMessageAt > 30000;
      if (stale && ws && ws.readyState === WebSocket.OPEN) {
        try { ws.close(4000, 'inactivity timeout'); } catch (_) {}
      }
    }, 5000);
  };

  const handleMessage = (msg) => {
    if (!msg || typeof msg !== 'object') return;
    if (msg.type === 'viewport_ack') {
      if (GFS_DEBUG) console.debug('[gfs/ws] viewport prioritized', msg);
      return;
    }
    if (msg.type === 'tile_update') {
      console.info('[gfs/ws] tile update', { pill: msg.pill, tile_key: msg.tile_key, status: msg.status, features: Array.isArray(msg.features) ? msg.features.length : 0 });
      return;
    }
    if (msg.type === 'snapshot_changed' && msg.detail?.location_id) {
      if (selectedLocation && msg.detail.location_id === selectedLocation.id) {
        if (msg.detail.active) showLiveOverlay();
        else hideLiveOverlay();
      }
    }
  };

  const connect = () => {
    if (manualClose || connecting || (ws && ws.readyState === WebSocket.OPEN)) return;
    connecting = true;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsPath = backoffMs > 4000 ? '/gfs/ws' : '/ws/gfs';
    ws = new WebSocket(`${proto}//${location.host}${wsPath}`);

    ws.onopen = () => {
      connecting = false;
      backoffMs = 1000;
      clearTimers();
      startHeartbeat();
      console.info('[gfs/ws] connected');
      try { sendViewportPriority('ws_open'); } catch (_) {}
    };
    ws.onmessage = (ev) => {
      lastMessageAt = Date.now();
      try { handleMessage(JSON.parse(ev.data)); } catch (_) {}
    };
    ws.onerror = (ev) => console.warn('[gfs/ws] socket error', { readyState: ws?.readyState, url: ws?.url || null });
    ws.onclose = () => {
      connecting = false;
      clearTimers();
      if (!manualClose) scheduleReconnect();
    };
  };

  const close = () => {
    manualClose = true;
    clearTimers();
    if (ws) {
      try { ws.close(1000, 'page unload'); } catch (_) {}
      ws = null;
    }
  };

  const sendViewport = (viewport, reason = 'steady') => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    try {
      ws.send(JSON.stringify({
        type: 'viewport',
        bbox: sanitizeViewportForQuery(viewport || getCanonicalViewport()),
        zoom: null,
        reason,
        pills: ['bait', 'boats', 'clouds', 'rain', 'currents', 'inland-water', 'lightning'],
      }));
      return true;
    } catch (_) { return false; }
  };

  return { connect, close, sendViewport };
}

const gfsSocket = createGfsSocket();

function sampleGrid(grid, bbox, lat, lon) {
  if (!bbox || !Array.isArray(grid) || !Array.isArray(grid[0])) return NaN;
  const arr = Array.isArray(grid[0][0]) ? grid[0] : grid;
  const ny = arr.length;
  const nx = Array.isArray(arr[0]) ? arr[0].length : 0;
  if (!ny || !nx) return NaN;
  const yi = Math.max(0, Math.min(ny - 1, Math.floor(((lat - bbox.south) / (bbox.north - bbox.south || 1)) * ny)));
  const xi = Math.max(0, Math.min(nx - 1, Math.floor(((lon - bbox.west) / (bbox.east - bbox.west || 1)) * nx)));
  return Number(arr[yi]?.[xi]);
}

function nearestOverlaySummary(loc) {
  const bbox = dataState.latest.bbox;
  const weather = dataState.latest.weather;
  const clouds = dataState.latest.clouds;
  const bait = dataState.latest.baitAdvanced || dataState.latest.baitBase;
  const lat = Number(loc?.lat);
  const lon = Number(loc?.lon);
  if (!bbox || !Number.isFinite(lat) || !Number.isFinite(lon)) return null;

  return {
    validTime: weather?.valid_time || clouds?.valid_time || bait?.valid_time || null,
    cloudCover: sampleGrid(weather?.fields?.cloud_total, bbox, lat, lon),
    rainRate: sampleGrid(weather?.fields?.precip_rate, bbox, lat, lon),
    lowCloud: sampleGrid(clouds?.cloud_layers?.find((layer) => layer?.name === 'low')?.density, bbox, lat, lon),
    baitOverall: Number(bait?.confidence?.overall ?? NaN),
  };
}

function sampleWeatherAt(lat, lon) {
  const bbox = dataState.latest.bbox;
  const weather = dataState.latest.weather;
  if (!bbox || !weather) return null;
  const windU = sampleGrid(weather?.fields?.wind_u, bbox, lat, lon);
  const windV = sampleGrid(weather?.fields?.wind_v, bbox, lat, lon);
  const tempK = sampleGrid(weather?.fields?.air_temp, bbox, lat, lon);
  const pressurePa = sampleGrid(weather?.fields?.pressure_msl, bbox, lat, lon);
  return {
    temperature_k: Number.isFinite(tempK) ? tempK : NaN,
    temperature_c: Number.isFinite(tempK) ? (tempK - 273.15) : NaN,
    temperature_f: Number.isFinite(tempK) ? (((tempK - 273.15) * 9) / 5) + 32 : NaN,
    pressure_pa: Number.isFinite(pressurePa) ? pressurePa : NaN,
    pressure_hpa: Number.isFinite(pressurePa) ? (pressurePa / 100) : NaN,
    wind_speed_mps: Number.isFinite(windU) && Number.isFinite(windV) ? Math.hypot(windU, windV) : NaN,
  };
}

function nearestBaitAt(lat, lon) {
  const bait = dataState.latest.baitAdvanced || dataState.latest.baitBase;
  const pts = Array.isArray(bait?.bait_score) ? bait.bait_score : [];
  if (!pts.length || !Number.isFinite(lat) || !Number.isFinite(lon)) return null;
  let best = null;
  let bestD2 = Infinity;
  for (const item of pts) {
    const plat = Number(item?.lat);
    const plon = Number(item?.lon);
    if (!Number.isFinite(plat) || !Number.isFinite(plon)) continue;
    const d2 = ((plat - lat) * (plat - lat)) + ((plon - lon) * (plon - lon));
    if (d2 < bestD2) {
      bestD2 = d2;
      best = item;
    }
  }
  return best;
}

const hud = createHud({
  root: document.getElementById('locationHud'),
  getOverlaySummary: nearestOverlaySummary,
  onSelectLocation: (loc) => {
    selectedLocation = loc;
    if (loc?.id) missingLiveLocationIds.delete(loc.id);
    gfsSocket.connect();
    refreshSelectedLiveState();
  },
  onStartLive: async (loc) => {
    activeLocation = loc;
    selectedLocation = loc;
    liveManuallyDismissed = false;
    showLiveOverlay();
    const refs = currentLiveOverlayRefs();
    await startLive({ locationId: loc.id, videoEl: refs.videoEl, overlayEl: refs.overlayEl });
    showStatus(`Live started: ${loc.name}`);
  },
  onStopLive: async (loc) => {
    await stopLive({
      locationId: loc.id,
      onBlob: async (blob) => {
        const f = new File([blob], `live-${Date.now()}.webm`, { type: 'video/webm' });
        await uploadSafe(`/gfs/api/location/${encodeURIComponent(loc.id)}/upload`, f, {}, null);
      },
    });
    hideLiveOverlay();
    showStatus(`Live stopped: ${loc.name}`);
    await hud.open(loc);
  },
});


const fishAiRuntime = installFishAI({
  globeEl,
  hud,
  getLocations: () => latestLocations,
  getDataState: () => dataState.latest,
  showStatus,
  debugPanelEvent,
  refreshMainSceneCache,
  nudgeMainSceneCacheRefresh,
  getCanonicalViewport,
  setLayerEnabled: (layer, enabled, opts = {}) => layerRuntime.world?.setLayerEnabled?.(layer, enabled, { reason: opts.reason || 'fishai_command', subscribe: true }),
});
if (fishAiRuntime) {
  debugPanelEvent('fishai/ready', { mode: 'titlebar_direction_search', endpoint: '/gfs/api/fishai' });
}

function installHoverHud() {
  const handler = (ev) => {
    const d = ev?.detail || {};
    const lat = Number(d?.latLng?.lat ?? d?.position?.lat ?? d?.lat);
    const lon = Number(d?.latLng?.lng ?? d?.position?.lng ?? d?.lng);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
    const sample = sampleWeatherAt(lat, lon);
    hud.updateHover({ lat, lon }, sample, nearestBaitAt(lat, lon));
    if (typeof window.updateHUD === 'function') {
      window.updateHUD(sample);
    }
  };
  globeEl.addEventListener('gmp-click', handler);
  globeEl.addEventListener('gmp-pointermove', handler);
  return () => {
    globeEl.removeEventListener('gmp-click', handler);
    globeEl.removeEventListener('gmp-pointermove', handler);
  };
}

window.addEventListener('beforeunload', () => {
  stopLivePolling();
  gfsSocket.close();
  if (dataState.activeAbort) {
    try { dataState.activeAbort.abort(); } catch (_) {}
  }
});

async function boot() {
  hideLiveOverlay();
  initCloudShellOpacityControl();
  setEarthFirstPriority('boot');

  const ready = await ensureMaps3D(globeEl, fallbackEl);
  if (!ready.ok) {
    showStatus(`Globe unavailable (${ready.reason})`);
    return;
  }

  const { maps3d } = await libs();
  const payload = await getJsonSafe('/gfs/api/locations', { locations: [], items: [] }, { timeoutMs: 12000, abortPrevious: false });
  const locations = Array.isArray(payload?.locations)
    ? payload.locations
    : (Array.isArray(payload?.items) ? payload.items : []);
  console.info('[gfs boot] locations payload loaded', {
    count: locations.length,
    payloadCount: payload?.count,
    hasLocationsKey: Array.isArray(payload?.locations),
    hasItemsKey: Array.isArray(payload?.items),
    first: locations[0] ? { id: locations[0].id || locations[0].location_key, lat: locations[0].lat, lon: locations[0].lon, name: locations[0].name } : null,
  });
  latestLocations = locations;
  renderLocationsSubscription(locations);
  // Boot order: CSV fish/orb markers first, then layer system enables
  // Jetstream -> Bait -> Boats -> Clouds -> Rain.
  initLayerSystem();
  // Inland/lake detail is zoom-gated. Global/world boot stays light; once the
  // camera reaches regional/coastal/harbor detail, cache-first inland geometry
  // and inland bait/temperature overlays can start.
  window.setTimeout(() => scheduleMandatoryInlandBootstrap(getCanonicalViewport(), 'boot_earth_first_deferred'), EARTH_FIRST_INLAND_DELAY_MS);
  if (!locations.length) {
    console.error('[gfs boot] no CSV fish locations were rendered; check /gfs/api/locations payload shape');
    showStatus('Ready, but 0 fish beacons loaded');
  }

  skyRuntime = startSkySystem({
    getViewport: getCanonicalViewport,
    getCameraAngles: parseCameraAngleSignature,
    getRangeMeters: parseRangeMeters,
    deferInitialMs: 6500,
  });
  const teardownSteady = installSteadyRefresh();
  const teardownHoverHud = installHoverHud();

  showStatus(`Ready • ${locations.length} fish beacons • earth first, inland detail waits for regional zoom`);
  sceneProgressState.nextSceneAt = Date.now() + MAIN_SCENE_CACHE_REFRESH_MS;
  updateSceneProgressTick();
  const teardownSceneProgress = window.setInterval(updateSceneProgressTick, 1000);
  // Do not run the old heavy frame path before first cache paint.
  window.setTimeout(() => refreshData('boot_cache_followup').catch((err) => console.info('[gfs boot] boot follow-up refresh skipped', { message: err?.message || String(err) })), 12000);
  afterEarthPaint(() => refreshMainSceneCache(getCanonicalViewport(), 'boot_scene_cache_fast_earth_first', { mode: 'fast', fast: true, refresh: false, includeStatic: false, reason: 'boot_scene_cache_fast_earth_first' }).catch((err) => console.info('[gfs scene-cache] boot fast skipped', { message: err?.message || String(err) })), EARTH_FIRST_BOOT_CACHE_DELAY_MS);
  window.setTimeout(() => nudgeMainSceneCacheRefresh(getCanonicalViewport(), 'boot_background_refresh_earth_first').catch(() => {}), EARTH_FIRST_WARM_DELAY_MS);
  window.setTimeout(() => scheduleGlobeCacheWarm(getCanonicalViewport(), 'website_load_earth_first'), EARTH_FIRST_WARM_DELAY_MS + 2500);
  window.setTimeout(() => scheduleCachePopRefreshes(getCanonicalViewport(), 'website_load_earth_first'), EARTH_FIRST_WARM_DELAY_MS + 4500);
  window.setInterval(() => {
    const vp = getCanonicalViewport();
    // 2-minute policy is now only a TTL/background heartbeat.  It must not be a
    // visible cache reload, full frame call, or cache-pop train.
    nudgeMainSceneCacheRefresh(vp, 'browser_2min_ttl_heartbeat').catch(() => {});
  }, MAIN_SCENE_CACHE_REFRESH_MS);
  // Extra cache-first pulls pick up the background-warmed frame/tiles without
  // blocking the initial UI. Keep these spaced out so cold GCP VMs do not thrash.
  window.setTimeout(() => refreshVisualPipeline(getCanonicalViewport(), 'post_warm_visual_25000').catch((err) => console.info('[gfs boot] post-warm visual skipped', { message: err?.message || String(err) })), 25000);
  window.setTimeout(() => nudgeMainSceneCacheRefresh(getCanonicalViewport(), 'post_warm_background_65000').catch(() => {}), 65000);
  startLivePolling();

  window.addEventListener('beforeunload', teardownSteady, { once: true });
  window.addEventListener('beforeunload', teardownHoverHud, { once: true });
  window.addEventListener('beforeunload', () => window.clearInterval(teardownSceneProgress), { once: true });
  window.addEventListener('beforeunload', () => skyRuntime?.teardown?.(), { once: true });
  window.addEventListener('beforeunload', () => { try { locationsTeardown?.(); } catch (_) {} }, { once: true });
}

boot().catch((e) => {
  showStatus(`Error: ${e.message}`);
  console.error(e);
});
