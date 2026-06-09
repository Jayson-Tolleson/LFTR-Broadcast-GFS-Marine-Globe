// LFTR World Subscription Renderer
// One browser-side authority for pill -> cache subscription -> frame -> renderer.
// Renderers never fetch provider/debug routes. Pills only enable/disable subscriptions.

export const WORLD_LAYER_CONTRACTS = Object.freeze({
  locations: Object.freeze({
    pill: 'locations',
    cacheLayers: ['locations'],
    static: true,
    renderer: 'local-marker-rerender',
    requestPolicy: 'load-once-local-only',
    clearPolicy: 'clear-only-on-pill-off',
    debugReplacement: '/gfs/api/locations',
  }),
  clouds: Object.freeze({
    pill: 'clouds',
    cacheLayers: ['clouds'],
    renderer: 'cloud-shells-plus-pooled-particles',
    requestPolicy: 'scene-cache-fast-read-then-background-refresh',
    clearPolicy: 'preserve-on-empty; clear-only-on-pill-off',
    debugReplacement: '/gfs/api/cache/refresh?layers=clouds',
    particleCandidate: true,
  }),
  rain: Object.freeze({
    pill: 'rain',
    cacheLayers: ['rain'],
    renderer: 'precip-emitter-particles',
    requestPolicy: 'scene-cache-fast-read-then-background-refresh',
    clearPolicy: 'preserve-on-warming; clear-only-on-pill-off',
    debugReplacement: '/gfs/api/cache/refresh?layers=rain',
    particleCandidate: true,
  }),
  lightning: Object.freeze({
    pill: 'lightning',
    cacheLayers: ['lightning'],
    renderer: 'short-lived-flash-events',
    requestPolicy: 'scene-cache-fast-read-then-background-refresh',
    clearPolicy: 'ttl-fade; clear-only-on-pill-off',
    debugReplacement: '/gfs/api/cache/refresh?layers=lightning',
    particleCandidate: true,
  }),
  jetstream: Object.freeze({
    pill: 'jetstream',
    cacheLayers: ['jetstream'],
    renderer: 'global-balloon-particle-pool',
    requestPolicy: 'scene-cache-fast-read-then-background-refresh',
    clearPolicy: 'pause/clear-pool-only-on-pill-off',
    debugReplacement: '/gfs/api/cache/refresh?layers=jetstream',
    particleCandidate: true,
  }),
  bait: Object.freeze({
    pill: 'bait',
    cacheLayers: ['bait'],
    renderer: 'server-polygon-reconciler',
    requestPolicy: 'scene-cache-fast-read-then-background-refresh',
    clearPolicy: 'preserve-last-good; fade-missing-polygons; clear-only-on-pill-off',
    polygonPolicy: 'server polygons only in normal mode; no client fallback unless explicit debug flag',
    debugReplacement: '/gfs/api/cache/refresh?layers=bait',
  }),
  'shark-intel': Object.freeze({
    pill: 'shark-intel',
    cacheLayers: ['shark-intel'],
    renderer: 'shark-legal-slot-marching-square-boil-contours',
    requestPolicy: 'scene-cache-fast-read-then-background-refresh',
    clearPolicy: 'preserve-last-good; clear-only-on-pill-off',
    polygonPolicy: 'shared bait marching-squares spine; stable contour ids/hashes; no renderer provider fetch',
    targetPolicy: 'leopard 36-42 inch prime slot first; >42 large ring; <36 nursery caution; tiger/sand shark watch overlays',
    debugReplacement: '/gfs/api/cache/refresh?layers=shark-intel',
  }),
  boater: Object.freeze({
    pill: 'boater',
    cacheLayers: ['boater'],
    renderer: 'current-polygon-reconciler-plus-boat-model-pool',
    requestPolicy: 'scene-cache-fast-read-then-background-refresh',
    clearPolicy: 'preserve-last-good; clear-only-on-pill-off',
    polygonPolicy: 'official boater/current polygons first; derived point contours only inside renderer from frame data',
    debugReplacement: '/gfs/api/cache/refresh?layers=boater',
  }),
  'inland-water': Object.freeze({
    pill: 'inland-water',
    cacheLayers: ['inland-water', 'inland_water_temp'],
    staticCacheLayers: ['inland-water'],
    companionLayers: ['inland_water_temp'],
    renderer: 'shoreline-geometry-plus-temp/bait-overlays',
    requestPolicy: 'read-only geometry cache + explicit build-cache + live temp companion',
    clearPolicy: 'geometry persists across temp/bait enrichment; clear-only-on-pill-off',
    polygonPolicy: 'stable lake/river ids; temp and bait overlays are children, never shoreline replacements',
    debugReplacement: '/gfs/api/inland-water + /gfs/api/inland-water/build-cache',
  }),
});

const ALIASES = Object.freeze({
  boats: 'boater',
  boater_awareness: 'boater',
  current: 'boater',
  shark: 'shark-intel',
  sharks: 'shark-intel',
  sharkIntel: 'shark-intel',
  shark_intel: 'shark-intel',
  tiger: 'shark-intel',
  sand_shark: 'shark-intel',
  currents: 'boater',
  inland: 'inland-water',
  inlandwater: 'inland-water',
  inlandWater: 'inland-water',
  inland_water: 'inland-water',
  inland_waterways: 'inland-water',
  inlandTemp: 'inland_water_temp',
  inland_water_temp: 'inland_water_temp',
});

export function normalizeWorldLayerName(name) {
  const raw = String(name || '').trim();
  return ALIASES[raw] || raw;
}

export function sceneLayersForWorldPill(name) {
  const layer = normalizeWorldLayerName(name);
  const contract = WORLD_LAYER_CONTRACTS[layer];
  if (contract?.cacheLayers) return [...contract.cacheLayers];
  if (layer === 'inland_water_temp') return ['inland_water_temp'];
  return layer ? [layer] : [];
}


const INLAND_DETAIL_TIERS = Object.freeze(new Set(['harbor', 'local', 'coastal', 'regional']));
const INLAND_OVERVIEW_TIERS = Object.freeze(new Set(['world', 'overview', 'regional', 'coastal', 'local', 'harbor']));
function sceneTierForViewport(viewport) {
  const v = viewport || {};
  const west = Number(v.west), east = Number(v.east), south = Number(v.south), north = Number(v.north);
  if (![west, east, south, north].every(Number.isFinite)) return 'world';
  const width = Math.abs(east - west);
  const height = Math.abs(north - south);
  const span = Math.max(width || 0, height || 0);
  const area = Math.max(0, width * height);
  if (span <= 1.6 && area <= 2.6) return 'harbor';
  if (span <= 4.0 && area <= 14.0) return 'coastal';
  if (span <= 12.0 && area <= 90.0) return 'regional';
  return 'world';
}
function inlandDetailAllowed(viewport) {
  return INLAND_DETAIL_TIERS.has(sceneTierForViewport(viewport));
}
function inlandOverviewAllowed(viewport) {
  return INLAND_OVERVIEW_TIERS.has(sceneTierForViewport(viewport));
}
function filterInlandLayersForViewport(layers, viewport) {
  const list = unique(layers);
  // Keep inland-water and inland_water_temp at world zoom for cheap outline/temp priming.
  // Bait rendering is gated inside the renderer by scene tier / payload flag.
  return inlandOverviewAllowed(viewport) ? list : list.filter((l) => l !== 'inland-water' && l !== 'inland_water_temp');
}

function unique(list) {
  return [...new Set((list || []).map(normalizeWorldLayerName).filter(Boolean))];
}

export class WorldSubscriptionRenderer {
  constructor({
    engine,
    getViewport,
    readSceneCache,
    nudgeRefresh,
    clearVisuals,
    syncPill,
    rememberChoice,
    renderLocations,
    latestLocations,
    layerPrefs,
    defaultEnabled,
    isUnavailable,
    refreshInland,
    ensureJetstream,
    debug,
  } = {}) {
    this.engine = engine;
    this.getViewport = getViewport;
    this.readSceneCache = readSceneCache;
    this.nudgeRefresh = nudgeRefresh;
    this.clearVisuals = clearVisuals;
    this.syncPill = syncPill;
    this.rememberChoice = rememberChoice;
    this.renderLocations = renderLocations;
    this.latestLocations = latestLocations || (() => []);
    this.layerPrefs = layerPrefs || (() => ({}));
    this.defaultEnabled = defaultEnabled || (() => true);
    this.isUnavailable = isUnavailable || (() => false);
    this.refreshInland = refreshInland;
    this.ensureJetstream = ensureJetstream;
    this.debug = debug || (() => {});
    this.lastLayerRequestAt = new Map();
    this.lastViewportRequestAt = 0;
    this.contracts = WORLD_LAYER_CONTRACTS;
  }

  layerOn(name) {
    const layerName = normalizeWorldLayerName(name);
    if (layerName === 'locations') return this.layerPrefs().locations !== false;
    const entry = this.engine?.layers?.[layerName];
    if (this.layerPrefs()[layerName] === false) return false;
    return entry ? Boolean(entry.enabled) : this.defaultEnabled(layerName);
  }

  activeMask(options = {}) {
    const includeStatic = Boolean(options.includeStatic);
    const includeCompanions = options.includeCompanions !== false;
    const viewport = options.viewport || this.getViewport?.();
    const inlandAllowed = inlandDetailAllowed(viewport);
    const openingFast = /boot_open_fast|website_load|initial|opening/i.test(String(options.reason || ''));
    return {
      locations: includeStatic && this.layerOn('locations'),
      clouds: this.layerOn('clouds'),
      rain: this.layerOn('rain'),
      lightning: this.layerOn('lightning'),
      jetstream: this.layerOn('jetstream'),
      bait: !openingFast && this.layerOn('bait'),
      sharkIntel: !openingFast && this.layerOn('shark-intel'),
      boater: !openingFast && this.layerOn('boater'),
      inlandWater: includeStatic && inlandOverviewAllowed(viewport) && this.layerOn('inland-water'),
      inlandWaterTemp: includeCompanions && inlandOverviewAllowed(viewport) && this.layerOn('inland-water'),
    };
  }

  activeSceneLayers(options = {}) {
    if (Array.isArray(options.layers) && options.layers.length) return filterInlandLayersForViewport(options.layers.flatMap(sceneLayersForWorldPill), options.viewport || this.getViewport?.());
    const mask = this.activeMask(options);
    const layers = [];
    if (mask.locations) layers.push('locations');
    if (mask.clouds) layers.push('clouds');
    if (mask.rain) layers.push('rain');
    if (mask.lightning) layers.push('lightning');
    if (mask.jetstream) layers.push('jetstream');
    if (mask.bait) layers.push('bait');
    if (mask.sharkIntel) layers.push('shark-intel');
    if (mask.boater) layers.push('boater');
    if (mask.inlandWater) layers.push('inland-water');
    if (mask.inlandWaterTemp) layers.push('inland_water_temp');
    return unique(layers);
  }

  enabledMap() {
    const out = {};
    for (const key of Object.keys(this.contracts)) out[key] = this.layerOn(key);
    return out;
  }

  async setLayerEnabled(name, enabled, { reason = 'pill_toggle', subscribe = true } = {}) {
    const layerName = normalizeWorldLayerName(name);
    const next = enabled !== false;
    if (layerName === 'locations') {
      this.rememberChoice?.('locations', next);
      if (next) this.renderLocations?.(this.latestLocations?.() || []);
      else this.clearVisuals?.('locations');
      this.syncPill?.('locations', next);
      this.debug('pill/toggle', { layer: 'locations', enabled: next, staticLayer: true, contract: this.contracts.locations });
      return true;
    }

    const layer = this.engine?.layers?.[layerName];
    if (!layer) {
      this.debug('pill/missing-layer', { layer: layerName, requested: name });
      console.warn('[gfs world] missing layer', { layer: layerName, requested: name });
      return false;
    }
    this.rememberChoice?.(layerName, next);
    this.engine?.setEnabled?.(layerName, next);
    this.syncPill?.(layerName, next);
    this.debug('pill/toggle', { layer: layerName, enabled: next, contract: this.contracts[layerName], activeLayers: this.enabledMap() });

    if (!next) {
      this.clearVisuals?.(layerName);
      return true;
    }
    if (subscribe) await this.requestLayer(layerName, `pill_${layerName}_on:${reason}`);
    return true;
  }

  async requestLayer(name, reason = 'pill_on', { readDelayMs = 20, nudgeDelayMs = 160 } = {}) {
    const layerName = normalizeWorldLayerName(name);
    let layers = sceneLayersForWorldPill(layerName);
    if (layerName === 'inland-water' && !inlandOverviewAllowed(this.getViewport?.())) {
      this.clearVisuals?.('inland-water');
      this.debug('world/inland-overview-request-skip', { layer: layerName, reason, tier: sceneTierForViewport(this.getViewport?.()), policy: 'inland_water_overview_not_allowed_for_this_view' });
      return null;
    }
    layers = filterInlandLayersForViewport(layers, this.getViewport?.());
    if (!layers.length) return null;
    const now = Date.now();
    const key = `${layerName}:${layers.join(',')}`;
    const last = Number(this.lastLayerRequestAt.get(key) || 0);
    if (last && (now - last) < 900) {
      this.debug('world/request-layer-deduped', { layer: layerName, layers, reason, ageMs: now - last });
      return null;
    }
    this.lastLayerRequestAt.set(key, now);
    const viewport = this.getViewport?.();
    const includeStatic = layers.includes('inland-water') || layers.includes('locations');
    this.debug('world/subscribe-layer', { layer: layerName, layers, reason, contract: this.contracts[layerName] || null });

    window.setTimeout(() => {
      this.readSceneCache?.(viewport, `${reason}_first_paint`, { mode: 'fast', fast: true, refresh: false, layers, includeStatic }).catch?.(() => {});
    }, readDelayMs);
    window.setTimeout(() => {
      this.nudgeRefresh?.(viewport, `${reason}_background_refresh`, { layers, includeStatic }).catch?.(() => {});
    }, nudgeDelayMs);
    if (layerName === 'inland-water') {
      window.setTimeout(() => this.refreshInland?.(viewport, `${reason}_inland_geometry_read`)?.catch?.(() => {}), nudgeDelayMs + 80);
    }
    if (layerName === 'jetstream') {
      window.setTimeout(() => this.ensureJetstream?.(reason)?.catch?.(() => {}), nudgeDelayMs + 100);
    }
    return { ok: true, layer: layerName, layers, reason };
  }

  async refreshViewport(viewport = null, reason = 'steady', options = {}) {
    const vp = viewport || this.getViewport?.();
    const layers = this.activeSceneLayers({ ...options, viewport: vp });
    if (!layers.length) {
      this.debug('world/refresh-skip', { reason, policy: 'no_active_scene_layers' });
      return null;
    }
    const now = Date.now();
    if (options.dedupe !== false && (now - this.lastViewportRequestAt) < 250 && /camera_move|mousemove/i.test(String(reason))) {
      this.debug('world/refresh-deduped', { reason, ageMs: now - this.lastViewportRequestAt, layers });
      return null;
    }
    this.lastViewportRequestAt = now;
    this.debug('world/refresh-start', { reason, layers, active: this.enabledMap(), contract: 'requests_create_cache_cache_creates_frame_frame_creates_rendering' });
    if (this.layerOn('jetstream')) this.ensureJetstream?.(reason)?.catch?.(() => {});
    const payload = await this.readSceneCache?.(vp, `${reason}_scene_cache_fast`, { mode: 'fast', fast: true, refresh: false, layers, includeStatic: layers.includes('inland-water') || layers.includes('locations') });
    this.nudgeRefresh?.(vp, `${reason}_background_refresh`, { layers, includeStatic: layers.includes('inland-water') || layers.includes('locations') })?.catch?.(() => {});
    if (this.layerOn('inland-water') && inlandOverviewAllowed(vp)) this.refreshInland?.(vp, `${reason}_inland_geometry_read`)?.catch?.(() => {});
    else if (this.layerOn('inland-water')) this.clearVisuals?.('inland-water');
    this.debug('world/refresh-complete', { reason, layers, ok: Boolean(payload) });
    return payload;
  }
}
