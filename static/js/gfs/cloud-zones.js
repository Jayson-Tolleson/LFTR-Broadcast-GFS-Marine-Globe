import { normalizePolygonFieldPayload } from './polygon_math.js';
import { createPolygon3D } from './polygon3d.js';

const CLOUD_PRESSURE_BANDS = {
  low: { baseHpa: 940, topHpa: 760, threshold: 20, color: '#dcefff' },
  mid: { baseHpa: 760, topHpa: 520, threshold: 18, color: '#eef6ff' },
  high: { baseHpa: 520, topHpa: 220, threshold: 14, color: '#ffffff' },
  total: { baseHpa: 880, topHpa: 520, threshold: 35, color: '#e9f4ff' },
};
const MAX_CLOUD_BODIES = Number(window.GFS_CLOUD_MAX_REGIONS || 240);
// User-preferred dense cloud texture: 4x particles, with rendering caps still bounded.
const CLOUD_PARTICLE_MULTIPLIER = Number(window.GFS_CLOUD_PARTICLE_MULTIPLIER || 4.0);
const CLOUD_PARTICLE_SIZE_MULTIPLIER = Number(window.GFS_CLOUD_PARTICLE_SIZE_MULTIPLIER || 0.25);

const CLOUD_PARTICLE_RENDER_STYLE = String(window.GFS_CLOUD_PARTICLE_RENDER_STYLE || window.__GFS_CLOUD_PARTICLE_RENDER_STYLE || 'hybrid_ellipse_clusters').toLowerCase();
const CLOUD_HYBRID_ELLIPSE_MIN_PER_BODY = Number(window.GFS_CLOUD_HYBRID_ELLIPSE_MIN_PER_BODY || 3);
const CLOUD_HYBRID_ELLIPSE_MAX_PER_BODY = Number(window.GFS_CLOUD_HYBRID_ELLIPSE_MAX_PER_BODY || 8);
const CLOUD_HYBRID_ELLIPSE_MICRO_MIN = Number(window.GFS_CLOUD_HYBRID_ELLIPSE_MICRO_MIN || 3);
const CLOUD_HYBRID_ELLIPSE_MICRO_MAX = Number(window.GFS_CLOUD_HYBRID_ELLIPSE_MICRO_MAX || 9);
const CLOUD_HYBRID_CLUSTER_SCALE = Number(window.GFS_CLOUD_HYBRID_CLUSTER_SCALE || 1.0);


function cloudParticleDemandMode() {
  const explicit = String(window.GFS_CLOUD_PARTICLE_MODE || window.__GFS_CLOUD_PARTICLE_MODE || '').toLowerCase();
  if (explicit) return explicit;
  if (window.__GFS_PARTY_TIME === true || window.__GFS_CLOUDS_PARTY_TIME === true) return 'high';
  return 'balanced';
}

function cloudParticleGovernorCap({ requested, deviceCap, mobile, viewportTier, replacingExisting = false }) {
  const mode = cloudParticleDemandMode();
  if (mode === 'off' || mode === 'none') return 0;
  const tier = String(viewportTier || '').toLowerCase();
  const tierHardCap = tier === 'harbor' ? (mobile ? 900 : 1800)
    : (tier === 'local' ? (mobile ? 760 : 1500)
      : (tier === 'regional' || tier === 'coastal' ? (mobile ? 520 : 1050)
        : (mobile ? 220 : 420)));
  const transitionScale = replacingExisting ? 0.42 : 1.0;
  const baseRequested = Math.max(0, Math.round(requested || tierHardCap));
  if (mode === 'high' || mode === 'party') return Math.max(120, Math.min(deviceCap, tierHardCap, Math.round(baseRequested * transitionScale)));
  if (mode === 'low' || mode === 'safe') return Math.max(80, Math.min(tierHardCap, Math.round(baseRequested * 0.40 * transitionScale)));
  if (mode === 'eco') return Math.max(40, Math.min(tierHardCap, Math.round(baseRequested * 0.22 * transitionScale)));
  // balanced: shells provide the cloud mass; particles are accents and are sharply reduced on world/global views.
  const balancedScale = tier === 'harbor' ? 0.72 : (tier === 'local' ? 0.58 : (tier === 'regional' || tier === 'coastal' ? 0.44 : 0.20));
  return Math.max(60, Math.min(deviceCap, tierHardCap, Math.round(baseRequested * balancedScale * transitionScale)));
}

// Hard cap: Jay asked for up to 500 cloud sprites total. Each sprite is a soft
// SVG cluster containing micro-ellipses, so this replaces thousands of costly
// marker particles while preserving cloud texture.
const DESKTOP_MAX_CLOUD_PARTICLES = Number(window.GFS_MAX_CLOUD_PARTICLES_DESKTOP || 500);
const MOBILE_MAX_CLOUD_PARTICLES = Number(window.GFS_MAX_CLOUD_PARTICLES_MOBILE || 320);
const MAX_CLOUD_PARTICLES = DESKTOP_MAX_CLOUD_PARTICLES;
// Cloud shells are expensive Google 3D polygons. Keep animation gentle:
// particles drift more often, shells follow at a slower cadence to avoid path-rewrite flicker.
const MAX_ADVECTION_STEP_SEC = Number(window.GFS_CLOUD_MAX_ADVECTION_STEP_SEC ?? 0.04);
const CLOUD_ADVECTION_INTERVAL_MS = Number(window.GFS_CLOUD_ADVECTION_INTERVAL_MS ?? 250);
// Shell path rewrites are expensive and can flash. Sprites advect continuously;
// shells morph slowly and only when the layer truly changes.
const CLOUD_SHELL_PATH_UPDATE_MS = Number(window.GFS_CLOUD_SHELL_PATH_UPDATE_MS ?? 12000);
const CLOUD_RENDER_MIN_REUSE_MS = Number(window.GFS_CLOUD_RENDER_MIN_REUSE_MS ?? 600000);
const CLOUD_GRID_JITTER_FRACTION = 0.42;
const CLOUD_PATH_SMOOTHING_ITERATIONS = 1;
const CLOUD_PATH_SMOOTHING_MAX_POINTS = 160;

function polygonApiPath() {
  return window.google?.maps?.maps3d?.Polygon3DElement ? 'Polygon3DElement.path' : 'gmp-polygon-3d.path';
}

function toNumber(v, fallback = 0) {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function normalizeFraction(value, fallback = 0.6) {
  const n = Number(value);
  if (!Number.isFinite(n)) return clamp(fallback, 0, 1);
  if (n > 1) return clamp(n / 100, 0, 1);
  return clamp(n, 0, 1);
}


// Cloud shells need a different material profile than rain/bait polygons.
// Keep the vertical sidewalls visually solid, make the lower/underside shells
// present but not black, and leave only a light cap/stroke shade for fill definition, while preserving neon edge outlines.
const CLOUD_SHELL_OPACITY = {
  sideMin: 0.62,
  sideMax: 0.90,
  undersideMin: 0.50,
  undersideMax: 0.74,
  capShadeMin: 0.14,
  capShadeMax: 0.32,
};

const CLOUD_HEIGHT_VISUAL_SCALE = Number(window.GFS_CLOUD_HEIGHT_VISUAL_SCALE || 1.42);
const CLOUD_EXTRUSION_VISUAL_SCALE = Number(window.GFS_CLOUD_EXTRUSION_VISUAL_SCALE || 1.35);

export function setCloudShellOpacityScale(value) {
  const n = Number(value);
  const scale = Number.isFinite(n) ? clamp(n, 0.0, 1.0) : 0.92;
  try { window.__gfsCloudShellOpacityScale = scale; } catch (_) {}
  try { window.GFS_CLOUD_SHELL_OPACITY = scale; } catch (_) {}
  try { localStorage.setItem('gfs.cloudShellOpacity', String(scale)); } catch (_) {}
  try { document.documentElement.style.setProperty('--gfs-cloud-shell-opacity-scale', String(scale)); } catch (_) {}
  try { window.__gfsDebugEvent?.('clouds/opacity-slider', { scale, percent: Math.round(scale * 100) }); } catch (_) {}
  refreshCloudShellOpacityScale();
  return scale;
}

function cloudShellOpacityScale() {
  const n = Number(window.__gfsCloudShellOpacityScale ?? window.GFS_CLOUD_SHELL_OPACITY);
  return Number.isFinite(n) ? clamp(n, 0.0, 1.0) : 0.92;
}

function finalCloudShellOpacity(baseOpacity, fadeScale = 1, max = 1.0) {
  // Slider is now literal transparency: 0% = invisible, 100% = full requested cloud material.
  return clamp(toNumber(baseOpacity, 0.2) * cloudShellOpacityScale() * clamp(toNumber(fadeScale, 1), 0, 1), 0.0, max);
}

function scaledCloudShellOpacity(value) {
  return finalCloudShellOpacity(value, 1, 1.0);
}

function applyCloudShellOpacity(element, fadeScale = null) {
  if (!element) return;
  const fade = fadeScale == null
    ? clamp(toNumber(element.__gfsFadeScale ?? element.getAttribute?.('data-cloud-fade-scale'), 1), 0, 1)
    : clamp(toNumber(fadeScale, 1), 0, 1);
  const baseFill = clamp(toNumber(element.__gfsBaseFillOpacity ?? element.getAttribute?.('data-cloud-base-fill-opacity') ?? element.getAttribute?.('fill-opacity'), 0.22), 0, 1);
  const baseStroke = clamp(toNumber(element.__gfsBaseStrokeOpacity ?? element.getAttribute?.('data-cloud-base-stroke-opacity') ?? element.getAttribute?.('stroke-opacity'), 0.12), 0, 1);
  const fill = finalCloudShellOpacity(baseFill, fade, 1.0);
  const stroke = cloudPartyModeEnabled() && cloudShellOpacityScale() > 0 ? 1 : finalCloudShellOpacity(baseStroke, fade, 1.0);
  try { element.__gfsFadeScale = fade; } catch (_) {}
  setVisualAttr(element, 'data-cloud-fade-scale', fade.toFixed(3));
  setVisualAttr(element, 'fill-opacity', fill.toFixed(3));
  setVisualAttr(element, 'stroke-opacity', stroke.toFixed(3));
  setVisualProp(element, 'fillOpacity', fill);
  setVisualProp(element, 'strokeOpacity', stroke);
}

function applyCloudParticleOpacity(element) {
  if (!element) return;
  const scale = cloudShellOpacityScale();
  try { element.style.opacity = String(clamp(scale, 0, 1)); } catch (_) {}
  setVisualAttr(element, 'data-cloud-particle-opacity-scale', scale.toFixed(3));
}

window.refreshCloudShellOpacity = refreshCloudShellOpacityScale;
window.setGfsCloudShellOpacity = setCloudShellOpacityScale;

export function refreshCloudShellOpacityScale() {
  try {
    document.querySelectorAll?.('[data-gfs-layer="clouds"],[data-cloud-shell]').forEach((el) => {
      applyCloudShellOpacity(el);
    });
    document.querySelectorAll?.('[data-cloud-particle]').forEach((el) => {
      applyCloudParticleOpacity(el);
    });
  } catch (_) {}
}

function cloudExtrudedHeight(height, cloudFraction = 0.6, extra = 0) {
  const h = Math.max(90, toNumber(height, 900));
  const scaledH = h * CLOUD_HEIGHT_VISUAL_SCALE;
  const fraction = normalizeFraction(cloudFraction, 0.6);
  const ratio = (0.58 + fraction * 0.42 + toNumber(extra, 0)) * CLOUD_EXTRUSION_VISUAL_SCALE;
  return Math.max(140, Math.min(scaledH * ratio, scaledH, 2400));
}

try {
  if (window.__gfsCloudShellOpacityScale == null) {
    const savedRaw = localStorage.getItem('gfs.cloudShellOpacity');
    const saved = savedRaw == null ? NaN : Number(savedRaw);
    window.__gfsCloudShellOpacityScale = Number.isFinite(saved) ? clamp(saved > 2 ? saved / 100 : saved, 0.0, 1.0) : 0.92;
  }
} catch (_) { try { window.__gfsCloudShellOpacityScale = 0.92; } catch (__) {} }

const PARTY_TIME_CLOUD_EDGE_COLORS = ['#ff3b30', '#34c759', '#0a84ff', '#ffd60a', '#bf5af2', '#30d5c8', '#111111'];
const PARTY_TIME_FRAME_MS = 260;
let partyTimeIntervalId = null;


function setVisualProp(target, prop, value) {
  try { target[prop] = value; } catch (_) {}
}

function setVisualAttr(target, attr, value) {
  if (value == null) return;
  try { target.setAttribute(attr, String(value)); } catch (_) {}
}

function cloudPartyModeEnabled() {
  return typeof window !== 'undefined' && window.__gfsPartyTime === true;
}

function stablePartySeed(input = '') {
  const str = String(input || 'cloud-party');
  let hash = 0;
  for (let i = 0; i < str.length; i += 1) hash = ((hash * 31) + str.charCodeAt(i)) >>> 0;
  return hash >>> 0;
}

function windChaseOffsetForElement(element) {
  if (!element) return 0;
  const u = toNumber(element.getAttribute?.('data-cloud-wind-u'), 0);
  const v = toNumber(element.getAttribute?.('data-cloud-wind-v'), 0);
  const lat = toNumber(element.getAttribute?.('data-cloud-anchor-lat'), 0);
  const lon = toNumber(element.getAttribute?.('data-cloud-anchor-lon'), 0);
  const speed = Math.hypot(u, v);
  if (speed < 0.05) return 0;
  const nx = u / speed;
  const ny = v / speed;
  const projected = (lon * nx) + (lat * ny);
  const spatialBands = Math.round(projected * 3.25);
  const speedBands = Math.round(clamp(speed * 0.45, 0, 5));
  return spatialBands + speedBands;
}

function partyEdgeColorForShell(key = '') {
  const phase = Math.max(0, toNumber((typeof window !== 'undefined' ? window.__gfsPartyTimePhase : 0), 0));
  const idx = (stablePartySeed(key) + phase) % PARTY_TIME_CLOUD_EDGE_COLORS.length;
  return PARTY_TIME_CLOUD_EDGE_COLORS[idx] || '#ff3b30';
}

function partyEdgeColorForElement(element) {
  const key = element?.getAttribute?.('data-cloud-party-key') || element?.getAttribute?.('data-cloud-base-stroke-color') || 'cloud-party';
  const phase = Math.max(0, toNumber((typeof window !== 'undefined' ? window.__gfsPartyTimePhase : 0), 0));
  const chaseOffset = windChaseOffsetForElement(element);
  const idx = (stablePartySeed(key) + phase - chaseOffset) % PARTY_TIME_CLOUD_EDGE_COLORS.length;
  const wrapped = ((idx % PARTY_TIME_CLOUD_EDGE_COLORS.length) + PARTY_TIME_CLOUD_EDGE_COLORS.length) % PARTY_TIME_CLOUD_EDGE_COLORS.length;
  return PARTY_TIME_CLOUD_EDGE_COLORS[wrapped] || '#ff3b30';
}

function stopCloudPartyAnimation() {
  if (partyTimeIntervalId != null) {
    try { window.clearInterval(partyTimeIntervalId); } catch (_) {}
    partyTimeIntervalId = null;
  }
}

function startCloudPartyAnimation(root = null) {
  stopCloudPartyAnimation();
  if (!cloudPartyModeEnabled()) return;
  if (typeof window !== 'undefined') {
    if (!Number.isFinite(Number(window.__gfsPartyTimePhase))) window.__gfsPartyTimePhase = 0;
  }
  partyTimeIntervalId = window.setInterval(() => {
    if (!cloudPartyModeEnabled()) {
      stopCloudPartyAnimation();
      return;
    }
    try { window.__gfsPartyTimePhase = (toNumber(window.__gfsPartyTimePhase, 0) + 1) % PARTY_TIME_CLOUD_EDGE_COLORS.length; } catch (_) {}
    applyCloudPartyModeToDom(root || document.querySelector('gmp-map-3d') || document);
  }, PARTY_TIME_FRAME_MS);
}

function decorateCloudShellElement(element, { shellKey = '', baseStrokeColor = null, baseFillOpacity = null, baseStrokeOpacity = null, baseStrokeWidth = null, windU = null, windV = null, anchorLat = null, anchorLon = null } = {}) {
  if (!element) return element;
  const resolvedColor = String(baseStrokeColor || element.getAttribute?.('stroke-color') || element.strokeColor || element.getAttribute?.('data-gfs-neon-color') || '#ffffff');
  const resolvedFillOpacity = clamp(toNumber(baseFillOpacity, toNumber(element.getAttribute?.('fill-opacity'), toNumber(element.fillOpacity, 0.22))), 0, 1);
  const resolvedOpacity = clamp(toNumber(baseStrokeOpacity, toNumber(element.getAttribute?.('stroke-opacity'), toNumber(element.strokeOpacity, 0.98))), 0, 1);
  const resolvedWidth = Math.max(0.25, toNumber(baseStrokeWidth, toNumber(element.getAttribute?.('stroke-width'), toNumber(element.strokeWidth, 2.4))));
  setVisualAttr(element, 'data-cloud-party-key', shellKey || `${resolvedColor}|${resolvedWidth}`);
  setVisualAttr(element, 'data-cloud-base-stroke-color', resolvedColor);
  setVisualAttr(element, 'data-cloud-base-fill-opacity', resolvedFillOpacity);
  try { element.__gfsBaseFillOpacity = resolvedFillOpacity; } catch (_) {}
  setVisualAttr(element, 'data-cloud-base-stroke-opacity', resolvedOpacity);
  try { element.__gfsBaseStrokeOpacity = resolvedOpacity; } catch (_) {}
  setVisualAttr(element, 'data-cloud-base-stroke-width', resolvedWidth);
  if (windU != null) setVisualAttr(element, 'data-cloud-wind-u', toNumber(windU, 0));
  if (windV != null) setVisualAttr(element, 'data-cloud-wind-v', toNumber(windV, 0));
  if (anchorLat != null) setVisualAttr(element, 'data-cloud-anchor-lat', toNumber(anchorLat, 0));
  if (anchorLon != null) setVisualAttr(element, 'data-cloud-anchor-lon', toNumber(anchorLon, 0));
  applyCloudShellVisualMode(element);
  return element;
}

function applyCloudShellVisualMode(element) {
  if (!element) return;
  const baseStrokeColor = String(element.getAttribute?.('data-cloud-base-stroke-color') || element.getAttribute?.('stroke-color') || '#ffffff');
  const baseStrokeOpacity = clamp(toNumber(element.getAttribute?.('data-cloud-base-stroke-opacity'), 0.98), 0, 1);
  const baseStrokeWidth = Math.max(0.25, toNumber(element.getAttribute?.('data-cloud-base-stroke-width'), 2.4));
  if (cloudPartyModeEnabled()) {
    const partyColor = partyEdgeColorForElement(element);
    const partyWidth = clamp(Math.max(baseStrokeWidth * 4, baseStrokeWidth + 6.5), 8, 22);
    setVisualProp(element, 'strokeColor', partyColor);
    setVisualProp(element, 'strokeWidth', partyWidth);
    setVisualAttr(element, 'stroke-color', partyColor);
    setVisualAttr(element, 'stroke-width', partyWidth);
    applyCloudShellOpacity(element);
    setVisualAttr(element, 'data-gfs-neon-color', partyColor);
    setVisualAttr(element, 'data-cloud-party-color', partyColor);
    try {
      element.style.filter = `drop-shadow(0 0 2px ${partyColor}) drop-shadow(0 0 6px ${partyColor}) drop-shadow(0 0 14px ${partyColor})`;
      element.style.opacity = '';
    } catch (_) {}
  } else {
    setVisualProp(element, 'strokeColor', baseStrokeColor);
    setVisualProp(element, 'strokeWidth', baseStrokeWidth);
    setVisualAttr(element, 'stroke-color', baseStrokeColor);
    setVisualAttr(element, 'stroke-width', baseStrokeWidth);
    applyCloudShellOpacity(element);
    setVisualAttr(element, 'data-gfs-neon-color', baseStrokeColor);
    try {
      element.style.filter = '';
      element.style.opacity = '';
    } catch (_) {}
  }
}

function applyCloudPartyModeToDom(root = null) {
  const scope = root || document;
  try {
    scope.querySelectorAll?.('[data-cloud-shell]').forEach((el) => applyCloudShellVisualMode(el));
  } catch (_) {}
  if (cloudPartyModeEnabled()) startCloudPartyAnimation(scope);
  else stopCloudPartyAnimation();
}

function cloudShellFillOpacity({ baseOpacity = 0.18, cloudFraction = 0.6, shell = '', family = '' } = {}) {
  const cover = normalizeFraction(cloudFraction, 0.6);
  const base = clamp(toNumber(baseOpacity, 0.18), 0, 1);
  const role = String(shell || '').toLowerCase();
  const fam = String(family || '').toLowerCase();
  const underside = role.includes('under') || role.includes('base') || role.includes('bottom');
  const vertical = fam.includes('vertical') || role.includes('tower') || role.includes('core');
  const targetMin = underside ? CLOUD_SHELL_OPACITY.undersideMin : CLOUD_SHELL_OPACITY.sideMin;
  const targetMax = underside ? CLOUD_SHELL_OPACITY.undersideMax : CLOUD_SHELL_OPACITY.sideMax;
  const boosted = targetMin + ((targetMax - targetMin) * clamp((cover * 0.78) + (base * 1.15), 0, 1));
  return scaledCloudShellOpacity(clamp(boosted + (vertical ? 0.04 : 0), targetMin, Math.min(0.90, targetMax + (vertical ? 0.05 : 0))));
}

function cloudShellCapShadeOpacity({ cloudFraction = 0.6, shell = '' } = {}) {
  const cover = normalizeFraction(cloudFraction, 0.6);
  const role = String(shell || '').toLowerCase();
  const underside = role.includes('under') || role.includes('bottom');
  if (underside) return scaledCloudShellOpacity(0.08);
  return scaledCloudShellOpacity(clamp(CLOUD_SHELL_OPACITY.capShadeMin + cover * 0.14, CLOUD_SHELL_OPACITY.capShadeMin, CLOUD_SHELL_OPACITY.capShadeMax));
}

function pressureAtFraction(baseHpa, topHpa, fraction) {
  const base = Number(baseHpa);
  const top = Number(topHpa);
  if (!Number.isFinite(base) || !Number.isFinite(top)) return null;
  const t = clamp(Number(fraction) || 0, 0, 1);
  // pressure falls with height; keep interpolation in pressure space and convert to height
  return base + ((top - base) * t);
}

function wrapLongitude(lon) {
  let value = Number(lon) || 0;
  while (value < -180) value += 360;
  while (value >= 180) value -= 360;
  return value;
}

function mixPoint(a, b, t) {
  const altitudeA = toNumber(a?.altitude ?? a?.altitude_m, NaN);
  const altitudeB = toNumber(b?.altitude ?? b?.altitude_m, altitudeA);
  const out = {
    lat: toNumber(a?.lat, 0) + (toNumber(b?.lat, 0) - toNumber(a?.lat, 0)) * t,
    lng: wrapLongitude(toNumber(a?.lng ?? a?.lon, 0) + (toNumber(b?.lng ?? b?.lon, 0) - toNumber(a?.lng ?? a?.lon, 0)) * t),
  };
  if (Number.isFinite(altitudeA) || Number.isFinite(altitudeB)) {
    out.altitude = toNumber(altitudeA, 0) + (toNumber(altitudeB, altitudeA) - toNumber(altitudeA, 0)) * t;
  }
  return out;
}

function smoothClosedCloudPath(path, iterations = CLOUD_PATH_SMOOTHING_ITERATIONS, maxPoints = CLOUD_PATH_SMOOTHING_MAX_POINTS) {
  if (!Array.isArray(path) || path.length < 4 || iterations <= 0) return path || [];
  let ring = path.map((p) => ({
    lat: toNumber(p?.lat, 0),
    lng: wrapLongitude(toNumber(p?.lng ?? p?.lon, 0)),
    ...(Number.isFinite(toNumber(p?.altitude ?? p?.altitude_m, NaN)) ? { altitude: toNumber(p?.altitude ?? p?.altitude_m, 0) } : {}),
  }));
  for (let iter = 0; iter < iterations; iter += 1) {
    if (ring.length * 2 > maxPoints) break;
    const next = [];
    for (let i = 0; i < ring.length; i += 1) {
      const a = ring[i];
      const b = ring[(i + 1) % ring.length];
      next.push(mixPoint(a, b, 0.25));
      next.push(mixPoint(a, b, 0.75));
    }
    ring = next;
  }
  return ring;
}

function uniqueId(prefix = 'cloud') {
  return `${prefix}-${Math.random().toString(36).slice(2, 10)}`;
}

function makeCloudParticleTemplate({ family = 'stratiform', color = '#ffffff', opacity = 0.36, scale = 1, sparkle = 0.25, shear = 0, seed = 0, cloudFraction = 0.55, shellHeight = 900, clusterDensity = 0.5 }) {
  const uid = uniqueId('cloud-ellipse-cluster');
  const style = CLOUD_PARTICLE_RENDER_STYLE;
  const baseScale = Math.max(0.02, toNumber(scale, 1) * CLOUD_PARTICLE_SIZE_MULTIPLIER * CLOUD_HYBRID_CLUSTER_SCALE);
  const density = clamp(toNumber(clusterDensity, cloudFraction), 0.05, 1.0);
  const heightScale = clamp(Math.sqrt(Math.max(120, toNumber(shellHeight, 900)) / 900), 0.72, 1.85);
  const familyStretch = family === 'cirriform' ? 2.05 : (family === 'stratiform' ? 1.46 : (family === 'vertical' ? 1.02 : 1.24));
  const familyLift = family === 'vertical' ? 1.52 : (family === 'cirriform' ? 0.72 : 1.0);
  const width = Math.round(clamp(42 * baseScale * familyStretch * (0.90 + density * 0.45), 16, 160));
  const height = Math.round(clamp(30 * baseScale * familyLift * heightScale * (0.82 + density * 0.36), 12, 128));
  const rot = Math.max(-32, Math.min(32, shear + ((seed - 0.5) * 14)));
  const coreOpacity = Math.min(0.96, opacity + 0.34 + sparkle * 0.10);
  const midOpacity = Math.min(0.80, opacity + 0.14 + sparkle * 0.08);
  const edgeOpacity = Math.max(0.0, opacity * 0.05);
  const tpl = document.createElement('template');

  // Hybrid approach: one marker object represents many internal cloud specks.
  // That lets each cloud body look filled with mass without 100s of DOM/map
  // particles per body.
  let micro = '';
  if (style.includes('hybrid') || style.includes('ellipse')) {
    const microCount = Math.round(clamp(
      CLOUD_HYBRID_ELLIPSE_MICRO_MIN + density * (CLOUD_HYBRID_ELLIPSE_MICRO_MAX - CLOUD_HYBRID_ELLIPSE_MICRO_MIN),
      4,
      48,
    ));
    for (let i = 0; i < microCount; i += 1) {
      const a = hashJitter(seed * 1000 + i, density * 100, i + 1) * Math.PI * 2;
      const rr = Math.pow(hashJitter(density * 37, seed * 991, i + 17), 0.58);
      const cx = 36 + Math.cos(a) * rr * (20 + density * 6);
      const cy = 23 + Math.sin(a) * rr * (9 + density * 5);
      const rx = 2.0 + hashJitter(seed, i, 31) * (5.6 + density * 5.2);
      const ry = 1.1 + hashJitter(i, seed, 47) * (2.8 + density * 2.4);
      const o = clamp(opacity * (0.12 + density * 0.26) * (0.60 + hashJitter(i, seed, 59) * 0.65), 0.02, 0.22);
      const fill = hashJitter(i, density, seed) > 0.72 ? '#ffffff' : color;
      micro += `<ellipse cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" rx="${rx.toFixed(1)}" ry="${ry.toFixed(1)}" fill="${fill}" fill-opacity="${o.toFixed(3)}"/>`;
    }
  }

  tpl.innerHTML = `
    <svg width="${width}" height="${height}" viewBox="0 0 72 46" xmlns="http://www.w3.org/2000/svg" aria-hidden="true" style="pointer-events:none;overflow:visible;filter:drop-shadow(0 0 ${Math.round(4 + sparkle * 6 + density * 4)}px rgba(238,248,255,${Math.min(0.36, opacity + 0.10).toFixed(2)}))">
      <defs>
        <radialGradient id="${uid}-core" cx="34%" cy="28%" r="78%">
          <stop offset="0%" stop-color="#ffffff" stop-opacity="${coreOpacity.toFixed(2)}"/>
          <stop offset="54%" stop-color="${color}" stop-opacity="${midOpacity.toFixed(2)}"/>
          <stop offset="100%" stop-color="${color}" stop-opacity="${edgeOpacity.toFixed(2)}"/>
        </radialGradient>
        <linearGradient id="${uid}-shear" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stop-color="#fff" stop-opacity="${Math.min(0.46, opacity + 0.14).toFixed(2)}"/>
          <stop offset="100%" stop-color="${color}" stop-opacity="0"/>
        </linearGradient>
      </defs>
      <g transform="rotate(${rot.toFixed(1)} 36 23)">
        <ellipse cx="36" cy="24" rx="33" ry="16" fill="url(#${uid}-core)" fill-opacity="${opacity.toFixed(2)}"/>
        <ellipse cx="27" cy="17" rx="16" ry="7" fill="#fff" fill-opacity="${Math.min(0.50, opacity + 0.12).toFixed(2)}"/>
        <ellipse cx="45" cy="18" rx="13" ry="6" fill="url(#${uid}-shear)" fill-opacity="${Math.min(0.40, opacity + 0.08).toFixed(2)}"/>
        <g data-cloud-micro-particles="hybrid">${micro}</g>
      </g>
    </svg>`;
  return tpl;
}

function createCloudParticleMarker(item) {
  const marker = document.createElement('gmp-marker-3d');
  marker.position = { lat: item.lat, lng: item.lon, altitude: item.altitude };
  marker.drawsWhenOccluded = true;
  marker.sizePreserved = true;
  marker.setAttribute('data-gfs-layer', 'clouds');
  marker.setAttribute('data-cloud-particle', item.family || 'cloud');
  marker.setAttribute('data-cloud-particle-style', CLOUD_PARTICLE_RENDER_STYLE);
  marker.setAttribute('data-cloud-cluster-density', String(toNumber(item.clusterDensity, item.cloudFraction || 0.5).toFixed(3)));
  marker.append(makeCloudParticleTemplate(item));
  applyCloudParticleOpacity(marker);
  return marker;
}

function cloudParticleCount(layer) {
  const cloudFraction = normalizeFraction(layer?.cloudFraction ?? layer?.cover ?? layer?.coverage, clamp(toNumber(layer?.opacity, 0.12) * 3.8, 0.22, 0.92));
  const thickness = Math.max(300, toNumber(layer?.height, 900));
  const thicknessScore = clamp(Math.sqrt(thickness / 900), 0.0, 1.0);
  const footprintScore = clamp((toNumber(layer?.latRadiusDeg, 0.04) + toNumber(layer?.lonRadiusDeg, 0.04)) * 7.5, 0.0, 1.0);
  const familyBonus = layer?.family === 'vertical' ? 2.0 : (layer?.family === 'cumuliform' ? 1.4 : (layer?.family === 'cirriform' ? -1.0 : 0.0));
  const minCount = Math.max(2, Math.round(CLOUD_HYBRID_ELLIPSE_MIN_PER_BODY));
  const maxCount = Math.max(minCount, Math.round(CLOUD_HYBRID_ELLIPSE_MAX_PER_BODY));
  const densityScore = clamp((cloudFraction * 0.58) + (thicknessScore * 0.24) + (footprintScore * 0.18), 0, 1);
  const count = Math.round(minCount + densityScore * (maxCount - minCount) + familyBonus);
  const mobile = /android|iphone|ipad|mobile/i.test(navigator.userAgent || '');
  const mobileMax = Math.max(6, Math.min(maxCount, Math.round(maxCount * 0.72)));
  return clamp(count, minCount, mobile ? mobileMax : maxCount);
}

function buildCloudParticlesForBody(body, layer, particleBudget) {
  if (!body || particleBudget.remaining <= 0) return [];
  const particles = [];
  const count = Math.min(cloudParticleCount(layer), particleBudget.remaining);
  const heading = windHeadingDeg(body.windU, body.windV) * Math.PI / 180;
  const cosH = Math.cos(heading);
  const sinH = Math.sin(heading);
  const innerLatRadius = Math.max(0.006, toNumber(layer.latRadiusDeg, 0.04) * 0.68);
  const innerLonRadius = Math.max(0.006, toNumber(layer.lonRadiusDeg, 0.04) * 0.68);
  const shellHeight = Math.max(300, toNumber(layer.height, 900));
  const baseAltitude = toNumber(layer.baseAltitude, 1500);
  const topAltitude = Math.max(baseAltitude + 50, toNumber(layer.topAltitude, baseAltitude + shellHeight));
  const cloudFraction = normalizeFraction(layer.cloudFraction ?? layer.cover ?? layer.coverage, clamp(toNumber(layer.opacity, 0.12) * 3.8, 0.22, 0.92));
  const opacity = clamp((0.05 + cloudFraction * 0.26) * (0.88 + toNumber(layer.opacity, 0.12) * 0.5), 0.06, 0.42);
  for (let idx = 0; idx < count; idx += 1) {
    const seed = hashJitter(body.anchorLat, body.anchorLon, idx + baseAltitude * 0.001 + shellHeight * 0.0001);
    const theta = (seed + idx / Math.max(1, count)) * Math.PI * 2;
    const r = Math.pow(hashJitter(body.anchorLon, body.anchorLat, idx + 12), 0.62) * 0.92;
    const band = 0.72 + hashJitter(body.anchorLat, body.anchorLon, idx + 18) * 0.42;
    const localLat = Math.sin(theta) * innerLatRadius * r * band;
    const localLon = Math.cos(theta) * innerLonRadius * r * (0.84 + hashJitter(body.anchorLon, body.anchorLat, idx + 24) * 0.38);
    const rotLat = (localLat * cosH) - (localLon * sinH);
    const rotLon = (localLat * sinH) + (localLon * cosH);
    const verticalSeed = hashJitter(body.anchorLat, body.anchorLon, idx + 43);
    const vertical = clamp(0.10 + Math.pow(verticalSeed, layer.family === 'vertical' ? 0.72 : 0.92) * 0.82, 0.08, 0.98);
    const pressureHpa = pressureAtFraction(layer.basePressureHpa, layer.topPressureHpa, vertical);
    const pressureAltitude = Number.isFinite(pressureHpa) ? pressureToHeightMeters(pressureHpa) : NaN;
    const linearAltitude = baseAltitude + ((topAltitude - baseAltitude) * vertical);
    const altitude = Math.round(Number.isFinite(pressureAltitude) ? clamp(pressureAltitude, Math.min(baseAltitude, topAltitude), Math.max(baseAltitude, topAltitude)) : linearAltitude);
    const particleOpacity = clamp(opacity * (0.78 + cloudFraction * 0.44) * (0.82 + hashJitter(body.anchorLon, body.anchorLat, idx + 171) * 0.24) * (0.84 + vertical * 0.18), 0.05, 0.50);
    particles.push({
      lat: body.anchorLat + rotLat,
      lon: wrapLongitude(body.anchorLon + rotLon),
      altitude,
      anchorLat: body.anchorLat,
      anchorLon: body.anchorLon,
      baseLocalLat: rotLat,
      baseLocalLon: rotLon,
      baseAltitude: altitude,
      shellId: body.shellId,
      shellHeight,
      windU: body.windU,
      windV: body.windV,
      localU: (hashJitter(body.anchorLat, body.anchorLon, idx + 70) - 0.5) * 1.35,
      localV: (hashJitter(body.anchorLon, body.anchorLat, idx + 84) - 0.5) * 1.35,
      wobblePhase: hashJitter(body.anchorLat, body.anchorLon, idx + 101) * Math.PI * 2,
      wobbleRate: 0.34 + hashJitter(body.anchorLon, body.anchorLat, idx + 111) * 0.82,
      wobbleLatAmp: innerLatRadius * (0.06 + hashJitter(body.anchorLat, body.anchorLon, idx + 121) * 0.13),
      wobbleLonAmp: innerLonRadius * (0.06 + hashJitter(body.anchorLon, body.anchorLat, idx + 131) * 0.13),
      latOffsetDeg: 0,
      lonOffsetDeg: 0,
      family: layer.family || 'stratiform',
      color: layer.color || '#ffffff',
      opacity: particleOpacity,
      cloudFraction,
      scale: (0.78 + hashJitter(body.anchorLat, body.anchorLon, idx + 151) * 1.35) * (0.88 + cloudFraction * 0.42) * clamp(Math.sqrt(shellHeight / 900), 0.72, 1.65),
      sparkle: hashJitter(body.anchorLon, body.anchorLat, idx + 159),
      clusterDensity: cloudFraction,
      particleRenderStyle: CLOUD_PARTICLE_RENDER_STYLE,
      shear: clamp((body.windU || 0) * 0.7, -18, 18),
      seed: hashJitter(body.anchorLat, body.anchorLon, idx + 167),
      marker: null,
    });
  }
  particleBudget.remaining -= particles.length;
  return particles;
}

function to2DGrid(value) {
  if (!Array.isArray(value)) return [];
  if (!Array.isArray(value[0])) return [];
  if (Array.isArray(value[0][0])) return value[0];
  return value;
}

function bboxFromPayload(payload) {
  const candidates = [payload?.bbox, payload?.bbox_used, payload?.viewport_bbox, payload?.meta?.bbox, payload?.meta?.bbox_used];
  for (const value of candidates) {
    if (Array.isArray(value) && value.length >= 4) {
      return { west: toNumber(value[0]), south: toNumber(value[1]), east: toNumber(value[2]), north: toNumber(value[3]) };
    }
    if (value && typeof value === 'object') {
      const west = toNumber(value.west, NaN);
      const south = toNumber(value.south, NaN);
      const east = toNumber(value.east, NaN);
      const north = toNumber(value.north, NaN);
      if ([west, south, east, north].every(Number.isFinite)) return { west, south, east, north };
    }
  }
  const items = Array.isArray(payload?.items) ? payload.items : [];
  const b = items.map((item) => item?.bounds || {}).filter(Boolean);
  const west = Math.min(...b.map((x) => toNumber(x.lon_min, Infinity)));
  const south = Math.min(...b.map((x) => toNumber(x.lat_min, Infinity)));
  const east = Math.max(...b.map((x) => toNumber(x.lon_max, -Infinity)));
  const north = Math.max(...b.map((x) => toNumber(x.lat_max, -Infinity)));
  if ([west, south, east, north].every(Number.isFinite)) return { west, south, east, north };
  return null;
}


function normalizeFeatureCenter(feature, bbox = null) {
  const safe = feature || {};
  let lat = toNumber(safe.lat ?? safe.latitude ?? safe.anchorLat ?? safe.centerLat ?? safe.center?.lat, NaN);
  let lon = toNumber(safe.lon ?? safe.lng ?? safe.longitude ?? safe.anchorLon ?? safe.centerLon ?? safe.center?.lon ?? safe.center?.lng, NaN);
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;

  // Catch the common swapped-coordinate failure: {lat:-118, lon:34}.
  if ((lat < -90 || lat > 90) && lon >= -90 && lon <= 90) {
    const oldLat = lat;
    lat = lon;
    lon = oldLat;
  }
  if (lat < -90 || lat > 90) return null;
  lon = wrapLongitude(lon);
  if (bbox && !pointNearBbox(lat, lon, bbox)) return null;
  return { lat, lon };
}

function bboxPadDeg(bbox) {
  if (!bbox) return { lat: 0.5, lon: 0.5 };
  const latSpan = Math.abs(toNumber(bbox.north, 0) - toNumber(bbox.south, 0));
  const lonSpan = Math.abs(toNumber(bbox.east, 0) - toNumber(bbox.west, 0));
  return {
    lat: clamp(latSpan * 0.18, 0.35, 3.5),
    lon: clamp(lonSpan * 0.18, 0.35, 5.0),
  };
}

function pointNearBbox(lat, lon, bbox) {
  if (!bbox) return true;
  const pad = bboxPadDeg(bbox);
  const south = Math.min(bbox.south, bbox.north) - pad.lat;
  const north = Math.max(bbox.south, bbox.north) + pad.lat;
  if (lat < south || lat > north) return false;
  const west = wrapLongitude(bbox.west - pad.lon);
  const east = wrapLongitude(bbox.east + pad.lon);
  const x = wrapLongitude(lon);
  if (west <= east) return x >= west && x <= east;
  return x >= west || x <= east;
}


export function cloudPayloadHasDrawableContent(payload) {
  if (!payload) return false;
  const seaCloudLayer = payload?.layers?.clouds || payload?.seaIntelligence?.layers?.clouds || null;
  if (seaCloudLayer && Array.isArray(seaCloudLayer.features) && seaCloudLayer.features.length) return true;
  if (Array.isArray(payload?.polygon_field_v1?.features) && payload.polygon_field_v1.features.length) return true;
  const items = (Array.isArray(payload?.tiles) && payload.tiles.length) ? payload.tiles : (Array.isArray(payload?.items) ? payload.items : []);
  if (items.length) return true;
  if (Array.isArray(payload?.scene?.clouds) && payload.scene.clouds.length) return true;
  if (Array.isArray(payload?.cloud_regions) && payload.cloud_regions.length) return true;
  const layers = Array.isArray(payload?.cloud_layers) ? payload.cloud_layers : [];
  if (layers.some((l) => Array.isArray(l?.density) && l.density.length)) return true;
  const fields = payload?.fields || {};
  return Boolean(fields.cloud_total || fields.cloud_low || fields.cloud_mid || fields.cloud_high || payload?.cloud_cover);
}

function cloudPayloadState(payload) {
  return String(payload?.source_state || payload?.payload_state || payload?.status || '').toLowerCase();
}

function stableCloudTileJitter(item, bbox = null) {
  const bounds = item?.bounds || {};
  const center = item?.center || {};
  const lat = toNumber(bounds.lat_center ?? center.lat ?? item?.lat, 0);
  const lon = toNumber(bounds.lon_center ?? center.lon ?? center.lng ?? item?.lon ?? item?.lng, 0);
  const cellLat = Math.max(0.0001, Math.abs(toNumber(bounds.lat_max, lat) - toNumber(bounds.lat_min, lat)));
  const cellLon = Math.max(0.0001, Math.abs(toNumber(bounds.lon_max, lon) - toNumber(bounds.lon_min, lon)));
  const tileKey = String(item?.tile_id || item?.id || `${lat.toFixed(4)}:${lon.toFixed(4)}`);
  let salt = 0;
  for (let i = 0; i < tileKey.length; i += 1) salt += tileKey.charCodeAt(i) * (i + 1);
  const j1 = hashJitter(lat, lon, salt + 11) - 0.5;
  const j2 = hashJitter(lon, lat, salt + 29) - 0.5;
  const scale = CLOUD_GRID_JITTER_FRACTION;
  return {
    lat: j1 * cellLat * scale,
    lon: j2 * cellLon * scale,
    cellLat,
    cellLon,
    salt,
  };
}

function organicizeCloudPath(path, seedLat, seedLon, salt = 0, jitter = null) {
  if (!Array.isArray(path) || path.length < 3) return path || [];
  const center = ringCenterAndScale(path);
  const latShift = toNumber(jitter?.lat, 0);
  const lonShift = toNumber(jitter?.lon, 0);
  const maxLatWarp = Math.max(0.001, toNumber(jitter?.cellLat, center.latRadiusDeg || 0.02) * 0.10);
  const maxLonWarp = Math.max(0.001, toNumber(jitter?.cellLon, center.lonRadiusDeg || 0.02) * 0.10);
  const warped = path.map((pt, idx) => {
    const phase = (idx / Math.max(1, path.length)) * Math.PI * 2;
    const n1 = hashJitter(seedLat + idx * 0.017, seedLon - idx * 0.019, salt + idx * 7) - 0.5;
    const n2 = hashJitter(seedLon - idx * 0.013, seedLat + idx * 0.011, salt + idx * 13) - 0.5;
    const radial = 0.72 + 0.28 * Math.sin(phase * 2.0 + salt * 0.01);
    return {
      lat: pt.lat + latShift + n1 * maxLatWarp * radial,
      lng: wrapLongitude(pt.lng + lonShift + n2 * maxLonWarp * radial),
    };
  });
  return smoothClosedCloudPath(warped, CLOUD_PATH_SMOOTHING_ITERATIONS);
}

function clearCloudLayerNodes(map3DElement = null) {
  stopAllCloudAdvection('clear_cloud_layer_nodes');
  const roots = [];
  if (map3DElement) roots.push(map3DElement);
  if (typeof document !== 'undefined') roots.push(document);
  const selectors = [
    '[data-gfs-layer="clouds"]',
    '[data-cloud-shell]',
    '[data-cloud-particle]',
    '[data-gfs-layer="ocean-fx"]',
    '[data-ocean-fx-kind]',
    'gmp-marker-3d[data-cloud-particle]',
    'gmp-polygon-3d[data-cloud-shell]',
  ].join(',');
  for (const root of roots) {
    try {
      root.querySelectorAll?.(selectors)?.forEach((el) => {
        try { el.remove(); } catch (_) {}
      });
    } catch (_) {}
  }
}

if (typeof window !== 'undefined') {
  window.__gfsPartyTime = window.__gfsPartyTime === true;
  window.__gfsPartyTimePhase = 0;
  window.clearGfsCloudLayer = function clearGfsCloudLayer() {
    clearCloudLayerNodes(document.querySelector('gmp-map-3d') || document.querySelector('gmp-map') || null);
  };
  window.setCloudPartyMode = function setCloudPartyMode(enabled) {
    window.__gfsPartyTime = enabled === true;
    if (window.__gfsPartyTime !== true) window.__gfsPartyTimePhase = 0;
    applyCloudPartyModeToDom(document.querySelector('gmp-map-3d') || document);
    return window.__gfsPartyTime;
  };
}

function densityPercent(value) {
  const n = toNumber(value, 0);
  if (n <= 1.25) return clamp(n * 100, 0, 100);
  return clamp(n, 0, 100);
}

function tileFromCloudRegion(region) {
  if (!region || typeof region !== 'object') return null;
  const c = region.center || {};
  const b = region.bbox || {};
  const intensity = region.intensity || {};
  const wind = region.wind || {};
  return {
    id: region.id,
    tile_id: region.id,
    bounds: {
      lat_center: c.lat,
      lon_center: c.lon,
      lat_min: b.south,
      lat_max: b.north,
      lon_min: b.west,
      lon_max: b.east,
    },
    low_density: intensity.low,
    mid_density: intensity.mid,
    high_density: intensity.high,
    coverage: toNumber(intensity.total, 0) * 100,
    precip_rate: toNumber(intensity.rain, 0),
    wind: { mid: { u: wind.u, v: wind.v } },
    cloud_type: region.cloud_type,
    regime: region.family,
    method: region.method || 'cloud_region_marching_squares_v1',
    cloud_region: region,
    region_footprint: region.footprint || [],
  };
}

function cloudTileFeatures(payload, bbox = null) {
  let items = Array.isArray(payload?.tiles) && payload.tiles.length
    ? payload.tiles
    : (Array.isArray(payload?.items) && payload.items.length ? payload.items : []);
  if ((!items || !items.length) && Array.isArray(payload?.cloud_regions) && payload.cloud_regions.length) {
    items = payload.cloud_regions.map(tileFromCloudRegion).filter(Boolean);
  }
  const out = [];
  for (const item of items) {
    const bounds = item?.bounds || {};
    const center = item?.center || {};
    const normalizedCenter = normalizeFeatureCenter({
      lat: bounds.lat_center ?? center.lat ?? item.lat,
      lon: bounds.lon_center ?? center.lon ?? center.lng ?? item.lon ?? item.lng,
    }, bbox);
    if (!normalizedCenter) continue;
    const { lat, lon } = normalizedCenter;
    const hints = Array.isArray(item.layer_hints) ? item.layer_hints : [];
    const lowHint = hints.find((x) => x?.band === 'low') || {};
    const midHint = hints.find((x) => x?.band === 'mid') || {};
    const highHint = hints.find((x) => x?.band === 'high') || {};
    const low = densityPercent(item.low_density ?? item.cloud_low ?? item.bands?.low?.density ?? lowHint.density);
    const mid = densityPercent(item.mid_density ?? item.cloud_mid ?? item.bands?.mid?.density ?? midHint.density);
    const high = densityPercent(item.high_density ?? item.cloud_high ?? item.bands?.high?.density ?? highHint.density);
    const total = densityPercent(item.coverage ?? item.cloud_total ?? item.estimated_density ?? Math.max(low, mid, high));
    const precip = toNumber(item.precip_rate ?? item.precipitation_factor ?? item.rain_factor ?? item.source_fields?.proxy_precip_rate, 0);
    const wind = item.wind || {};
    const windMid = wind.mid || wind.low || wind.high || {};
    const jitter = stableCloudTileJitter(item, bbox);
    const jitteredCenter = normalizeFeatureCenter({ lat: lat + jitter.lat, lon: lon + jitter.lon }, bbox) || { lat, lon };
    out.push({
      lat: jitteredCenter.lat,
      lon: jitteredCenter.lon,
      grid_lat: lat,
      grid_lon: lon,
      _organicJitter: jitter,
      cloud_low: low,
      cloud_mid: mid,
      cloud_high: high,
      cloud_total: Math.max(total, low, mid, high),
      precip_rate: precip,
      wind_u: toNumber(windMid.u ?? item.wind_u, 0),
      wind_v: toNumber(windMid.v ?? item.wind_v, 0),
      cell_lat_deg: Math.max(Math.abs(toNumber(bounds.lat_max, lat) - toNumber(bounds.lat_min, lat)), 0.04),
      cell_lon_deg: Math.max(Math.abs(toNumber(bounds.lon_max, lon) - toNumber(bounds.lon_min, lon)), 0.04),
      tile_id: item.tile_id || item.id || `${lat.toFixed(3)}:${lon.toFixed(3)}`,
      rawTile: item,
      _serverShellsAvailable: !!(item?.bands && Object.values(item.bands).some((b) => Array.isArray(b?.shells) && b.shells.length && Array.isArray(b?.footprints))),
      _cloudRegionMethod: item.method || item.source_method || item.cloud_region?.method || null,
    });
  }
  return out;
}

function latLonFromIndex(i, j, ny, nx, bbox) {
  const lat = bbox.south + ((i + 0.5) / Math.max(1, ny)) * (bbox.north - bbox.south);
  const lon = bbox.west + ((j + 0.5) / Math.max(1, nx)) * (bbox.east - bbox.west);
  return { lat, lon };
}

function cloudFeaturesFromContract(payload, bbox = null) {
  const raw = normalizePolygonFieldPayload(payload?.polygon_field_v1 || null);
  const out = [];
  for (const feature of raw) {
    const center = normalizeFeatureCenter(feature, bbox);
    if (!center) continue;
    out.push({ ...feature, lat: center.lat, lon: center.lon });
  }
  return out;
}

function contourPathFromFeature(feature, altitude = 0) {
  const raw = Array.isArray(feature?.path) ? feature.path : [];
  const path = [];
  for (const pt of raw) {
    const lat = toNumber(pt?.lat ?? pt?.latitude, NaN);
    const lng = toNumber(pt?.lng ?? pt?.lon ?? pt?.longitude, NaN);
    const alt = toNumber(pt?.altitude ?? pt?.altitude_m, altitude);
    if (!Number.isFinite(lat) || !Number.isFinite(lng) || lat < -90 || lat > 90 || lng < -180 || lng > 180) continue;
    const prev = path[path.length - 1];
    if (prev && Math.abs(prev.lat - lat) < 1e-8 && Math.abs(prev.lng - lng) < 1e-8) continue;
    path.push({ lat, lng, altitude: Number.isFinite(alt) ? alt : altitude });
  }
  if (path.length >= 2) {
    const first = path[0];
    const last = path[path.length - 1];
    if (Math.abs(first.lat - last.lat) < 1e-8 && Math.abs(first.lng - last.lng) < 1e-8) path.pop();
  }
  return path.length >= 3 ? smoothClosedCloudPath(path, CLOUD_PATH_SMOOTHING_ITERATIONS) : [];
}

function featureCentroidFromPath(path) {
  if (!Array.isArray(path) || !path.length) return null;
  let lat = 0;
  let lon = 0;
  for (const p of path) { lat += toNumber(p.lat, 0); lon += toNumber(p.lng, 0); }
  return { lat: lat / path.length, lon: lon / path.length };
}

function seaContourCloudFeatures(payload) {
  const raw = Array.isArray(payload?.polygon_field_v1?.features)
    ? payload.polygon_field_v1.features
    : (Array.isArray(payload?.layers?.clouds?.features) ? payload.layers.clouds.features : []);
  const features = [];
  for (const feature of raw) {
    if (!Array.isArray(feature?.path) || feature.path.length < 3) continue;
    const path = contourPathFromFeature(feature, toNumber(feature.altitude_m, 2600));
    if (path.length < 3) continue;
    const centroid = featureCentroidFromPath(path);
    if (!centroid) continue;
    features.push({ ...feature, path, lat: centroid.lat, lon: centroid.lon });
  }
  return features;
}

function cellSizeDeg(bbox, ny, nx) {
  return {
    lat: Math.abs((bbox.north - bbox.south) / Math.max(1, ny)),
    lon: Math.abs((bbox.east - bbox.west) / Math.max(1, nx)),
  };
}

function hashJitter(lat, lon, salt = 0) {
  const v = Math.sin((lat * 12.9898) + (lon * 78.233) + (salt * 19.19)) * 43758.5453;
  return v - Math.floor(v);
}

function pressureToHeightMeters(hpa) {
  const pressure = clamp(toNumber(hpa, 1013.25), 80, 1050);
  return 44330 * (1 - Math.pow(pressure / 1013.25, 0.1903));
}

function metersToLatDegrees(meters) {
  return meters / 111320;
}

function metersToLonDegrees(meters, lat) {
  const lonScale = Math.max(0.2, Math.cos((Number(lat) * Math.PI) / 180));
  return meters / (111320 * lonScale);
}

function advectPath(basePath, latOffsetDeg, lonOffsetDeg) {
  return basePath.map((point) => ({
    lat: point.lat + latOffsetDeg,
    lng: wrapLongitude(point.lng + lonOffsetDeg),
  }));
}

function roundedCloudPath({ lat, lon, latRadiusDeg, lonRadiusDeg, wobble = 0.18, points = 14, elongation = 1, headingDeg = 90 }) {
  const path = [];
  const basePhase = hashJitter(lat, lon) * Math.PI * 2;
  const heading = (headingDeg * Math.PI) / 180;
  const cosH = Math.cos(heading);
  const sinH = Math.sin(heading);
  for (let i = 0; i < points; i += 1) {
    const t = (i / points) * Math.PI * 2;
    const harmonic = Math.sin((t * 2) + basePhase) * wobble;
    const secondary = Math.cos((t * 3) - basePhase) * (wobble * 0.55);
    const scale = 1 + harmonic + secondary;
    const localLat = Math.sin(t) * latRadiusDeg * scale;
    const localLon = Math.cos(t) * lonRadiusDeg * scale * elongation;
    const rotLat = (localLat * cosH) - (localLon * sinH);
    const rotLon = (localLat * sinH) + (localLon * cosH);
    path.push({ lat: lat + rotLat, lng: lon + rotLon });
  }
  return smoothClosedCloudPath(path, 1, CLOUD_PATH_SMOOTHING_MAX_POINTS);
}

function buildCloudPressureBand(type, density, totalBoost, family = 'stratiform') {
  const band = CLOUD_PRESSURE_BANDS[type] || CLOUD_PRESSURE_BANDS.total;
  const weight = clamp(density / 100, 0, 1);
  const deepening = clamp((totalBoost - 0.82) / 0.45, 0, 1);
  const familyStretch = family === 'vertical' ? 1.55 : (family === 'cumuliform' ? 1.34 : (family === 'cirriform' ? 0.96 : 1.16));
  const baseHpa = band.baseHpa - ((band.baseHpa - band.topHpa) * weight * 0.16);
  const topHpa = band.topHpa - ((band.topHpa * 0.07) * deepening * weight * familyStretch);
  const baseAltitude = pressureToHeightMeters(baseHpa);
  const topAltitude = pressureToHeightMeters(topHpa);
  return {
    baseAltitude: Math.round(baseAltitude),
    topAltitude: Math.round(topAltitude),
    height: Math.max(850, Math.round((topAltitude - baseAltitude) * familyStretch * CLOUD_HEIGHT_VISUAL_SCALE)),
    color: band.color,
    weight,
    baseHpa,
    topHpa,
  };
}

function classifyCloudMorphology(feature) {
  const low = toNumber(feature?.cloud_low, 0);
  const mid = toNumber(feature?.cloud_mid, 0);
  const high = toNumber(feature?.cloud_high, 0);
  const total = toNumber(feature?.cloud_total, Math.max(low, mid, high));
  const precip = toNumber(feature?.precip_rate, 0);
  const dominant = high >= Math.max(low, mid) ? 'high' : (mid >= low ? 'mid' : 'low');

  if (precip >= 0.85 && total >= 72) return { family: 'vertical', subtype: 'cumulonimbus' };
  if (precip >= 0.35 && mid >= 45 && total >= 68) return { family: 'stratiform', subtype: 'nimbostratus' };
  if (high >= 58 && low < 35 && mid < 45) return { family: 'cirriform', subtype: high >= 78 ? 'cirrostratus' : 'cirrus' };
  if (low >= 58 && total < 75) return { family: 'cumuliform', subtype: low >= 78 ? 'towering-cumulus' : 'cumulus' };
  if (dominant === 'mid' && total >= 62) return { family: 'stratiform', subtype: 'altostratus' };
  return { family: 'stratiform', subtype: total >= 60 ? 'stratus' : 'stratocumulus' };
}

function shellBlueprints(morphology) {
  switch (morphology.family) {
    case 'cirriform':
      return [
        { shell: 'veil', latScale: 0.82, lonScale: 1.8, opacityBase: 0.045, opacitySpan: 0.045, wobble: 0.12, points: 16, elongation: 1.6 },
        { shell: 'streak', latScale: 0.56, lonScale: 1.45, opacityBase: 0.03, opacitySpan: 0.035, wobble: 0.08, points: 12, elongation: 1.9 },
      ];
    case 'vertical':
      return [
        { shell: 'core', latScale: 0.72, lonScale: 0.76, opacityBase: 0.09, opacitySpan: 0.10, wobble: 0.2, points: 14, elongation: 1.0 },
        { shell: 'tower', latScale: 0.5, lonScale: 0.54, opacityBase: 0.08, opacitySpan: 0.08, wobble: 0.24, points: 12, elongation: 0.92, baseLift: 0.22, heightBoost: 0.42 },
        { shell: 'anvil', latScale: 0.96, lonScale: 1.42, opacityBase: 0.052, opacitySpan: 0.06, wobble: 0.16, points: 16, elongation: 1.25, baseLift: 0.78, heightBoost: 0.18 },
      ];
    case 'cumuliform':
      return [
        { shell: 'body', latScale: 0.7, lonScale: 0.84, opacityBase: 0.075, opacitySpan: 0.10, wobble: 0.24, points: 14, elongation: 1.0 },
        { shell: 'tuft', latScale: 0.46, lonScale: 0.5, opacityBase: 0.055, opacitySpan: 0.075, wobble: 0.3, points: 11, elongation: 0.94, baseLift: 0.38, heightBoost: 0.26 },
      ];
    default:
      return [
        { shell: 'deck', latScale: 0.96, lonScale: 1.3, opacityBase: 0.07, opacitySpan: 0.09, wobble: 0.14, points: 16, elongation: 1.18 },
        { shell: 'underside', latScale: 0.82, lonScale: 1.08, opacityBase: 0.04, opacitySpan: 0.055, wobble: 0.1, points: 14, elongation: 1.12, baseLift: 0.14, heightBoost: 0.08 },
      ];
  }
}


function parseCssColorAndOpacity(value, fallbackColor = '#ffffff', fallbackOpacity = 0.25) {
  const text = String(value || '').trim();
  const m = text.match(/^rgba?\(([^)]+)\)$/i);
  if (m) {
    const parts = m[1].split(',').map((x) => x.trim());
    const r = clamp(Math.round(toNumber(parts[0], 255)), 0, 255);
    const g = clamp(Math.round(toNumber(parts[1], 255)), 0, 255);
    const b = clamp(Math.round(toNumber(parts[2], 255)), 0, 255);
    const a = parts.length >= 4 ? clamp(toNumber(parts[3], fallbackOpacity), 0, 1) : fallbackOpacity;
    return { color: `rgb(${r}, ${g}, ${b})`, opacity: a };
  }
  return { color: text || fallbackColor, opacity: fallbackOpacity };
}

function normalizeBackendRing(points, bbox = null) {
  const out = [];
  for (const p of Array.isArray(points) ? points : []) {
    const lat = toNumber(p?.lat, NaN);
    const lon = wrapLongitude(toNumber(p?.lng ?? p?.lon, NaN));
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
    if (lat < -90 || lat > 90 || lon < -180 || lon >= 180) continue;
    if (bbox && (lat < bbox.south - 0.25 || lat > bbox.north + 0.25 || lon < bbox.west - 0.25 || lon > bbox.east + 0.25)) continue;
    const prev = out[out.length - 1];
    if (prev && Math.abs(prev.lat - lat) < 1e-8 && Math.abs(prev.lng - lon) < 1e-8) continue;
    out.push({ lat, lng: lon });
  }
  if (out.length >= 4) {
    const a = out[0];
    const b = out[out.length - 1];
    if (Math.abs(a.lat - b.lat) < 1e-8 && Math.abs(a.lng - b.lng) < 1e-8) out.pop();
  }
  return out.length >= 3 ? out : [];
}

function ringCenterAndScale(path) {
  if (!Array.isArray(path) || path.length < 3) return { lat: 0, lon: 0, latRadiusDeg: 0.04, lonRadiusDeg: 0.04 };
  let minLat = 90; let maxLat = -90; let minLon = 180; let maxLon = -180; let sumLat = 0; let sumLon = 0;
  for (const p of path) {
    minLat = Math.min(minLat, p.lat); maxLat = Math.max(maxLat, p.lat);
    minLon = Math.min(minLon, p.lng); maxLon = Math.max(maxLon, p.lng);
    sumLat += p.lat; sumLon += p.lng;
  }
  return {
    lat: sumLat / path.length,
    lon: sumLon / path.length,
    latRadiusDeg: Math.max(0.006, (maxLat - minLat) * 0.5),
    lonRadiusDeg: Math.max(0.006, (maxLon - minLon) * 0.5),
  };
}

function serverCloudShellLayersFromTile(tile, bbox = null, feature = null) {
  const out = [];
  const bands = tile?.bands || {};
  const regime = tile?.regime || 'cloud';
  for (const bandName of ['low', 'mid', 'high']) {
    const band = bands?.[bandName];
    if (!band || !Array.isArray(band.shells) || !Array.isArray(band.footprints)) continue;
    const wind = band.wind || {};
    for (const shell of band.shells) {
      const fp = band.footprints?.[Number(shell.footprint_ref) || 0];
      const rawPath = normalizeBackendRing(fp?.points, bbox);
      if (rawPath.length < 3) continue;
      const jitter = feature?._organicJitter || stableCloudTileJitter(tile, bbox);
      const path = organicizeCloudPath(rawPath, toNumber(feature?.grid_lat ?? feature?.lat ?? tile?.lat, 0), toNumber(feature?.grid_lon ?? feature?.lon ?? tile?.lon, 0), toNumber(jitter?.salt, 0) + toNumber(shell.z_index, 0) + bandName.length * 31, jitter);
      const center = ringCenterAndScale(path);
      const fill = parseCssColorAndOpacity(shell.fill, '#eef6ff', 0.22);
      const rawFillOpacity = clamp(fill.opacity, 0.08, 0.62);
      const stroke = parseCssColorAndOpacity(shell.stroke, fill.color, 0.06);
      stroke.opacity = clamp(stroke.opacity * 0.45, 0.04, 0.16);
      const baseAltitude = Math.max(0, toNumber(shell.base_m, band.base_altitude_m || 1200));
      const topAltitude = Math.max(baseAltitude + 50, toNumber(shell.top_m, band.top_altitude_m || baseAltitude + 900));
      const payloadExtrudedHeight = toNumber(shell.extruded_height_m ?? shell.extrude_height_m ?? shell.extrudedHeight ?? shell.extrudeHeight ?? shell.height_m ?? shell.height, NaN);
      const fracRaw = shell.cloud_fraction ?? shell.cover_fraction ?? shell.coverage ?? shell.cover ?? feature?.cloud_total ?? tile?.cloud_total ?? 60;
      const cloudFraction = normalizeFraction(fracRaw, 0.62);
      const pressureBandCfg = CLOUD_PRESSURE_BANDS[bandName] || CLOUD_PRESSURE_BANDS.total;
      out.push({
        path,
        lat: center.lat,
        lon: center.lon,
        baseAltitude,
        topAltitude,
        height: Math.max(50, topAltitude - baseAltitude),
        extrudedHeight: Number.isFinite(payloadExtrudedHeight) ? Math.max(50, payloadExtrudedHeight) : Math.max(50, topAltitude - baseAltitude),
        latRadiusDeg: center.latRadiusDeg,
        lonRadiusDeg: center.lonRadiusDeg,
        color: fill.color,
        opacity: cloudShellFillOpacity({ baseOpacity: rawFillOpacity, cloudFraction, shell: shell.role || 'shell', family: regime }),
        cloudFraction,
        strokeColor: stroke.color,
        strokeOpacity: cloudShellCapShadeOpacity({ cloudFraction, shell: shell.role || 'shell' }),
        basePressureHpa: pressureBandCfg.baseHpa,
        topPressureHpa: pressureBandCfg.topHpa,
        strokeWidth: toNumber(shell.stroke_width, 0.5),
        pressureBand: bandName,
        family: regime,
        subtype: shell.role || bandName,
        shell: shell.role || 'shell',
        windU: toNumber(wind.u, 0),
        windV: toNumber(wind.v, 0),
        zIndex: toNumber(shell.z_index, 0),
      });
    }
  }
  out.sort((a, b) => a.zIndex - b.zIndex);
  return out;
}

function makeServerCloudShellBody(layer) {
  const element = createPolygon3D({
    path: layer.path,
    altitude: layer.baseAltitude,
    altitudeMode: 'absolute',
    fillColor: layer.color,
    fillOpacity: layer.opacity,
    strokeColor: layer.strokeColor || layer.color,
    strokeOpacity: layer.strokeOpacity ?? 0.1,
    strokeWidth: layer.strokeWidth ?? 0.5,
    extrudedHeight: cloudExtrudedHeight(layer.extrudedHeight ?? layer.height, layer.cloudFraction),
    neonGlow: true,
    hover: {
      title: `${layer.family || 'cloud'} ${layer.pressureBand || ''} ${layer.shell || 'shell'}`.trim(),
      lines: [
        `Backend shell: ${layer.shell || 'shell'}`,
        `Band: ${layer.pressureBand || 'cloud'}`,
        `Base altitude: ${Math.round(layer.baseAltitude)} m`,
        `Shell height: ${Math.round(layer.height)} m`,
        `Extrude payload: ${Math.round(toNumber(layer.extrudedHeight, layer.height))} m`,
        `Opacity: ${Math.round(clamp(layer.opacity, 0, 1) * 100)}%`,
        `Cloud fraction: ${Math.round(normalizeFraction(layer.cloudFraction, 0.6) * 100)}%`,
        `Wind: ${Math.hypot(toNumber(layer.windU, 0), toNumber(layer.windV, 0)).toFixed(1)} m/s`,
      ],
      metrics: { layer: 'clouds', source: 'backend', path_points: layer.path?.length || 0, altitude_m: layer.baseAltitude },
      payload: { family: layer.family, band: layer.pressureBand, subtype: layer.subtype, base_altitude_m: layer.baseAltitude, top_altitude_m: layer.topAltitude || (layer.baseAltitude + layer.height), height_m: layer.height, extruded_height_m: toNumber(layer.extrudedHeight, layer.height), cover: layer.opacity, cloud_fraction: layer.cloudFraction, base_pressure_hpa: layer.basePressureHpa, top_pressure_hpa: layer.topPressureHpa, wind_u: layer.windU, wind_v: layer.windV, path_points: layer.path?.length || 0 },
    },
  });
  if (!element) return null;
  decorateCloudShellElement(element, {
    shellKey: [layer.family || 'cloud', layer.pressureBand || 'band', layer.shell || 'shell', Math.round(toNumber(layer.lat, 0) * 100), Math.round(toNumber(layer.lon, 0) * 100), Math.round(toNumber(layer.baseAltitude, 0))].join('|'),
    baseFillOpacity: toNumber(element.getAttribute?.('fill-opacity'), layer.opacity ?? 0.2),
    baseStrokeColor: element.getAttribute?.('stroke-color') || layer.strokeColor || layer.color,
    baseStrokeOpacity: toNumber(element.getAttribute?.('stroke-opacity'), layer.strokeOpacity ?? 0.1),
    baseStrokeWidth: toNumber(element.getAttribute?.('stroke-width'), layer.strokeWidth ?? 0.5),
    windU: layer.windU,
    windV: layer.windV,
    anchorLat: layer.lat,
    anchorLon: layer.lon,
  });
  try {
    element.setAttribute('data-gfs-layer', 'clouds');
    element.setAttribute('data-cloud-shell', layer.family || 'cloud');
    element.setAttribute('data-cloud-shell-source', 'backend');
  } catch (_) {}
  return {
    element,
    shellId: uniqueId('cloud-shell'),
    basePath: layer.path,
    anchorLat: layer.lat,
    anchorLon: layer.lon,
    windU: toNumber(layer.windU, 0),
    windV: toNumber(layer.windV, 0),
    latOffsetDeg: 0,
    lonOffsetDeg: 0,
    particleMeanLatOffsetDeg: 0,
    particleMeanLonOffsetDeg: 0,
    cloudFraction: normalizeFraction(layer.cloudFraction, 0.6),
    topAltitude: layer.topAltitude || (layer.baseAltitude + layer.height),
    basePressureHpa: layer.basePressureHpa,
    topPressureHpa: layer.topPressureHpa,
  };
}

function makeSeaContourCloudBody(feature, shellIndex = 0) {
  const level = clamp(toNumber(feature?.level, toNumber(feature?.cloud_total, 60) / 100), 0.05, 1);
  const total = Math.max(35, toNumber(feature?.cloud_total, level * 100));
  const low = toNumber(feature?.cloud_low, level * 42);
  const mid = toNumber(feature?.cloud_mid, level * 58);
  const high = toNumber(feature?.cloud_high, level * 72);
  const morphology = classifyCloudMorphology({ ...feature, cloud_total: total, cloud_low: low, cloud_mid: mid, cloud_high: high });
  const preferredBand = high >= Math.max(low, mid) ? 'high' : (mid >= low ? 'mid' : 'low');
  const band = buildCloudPressureBand(preferredBand, Math.max(low, mid, high, total), 1.0, morphology.family);
  const lift = shellIndex * Math.max(140, band.height * 0.18);
  const baseAltitude = Math.round(band.baseAltitude + lift);
  const path = contourPathFromFeature(feature, baseAltitude);
  if (path.length < 3) return null;
  const cloudFraction = normalizeFraction(total, clamp(level, 0.2, 0.95));
  const opacity = cloudShellFillOpacity({
    baseOpacity: 0.12 + level * 0.10 + shellIndex * 0.018,
    cloudFraction,
    shell: shellIndex === 0 ? 'contour-sidewall' : 'contour-cap',
    family: morphology.family,
  });
  const element = createPolygon3D({
    path,
    altitude: baseAltitude,
    altitudeMode: 'absolute',
    fillColor: band.color,
    fillOpacity: opacity,
    strokeColor: band.color,
    strokeOpacity: cloudShellCapShadeOpacity({ cloudFraction, shell: shellIndex === 0 ? 'contour-sidewall' : 'contour-cap' }),
    strokeWidth: shellIndex === 0 ? 0.7 : 0.45,
    extrudedHeight: cloudExtrudedHeight(band.height, cloudFraction, shellIndex * 0.08),
    neonGlow: true,
    hover: {
      title: 'FastAPI cloud contour shell',
      lines: [
        `Contour level: ${Math.round(level * 100)}%`,
        `Path points: ${path.length}`,
        `Raw points: ${feature.raw_path_len || feature.path_len || path.length}`,
        `Path stride: ${feature.path_stride || 1}`,
        `Detail: ${feature.path_detail || 'contour footprint'}`,
        `Band: ${preferredBand}`,
        `Cloud fraction: ${Math.round(cloudFraction * 100)}%`,
      ],
      metrics: { layer: 'clouds', source: 'fastapi_sea_contour', path_points: path.length, raw_path_points: feature.raw_path_len || path.length, path_stride: feature.path_stride || 1 },
      payload: { level, cloud_fraction: cloudFraction, base_pressure_hpa: band.baseHpa, top_pressure_hpa: band.topHpa, base_altitude_m: baseAltitude, top_altitude_m: baseAltitude + band.height, path_points: path.length, raw_path_points: feature.raw_path_len || path.length, path_stride: feature.path_stride || 1, detail_tier: feature.detail_tier, resolution: feature.sea_resolution },
    },
  });
  if (!element) return null;
  decorateCloudShellElement(element, {
    shellKey: [morphology.family || 'cloud', preferredBand || 'band', feature.path_detail || 'contour', Math.round(toNumber(feature.lat, 0) * 100), Math.round(toNumber(feature.lon, 0) * 100), shellIndex].join('|'),
    baseStrokeColor: element.getAttribute?.('stroke-color') || band.color,
    baseStrokeOpacity: toNumber(element.getAttribute?.('stroke-opacity'), cloudShellCapShadeOpacity({ cloudFraction, shell: shellIndex === 0 ? 'contour-sidewall' : 'contour-cap' })),
    baseStrokeWidth: toNumber(element.getAttribute?.('stroke-width'), shellIndex === 0 ? 0.7 : 0.45),
    windU: feature.wind_u,
    windV: feature.wind_v,
    anchorLat: feature.lat,
    anchorLon: feature.lon,
  });
  try {
    element.setAttribute('data-gfs-layer', 'clouds');
    element.setAttribute('data-cloud-shell', morphology.family || 'cloud');
    element.setAttribute('data-cloud-shell-source', 'fastapi-sea-contour');
    element.setAttribute('data-cloud-path-detail', feature.path_detail || 'contour');
  } catch (_) {}
  return {
    element,
    shellId: uniqueId('cloud-contour-shell'),
    basePath: path,
    anchorLat: feature.lat,
    anchorLon: feature.lon,
    windU: toNumber(feature.wind_u, 0),
    windV: toNumber(feature.wind_v, 0),
    latOffsetDeg: 0,
    lonOffsetDeg: 0,
    particleMeanLatOffsetDeg: 0,
    particleMeanLonOffsetDeg: 0,
    cloudFraction: normalizeFraction(cloudFraction, 0.6),
    topAltitude: baseAltitude + band.height,
    basePressureHpa: band.baseHpa,
    topPressureHpa: band.topHpa,
  };
}

function cloudBodiesForFeature(feature, footprintScale) {
  const low = toNumber(feature?.cloud_low, 0);
  const mid = toNumber(feature?.cloud_mid, 0);
  const high = toNumber(feature?.cloud_high, 0);
  const total = toNumber(feature?.cloud_total, Math.max(low, mid, high));
  const totalBoost = clamp(total / 100, 0.82, 1.25);
  const morphology = classifyCloudMorphology(feature);
  const blueprints = shellBlueprints(morphology);
  const layers = [];

  const appendLayer = (type, density) => {
    const band = buildCloudPressureBand(type, density, totalBoost, morphology.family);
    const weight = band.weight;
    if (weight <= 0) return;
    for (const bp of blueprints) {
      const cloudFraction = normalizeFraction(Math.max(density, total), clamp(weight, 0.25, 0.95));
      const baseAltitude = Math.round(band.baseAltitude + (band.height * (bp.baseLift || 0)));
      const height = Math.round(band.height * (1 + (bp.heightBoost || 0)));
      layers.push({
        baseAltitude,
        topAltitude: baseAltitude + height,
        height,
        opacity: cloudShellFillOpacity({
          baseOpacity: (bp.opacityBase + (weight * bp.opacitySpan)),
          cloudFraction,
          shell: bp.shell,
          family: morphology.family,
        }),
        cloudFraction,
        latRadiusDeg: footprintScale.lat * (bp.latScale + (weight * 0.42)),
        lonRadiusDeg: footprintScale.lon * (bp.lonScale + (weight * 0.48)),
        color: band.color,
        pressureBand: type,
        basePressureHpa: band.baseHpa,
        topPressureHpa: band.topHpa,
        family: morphology.family,
        subtype: morphology.subtype,
        wobble: bp.wobble,
        points: bp.points,
        elongation: bp.elongation,
        shell: bp.shell,
      });
    }
  };

  if (low >= CLOUD_PRESSURE_BANDS.low.threshold) appendLayer('low', low);
  if (mid >= CLOUD_PRESSURE_BANDS.mid.threshold) appendLayer('mid', mid);
  if (high >= CLOUD_PRESSURE_BANDS.high.threshold) appendLayer('high', high);
  if (!layers.length && total >= CLOUD_PRESSURE_BANDS.total.threshold) appendLayer('total', total);
  return layers;
}

function sampleGridBilinear(grid, bbox, lat, lon) {
  if (!Array.isArray(grid) || !Array.isArray(grid[0]) || !bbox) return null;
  const arr = Array.isArray(grid[0][0]) ? grid[0] : grid;
  const ny = arr.length;
  const nx = Array.isArray(arr[0]) ? arr[0].length : 0;
  if (!ny || !nx) return null;
  const y = clamp(((lat - bbox.south) / Math.max(1e-6, bbox.north - bbox.south)) * (ny - 1), 0, ny - 1);
  const x = clamp(((lon - bbox.west) / Math.max(1e-6, bbox.east - bbox.west)) * (nx - 1), 0, nx - 1);
  const y0 = Math.floor(y);
  const x0 = Math.floor(x);
  const y1 = Math.min(ny - 1, y0 + 1);
  const x1 = Math.min(nx - 1, x0 + 1);
  const fy = y - y0;
  const fx = x - x0;
  const q11 = toNumber(arr[y0]?.[x0], NaN);
  const q21 = toNumber(arr[y0]?.[x1], q11);
  const q12 = toNumber(arr[y1]?.[x0], q11);
  const q22 = toNumber(arr[y1]?.[x1], q21);
  if (![q11, q21, q12, q22].every(Number.isFinite)) return null;
  return (q11 * (1 - fx) * (1 - fy)) + (q21 * fx * (1 - fy)) + (q12 * (1 - fx) * fy) + (q22 * fx * fy);
}

function windHeadingDeg(u, v) {
  if (!Number.isFinite(u) || !Number.isFinite(v)) return 90;
  return ((Math.atan2(u, v) * 180 / Math.PI) + 360) % 360;
}

function makeCloudBody({ lat, lon, baseAltitude, topAltitude = null, height, latRadiusDeg, lonRadiusDeg, color, opacity, cloudFraction = null, basePressureHpa = null, topPressureHpa = null, windU = 0, windV = 0, family = 'stratiform', wobble = 0.16, points = 14, elongation = 1.0 }) {
  const headingDeg = windHeadingDeg(windU, windV);
  const basePath = roundedCloudPath({ lat, lon, latRadiusDeg, lonRadiusDeg, wobble, points, elongation: family === 'cirriform' ? Math.max(1.25, elongation) : elongation, headingDeg });
  const element = createPolygon3D({
    path: basePath,
    altitude: baseAltitude,
    altitudeMode: 'absolute',
    fillColor: color,
    fillOpacity: opacity,
    strokeColor: color,
    strokeOpacity: cloudShellCapShadeOpacity({ cloudFraction, shell: family }),
    strokeWidth: 0.45,
    extrudedHeight: cloudExtrudedHeight(height, cloudFraction),
    neonGlow: true,
    hover: {
      title: `${family} cloud shell`,
      lines: [
        `Base altitude: ${Math.round(baseAltitude)} m`,
        `Shell height: ${Math.round(height)} m`,
        `Opacity: ${Math.round(clamp(opacity, 0, 1) * 100)}%`,
        `Wind: ${Math.hypot(toNumber(windU, 0), toNumber(windV, 0)).toFixed(1)} m/s @ ${headingDeg.toFixed(0)}°`,
      ],
      metrics: { layer: 'clouds', source: 'frontend_cloud_shell', path_points: basePath?.length || 0, altitude_m: baseAltitude },
      payload: { family, base_altitude_m: baseAltitude, top_altitude_m: topAltitude || (baseAltitude + height), height_m: height, cover: opacity, cloud_fraction: cloudFraction, base_pressure_hpa: basePressureHpa, top_pressure_hpa: topPressureHpa, wind_u: windU, wind_v: windV, heading_deg: headingDeg, path_points: basePath?.length || 0 },
    },
  });
  if (!element) return null;
  try { element.setAttribute('data-gfs-layer', 'clouds'); element.setAttribute('data-cloud-shell', family); } catch (_) {}
  decorateCloudShellElement(element, {
    shellKey: [family || 'cloud', Math.round(toNumber(lat, 0) * 100), Math.round(toNumber(lon, 0) * 100), Math.round(toNumber(baseAltitude, 0))].join('|'),
    baseFillOpacity: opacity,
    baseStrokeColor: color,
    baseStrokeOpacity: cloudShellCapShadeOpacity({ cloudFraction, shell: family }),
    baseStrokeWidth: 0.45,
    windU, windV, anchorLat: lat, anchorLon: lon,
  });
  return {
    element,
    shellId: uniqueId('cloud-shell'),
    basePath,
    anchorLat: lat,
    anchorLon: lon,
    windU: toNumber(windU, 0),
    windV: toNumber(windV, 0),
    latOffsetDeg: 0,
    lonOffsetDeg: 0,
    particleMeanLatOffsetDeg: 0,
    particleMeanLonOffsetDeg: 0,
    cloudFraction: normalizeFraction(cloudFraction, 0.6),
    topAltitude: topAltitude || (baseAltitude + height),
    basePressureHpa,
    topPressureHpa,
  };
}


const cloudAdvectionCarry = [];
const cloudSceneStops = [];

function registerCloudSceneStop(stopFn) {
  if (typeof stopFn !== 'function') return stopFn;
  cloudSceneStops.push(stopFn);
  if (cloudSceneStops.length > 48) cloudSceneStops.splice(0, cloudSceneStops.length - 48);
  return stopFn;
}

function stopAllCloudAdvection(reason = 'clear_cloud_layer') {
  const stops = cloudSceneStops.splice(0);
  for (const stop of stops) {
    try { stop(); } catch (_) {}
  }
  try { window.__gfsDebugEvent?.('clouds/stop-all-advection', { reason, stopped: stops.length }); } catch (_) {}
}

function rememberCloudAdvectionCarry(items = []) {
  cloudAdvectionCarry.length = 0;
  for (const item of items) {
    if (!item) continue;
    cloudAdvectionCarry.push({
      lat: Number(item.anchorLat),
      lon: Number(item.anchorLon),
      latOffsetDeg: Number(item.latOffsetDeg) || 0,
      lonOffsetDeg: Number(item.lonOffsetDeg) || 0,
      t: Date.now(),
    });
  }
  if (cloudAdvectionCarry.length > 300) cloudAdvectionCarry.splice(300);
}

function nearestCloudCarry(lat, lon) {
  let best = null;
  let bestD = Infinity;
  for (const c of cloudAdvectionCarry) {
    if (!Number.isFinite(c.lat) || !Number.isFinite(c.lon)) continue;
    const d = Math.hypot((Number(lat) || 0) - c.lat, (Number(lon) || 0) - c.lon);
    if (d < bestD) { best = c; bestD = d; }
  }
  if (!best || bestD > 1.6 || Date.now() - best.t > 180000) return null;
  return best;
}

function applyCloudCarry(body, particles = []) {
  const carry = nearestCloudCarry(body?.anchorLat, body?.anchorLon);
  if (!carry) return;
  body.latOffsetDeg = carry.latOffsetDeg;
  body.lonOffsetDeg = carry.lonOffsetDeg;
  try { if (body.element) body.element.path = advectPath(body.basePath, body.latOffsetDeg, body.lonOffsetDeg); } catch (_) {}
  for (const particle of particles) {
    particle.latOffsetDeg = carry.latOffsetDeg;
    particle.lonOffsetDeg = carry.lonOffsetDeg;
  }
}

function startCloudAdvection(items, particles = []) {
  if (!items.length && !particles.length) return () => {};
  let rafId = 0;
  let stopped = false;
  let lastTs = 0;
  let lastDrawTs = 0;
  let elapsed = 0;
  const itemsByShell = new Map();
  for (const item of items) {
    if (item?.shellId) itemsByShell.set(item.shellId, item);
  }

  const tick = (ts) => {
    if (stopped) return;
    if (!lastTs) lastTs = ts;
    if (lastDrawTs && (ts - lastDrawTs) < CLOUD_ADVECTION_INTERVAL_MS) {
      rafId = requestAnimationFrame(tick);
      return;
    }
    const dtSec = Math.min(MAX_ADVECTION_STEP_SEC, Math.max(0.01, (ts - lastTs) * 0.001));
    lastTs = ts;
    lastDrawTs = ts;
    elapsed += dtSec;

    // Hybrid ellipse clusters are the advected mass. Each marker is one soft
    // ellipse cluster containing many visual micro-specks, so the shell feels
    // filled while the browser only moves 10-20 objects per cloud body.
    const means = new Map();
    for (const particle of particles) {
      if (!particle.marker) continue;
      particle.latOffsetDeg += metersToLatDegrees((particle.windV + particle.localV) * dtSec);
      particle.lonOffsetDeg += metersToLonDegrees((particle.windU + particle.localU) * dtSec, particle.anchorLat + particle.latOffsetDeg);
      const phase = particle.wobblePhase + elapsed * particle.wobbleRate;
      const wobbleLat = Math.sin(phase) * particle.wobbleLatAmp;
      const wobbleLon = Math.cos(phase * 0.83) * particle.wobbleLonAmp;
      const wobbleAlt = Math.sin(phase * 1.31) * Math.min(220, particle.shellHeight * 0.055);
      const lat = particle.anchorLat + particle.baseLocalLat + particle.latOffsetDeg + wobbleLat;
      const lng = wrapLongitude(particle.anchorLon + particle.baseLocalLon + particle.lonOffsetDeg + wobbleLon);
      particle.marker.position = {
        lat,
        lng,
        altitude: Math.max(80, Math.round(particle.baseAltitude + wobbleAlt)),
      };
      const shellId = particle.shellId || null;
      if (shellId) {
        const acc = means.get(shellId) || { lat: 0, lon: 0, count: 0 };
        acc.lat += particle.latOffsetDeg;
        acc.lon += particle.lonOffsetDeg;
        acc.count += 1;
        means.set(shellId, acc);
      }
    }

    // Shell path updates are intentionally slow. Rewriting Google 3D polygon paths
    // at animation-frame speed causes the visible cloud flashing Jay reported.
    const shouldUpdateShellPaths = CLOUD_SHELL_PATH_UPDATE_MS > 0 && (!tick.lastShellPathUpdateTs || (ts - tick.lastShellPathUpdateTs) >= CLOUD_SHELL_PATH_UPDATE_MS);
    for (const item of items) {
      const mean = item.shellId ? means.get(item.shellId) : null;
      if (mean && mean.count > 0) {
        item.particleMeanLatOffsetDeg = mean.lat / mean.count;
        item.particleMeanLonOffsetDeg = mean.lon / mean.count;
        item.latOffsetDeg = item.particleMeanLatOffsetDeg;
        item.lonOffsetDeg = item.particleMeanLonOffsetDeg;
      } else {
        item.latOffsetDeg += metersToLatDegrees(item.windV * dtSec);
        item.lonOffsetDeg += metersToLonDegrees(item.windU * dtSec, item.anchorLat + item.latOffsetDeg);
      }
      if (shouldUpdateShellPaths && item.element) {
        try { item.element.path = advectPath(item.basePath, item.latOffsetDeg, item.lonOffsetDeg); } catch (_) {}
      }
    }
    if (shouldUpdateShellPaths) tick.lastShellPathUpdateTs = ts;
    rafId = requestAnimationFrame(tick);
  };

  rafId = requestAnimationFrame(tick);
  return () => {
    rememberCloudAdvectionCarry(items);
    stopped = true;
    if (rafId) cancelAnimationFrame(rafId);
  };
}

export function estimateCloudColumnAltitudes(cloudTotal = 0, layerMix = {}) {
  const low = toNumber(layerMix.low, 0);
  const mid = toNumber(layerMix.mid, 0);
  const high = toNumber(layerMix.high, 0);
  const total = Math.max(cloudTotal, low, mid, high);
  const dominant = high >= Math.max(mid, low) && high >= 16
    ? 'high'
    : (mid >= Math.max(low, high) && mid >= 18 ? 'mid' : (low >= 20 ? 'low' : 'total'));
  const morphology = classifyCloudMorphology({ cloud_low: low, cloud_mid: mid, cloud_high: high, cloud_total: total });
  const band = buildCloudPressureBand(dominant, total, clamp(total / 100, 0.82, 1.24), morphology.family);
  return {
    cloudBaseAltitude: band.baseAltitude,
    cloudTopAltitude: band.baseAltitude + band.height,
    dominantBand: dominant,
    family: morphology.family,
    subtype: morphology.subtype,
  };
}



function fadeRemoveCloudElements(elements, stopAdvection = null, durationMs = 5200) {
  const list = Array.isArray(elements) ? elements.filter(Boolean) : [];
  const duration = Math.max(1200, Number(durationMs) || 5200);
  const started = Date.now();
  for (const el of list) {
    try {
      el.setAttribute?.('data-gfs-fading-out', 'true');
      el.style && (el.style.pointerEvents = 'none');
    } catch (_) {}
  }
  const tick = () => {
    const t = Math.min(1, (Date.now() - started) / duration);
    const keep = 1 - t;
    for (const el of list) {
      try {
        const isParticle = el.getAttribute?.('data-cloud-particle') != null || el.matches?.('[data-cloud-particle]');
        if (isParticle && el.style) {
          el.style.opacity = String(clamp(keep * cloudShellOpacityScale(), 0, 1));
        } else if (el.getAttribute?.('data-cloud-shell') != null || el.getAttribute?.('data-gfs-layer') === 'clouds') {
          applyCloudShellOpacity(el, keep);
        } else if (el.style) {
          el.style.opacity = String(Math.max(0, keep));
        }
      } catch (_) {}
    }
    if (t < 1) {
      try { requestAnimationFrame(tick); } catch (_) { setTimeout(tick, 80); }
      return;
    }
    try { if (typeof stopAdvection === 'function') stopAdvection(); } catch (_) {}
    for (const el of list) {
      try { el.remove(); } catch (_) {}
    }
  };
  try { requestAnimationFrame(tick); } catch (_) { setTimeout(tick, 80); }
}

function fadeInCloudElements(elements, durationMs = 3600) {
  const list = Array.isArray(elements) ? elements.filter(Boolean) : [];
  const duration = Math.max(900, Number(durationMs) || 3600);
  const started = Date.now();
  for (const el of list) {
    try {
      el.setAttribute?.('data-gfs-fading-in', 'true');
      const isParticle = el.getAttribute?.('data-cloud-particle') != null || el.matches?.('[data-cloud-particle]');
      if (isParticle && el.style) el.style.opacity = '0';
      else if (el.getAttribute?.('data-cloud-shell') != null || el.getAttribute?.('data-gfs-layer') === 'clouds') applyCloudShellOpacity(el, 0.001);
      else if (el.style) el.style.opacity = '0';
    } catch (_) {}
  }
  const tick = () => {
    const t = Math.min(1, (Date.now() - started) / duration);
    // Smoothstep avoids the hard flash at the beginning of a cache/TTL swap.
    const show = t * t * (3 - (2 * t));
    for (const el of list) {
      try {
        const isParticle = el.getAttribute?.('data-cloud-particle') != null || el.matches?.('[data-cloud-particle]');
        if (isParticle && el.style) {
          el.style.opacity = String(clamp(show * cloudShellOpacityScale(), 0, 1));
        } else if (el.getAttribute?.('data-cloud-shell') != null || el.getAttribute?.('data-gfs-layer') === 'clouds') {
          applyCloudShellOpacity(el, show);
        } else if (el.style) {
          el.style.opacity = String(show);
        }
      } catch (_) {}
    }
    if (t < 1) {
      try { requestAnimationFrame(tick); } catch (_) { setTimeout(tick, 80); }
      return;
    }
    for (const el of list) {
      try { el.removeAttribute?.('data-gfs-fading-in'); } catch (_) {}
    }
  };
  try { requestAnimationFrame(tick); } catch (_) { setTimeout(tick, 80); }
}

function preserveCloudDisposer(reason = 'preserve_existing_clouds') {
  const disposer = () => {};
  disposer.__gfsPreservePrevious = true;
  disposer.__gfsReason = reason;
  return disposer;
}

function renderedDisposer(fn, { keepExisting = true } = {}) {
  const disposer = function disposeRenderedCloudLayer() {
    try { fn?.(); } catch (_) {}
  };
  disposer.__gfsDidRender = true;
  // Clouds are an accumulating/morphing layer. New shells may overlap old shells;
  // RendererLayer must not fire the previous disposer on every TTL/cache update,
  // because that fade-out/fade-in swap is the visible flashing Jay reported.
  disposer.__gfsKeepExisting = keepExisting === true;
  return disposer;
}


function stableCloudPayloadRenderSignature(payload) {
  try {
    const bbox = bboxFromPayload(payload) || {};
    const tileFeatures = cloudTileFeatures(payload, bbox);
    const seaFeatures = seaContourCloudFeatures(payload);
    const features = tileFeatures.length ? tileFeatures : (seaFeatures.length ? seaFeatures : (Array.isArray(payload?.features) ? payload.features : (Array.isArray(payload?.items) ? payload.items : [])));
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
    const b = [bbox.west, bbox.south, bbox.east, bbox.north].map((v) => Number(v || 0).toFixed(1)).join(',');
    return [b, count, payload?.cloud_region_count || payload?.sea_feature_count || '', first, mid, last].join('|');
  } catch (_) { return String(payload?.version || payload?.cache?.version || 'clouds'); }
}

function renderCloudZonesImpl({ payload, map3DElement }) {
  const created = [];
  const advected = [];
  const particles = [];
  const rb = payload?.render_budget || payload?.scene_plan?.render_budget || {};
  const mobile = /android|iphone|ipad|mobile/i.test(navigator.userAgent || '');
  const viewportTier = String(payload?.sea_resolution?.tier || payload?.scene_tier || payload?.scene_plan?.tier || '').toLowerCase();
  const requestedCloudBodies = Number(rb.max_cloud_shells || MAX_CLOUD_BODIES);
  const maxCloudBodies = Math.max(1, Math.round(requestedCloudBodies || MAX_CLOUD_BODIES));
  if (!map3DElement || !payload) return () => {};
  const replacingExistingClouds = Boolean(map3DElement?.querySelector?.('[data-cloud-shell],[data-cloud-particle]'));
  const stableSig = stableCloudPayloadRenderSignature(payload);
  const lastRender = (typeof window !== 'undefined' && window.__gfsCloudStableRender) || null;
  if (replacingExistingClouds && lastRender && lastRender.signature === stableSig && (Date.now() - Number(lastRender.ts || 0)) < CLOUD_RENDER_MIN_REUSE_MS) {
    console.debug('[GFS] render/noop-same-layer-version', { layer: 'clouds', signature: stableSig, age_ms: Date.now() - Number(lastRender.ts || 0) });
    try { window.__gfsDebugEvent?.('clouds/reuse-existing-scene', { signature: stableSig, age_ms: Date.now() - Number(lastRender.ts || 0), policy: 'advect_sprites_no_full_redraw' }); } catch (_) {}
    return preserveCloudDisposer('same_stable_cloud_scene_advect_only');
  }
  const requestedCloudParticles = Number(rb.max_cloud_particles || MAX_CLOUD_PARTICLES);
  const deviceCap = mobile ? MOBILE_MAX_CLOUD_PARTICLES : DESKTOP_MAX_CLOUD_PARTICLES;
  const maxCloudParticles = cloudParticleGovernorCap({ requested: requestedCloudParticles, deviceCap, mobile, viewportTier, replacingExisting: replacingExistingClouds });
  const particleBudget = { remaining: maxCloudParticles };
  if (typeof window !== 'undefined' && window.__gfsCloudsDisabled === true) {
    clearCloudLayerNodes(map3DElement);
    return () => {};
  }
  console.info('[gfs clouds] polygon api', { api: polygonApiPath(), advectionMs: CLOUD_ADVECTION_INTERVAL_MS, shellPathUpdateMs: CLOUD_SHELL_PATH_UPDATE_MS, opacityScale: cloudShellOpacityScale() });

  const state = cloudPayloadState(payload);
  const hasDrawable = cloudPayloadHasDrawableContent(payload);
  if (payload?.heuristic === true || state === 'synthetic') {
    console.warn('[gfs clouds] synthetic/mock payload refused; holding existing clouds if any', { source: payload?.source, state });
    return preserveCloudDisposer('synthetic_mock_payload');
  }
  if ((state === 'warming' || state === 'pending' || state === 'queued') && !hasDrawable) {
    console.info('[gfs clouds] warming shell received; preserving existing cloud shells until live payload arrives', { source: payload?.source, state });
    return preserveCloudDisposer('warming_empty_payload');
  }
  const bbox = bboxFromPayload(payload);
  if (!bbox) {
    console.warn('[gfs clouds] no bbox in real cloud payload', { source: payload?.source, state });
    return preserveCloudDisposer('missing_bbox');
  }

  if (!hasDrawable) {
    console.info('[gfs clouds] no drawable live cloud content; preserving existing clouds', { source: payload?.source, state });
    return preserveCloudDisposer('no_drawable_payload');
  }

  // Prefer viewport-bounded cloud tiles/scene clouds. The polygon_field_v1 path is
  // kept only as a fallback because bad lat/lon arrays can place clouds at poles.
  const tileFeatures = cloudTileFeatures(payload, bbox);
  const allowPolygonCloudFallback = payload?.allow_polygon_cloud_fallback === true || window.__GFS_DEBUG_POLYGON_CLOUDS === true;
  // Sea-engine/derived contour payloads are already viewport-bounded and are the
  // intended cloud geometry. Do not gate them behind the old polygon fallback flag.
  // Only the raw polygon_field_v1 fallback stays opt-in because it can contain bad axes.
  const seaContourFeatures = seaContourCloudFeatures(payload);
  const preferSeaContours = payload?.prefer_sea_contours === true || payload?.prefer_derived_contours === true || payload?.derived_render_geometry === true || seaContourFeatures.length > 0;
  const contractFeatures = ((!tileFeatures.length || preferSeaContours) && !seaContourFeatures.length && allowPolygonCloudFallback) ? cloudFeaturesFromContract(payload, bbox) : [];
  const featureSource = (!preferSeaContours && tileFeatures.length) ? tileFeatures : contractFeatures;

  if ((preferSeaContours || !tileFeatures.length) && seaContourFeatures.length) {
    const frag = document.createDocumentFragment();
    let bodyCount = 0;
    const cap = maxCloudBodies;
    for (let i = 0; i < seaContourFeatures.length && bodyCount < cap; i += 1) {
      const feature = seaContourFeatures[i];
      const shellCopies = 1;
      for (let shell = 0; shell < shellCopies && bodyCount < cap; shell += 1) {
        const body = makeSeaContourCloudBody(feature, shell);
        if (!body) continue;
        frag.append(body.element);
        created.push(body.element);
        advected.push(body);
        const seaBand = buildCloudPressureBand((toNumber(feature?.cloud_high, 0) >= Math.max(toNumber(feature?.cloud_low, 0), toNumber(feature?.cloud_mid, 0))) ? 'high' : (toNumber(feature?.cloud_mid, 0) >= toNumber(feature?.cloud_low, 0) ? 'mid' : 'low'), Math.max(toNumber(feature?.cloud_total, 60), toNumber(feature?.level, 0.5) * 100), 1.0, 'stratiform');
        const particleLayer = {
          latRadiusDeg: Math.max(0.025, toNumber(feature.cell_lat_deg, 0.08)),
          lonRadiusDeg: Math.max(0.025, toNumber(feature.cell_lon_deg, 0.08)),
          height: seaBand.height,
          baseAltitude: body.baseAltitude || seaBand.baseAltitude,
          topAltitude: body.topAltitude || seaBand.topAltitude,
          basePressureHpa: body.basePressureHpa || seaBand.baseHpa,
          topPressureHpa: body.topPressureHpa || seaBand.topHpa,
          opacity: 0.08 + toNumber(feature.level, 0.5) * 0.07,
          cloudFraction: normalizeFraction(Math.max(toNumber(feature?.cloud_total, 0), toNumber(feature?.level, 0.5) * 100), 0.6),
          family: 'stratiform',
          color: '#eef6ff',
        };
        const bodyParticles = buildCloudParticlesForBody(body, particleLayer, particleBudget);
        applyCloudCarry(body, bodyParticles);
        for (const particle of bodyParticles) {
          particle.marker = createCloudParticleMarker(particle);
          frag.append(particle.marker);
          created.push(particle.marker);
          particles.push(particle);
        }
        bodyCount += 1;
      }
    }
    if (bodyCount > 0) {
      // Append the next cloud scene before old disposer removes the prior one; avoids visible update jumps/blanks.
      map3DElement.append(frag);
      fadeInCloudElements(created, replacingExistingClouds ? 5200 : 1600);
      const stopAdvection = registerCloudSceneStop(startCloudAdvection(advected, particles));
      try { window.__gfsDebugEvent?.('clouds/render', { source: 'fastapi_sea_contour_footprints', bodies: bodyCount, particles: particles.length, maxCloudParticles,
    cloudParticleMode: cloudParticleDemandMode(), maxCloudBodies, contours: seaContourFeatures.length, renderedPathPoints: advected.reduce((n, b) => n + (Array.isArray(b.basePath) ? b.basePath.length : 0), 0), tier: payload?.sea_resolution?.tier || viewportTier }); } catch (_) {}
      console.info('[gfs clouds] rendered FastAPI contour-footprint shells', {
        bodies: bodyCount,
        particles: particles.length,
        particleCap: maxCloudParticles,
        bodyCap: maxCloudBodies,
        contours: seaContourFeatures.length,
        tier: payload?.sea_resolution?.tier,
        subgrid: payload?.sea_resolution?.subgrid_used,
        detail: seaContourFeatures[0]?.path_detail || 'contour_path',
        pathStride: seaContourFeatures[0]?.path_stride || 1,
        source: 'fastapi_sea_contour_footprints_visible_preferred',
      });
      console.debug('[GFS] clouds/morph-existing-bodies', { version: stableSig, bodies: created.filter((el) => el?.hasAttribute?.('data-cloud-shell')).length, sprites: particles.length, spriteCap: maxCloudParticles });
      try { window.__gfsCloudStableRender = { signature: stableSig, ts: Date.now() }; } catch (_) {}
      return renderedDisposer(() => {
        fadeRemoveCloudElements(created, stopAdvection, 6000);
      });
    }
  }

  if (featureSource.length) {
    const frag = document.createDocumentFragment();
    let bodyCount = 0;
    for (let i = 0; i < featureSource.length; i += 1) {
      const feature = featureSource[i]?.properties || featureSource[i] || {};
      const normalizedCenter = normalizeFeatureCenter(feature, bbox);
      if (!normalizedCenter) continue;
      const { lat, lon } = normalizedCenter;
      const serverLayers = serverCloudShellLayersFromTile(feature.rawTile, bbox, feature);
      const footprint = { lat: Math.max(toNumber(feature.cell_lat_deg, 0.12) * 0.94, 0.05), lon: Math.max(toNumber(feature.cell_lon_deg, 0.12) * 0.94, 0.05) };
      const layers = serverLayers.length ? serverLayers : cloudBodiesForFeature(feature, footprint);
      for (const layer of layers) {
        const body = serverLayers.length
          ? makeServerCloudShellBody(layer)
          : makeCloudBody({ lat, lon, windU: feature.wind_u, windV: feature.wind_v, ...layer });
        if (!body) continue;
        frag.append(body.element);
        created.push(body.element);
        advected.push(body);
        const bodyParticles = buildCloudParticlesForBody(body, layer, particleBudget);
        applyCloudCarry(body, bodyParticles);
        for (const particle of bodyParticles) {
          particle.marker = createCloudParticleMarker(particle);
          frag.append(particle.marker);
          created.push(particle.marker);
          particles.push(particle);
        }
        bodyCount += 1;
        if (bodyCount >= maxCloudBodies) break;
      }
      if (bodyCount >= maxCloudBodies) break;
    }
    if (bodyCount > 0) {
      // Replace only after the new scene is already built in memory. This
      // avoids a blank frame while cloud shells are being constructed.
      // Append the next cloud scene before old disposer removes the prior one; avoids visible update jumps/blanks.
      map3DElement.append(frag);
      fadeInCloudElements(created, replacingExistingClouds ? 5200 : 1600);
      const stopAdvection = registerCloudSceneStop(startCloudAdvection(advected, particles));
      try { window.__gfsDebugEvent?.('clouds/render', { source: tileFeatures.length ? 'backend_cloud_shells_from_tiles' : 'polygon_field_v1', bodies: bodyCount, particles: particles.length, maxCloudParticles, maxCloudBodies, cloudParticleMode: cloudParticleDemandMode(), particleGovernor: 'balanced_default_high_body_count_lower_particle_pressure', sourceFeatures: featureSource.length, renderedPathPoints: advected.reduce((n, b) => n + (Array.isArray(b.basePath) ? b.basePath.length : 0), 0), tier: 'stable_500_cloud_body_budget' }); } catch (_) {}
      console.info('[gfs clouds] rendered bodies', { bodies: bodyCount, particles: particles.length, particleCap: maxCloudParticles, bodyCap: maxCloudBodies, cloudParticleMode: cloudParticleDemandMode(), particleGovernor: 'balanced_default_high_body_count_lower_particle_pressure', sceneTier: 'stable_500_cloud_body_budget', renderBudget: rb, source: tileFeatures.length ? (payload?.cloud_region_count ? 'backend_cloud_regions_marching_squares' : 'backend_cloud_shells_from_tiles') : 'polygon_field_v1', mode: payload?.cloud_region_count ? 'cloud_region_marching_squares_shells_particles_advect_shell_follows_centroid' : 'organic_backend_shells_particles_advect_shell_follows_centroid' });
      try { window.__gfsCloudStableRender = { signature: stableSig, ts: Date.now() }; } catch (_) {}
      return renderedDisposer(() => {
        fadeRemoveCloudElements(created, stopAdvection, 6000);
      });
    }
    console.warn('[gfs clouds] feature payload had entries but made zero bodies; falling back to grid renderer', { featureCount: featureSource.length, seaContourFeatures: seaContourFeatures.length, source: payload?.source, state });
  }

  const low = to2DGrid(payload?.cloud_layers?.find((l) => l?.name === 'low')?.density);
  const mid = to2DGrid(payload?.cloud_layers?.find((l) => l?.name === 'mid')?.density);
  const high = to2DGrid(payload?.cloud_layers?.find((l) => l?.name === 'high')?.density);
  const total = to2DGrid(payload?.fields?.cloud_total || payload?.cloud_cover);
  const precip = to2DGrid(payload?.fields?.precip_rate || payload?.fields?.prate);
  const windUGrid = to2DGrid(payload?.fields?.wind_u);
  const windVGrid = to2DGrid(payload?.fields?.wind_v);
  const grid = low.length ? low : (mid.length ? mid : (high.length ? high : total));
  if (!grid.length) {
    console.info('[gfs clouds] no drawable real cloud grid/features yet', { source: payload?.source, state });
    return preserveCloudDisposer('no_grid_features');
  }

  const ny = grid.length;
  const nx = Array.isArray(grid[0]) ? grid[0].length : 0;
  if (!ny || !nx) return preserveCloudDisposer('empty_grid_dimensions');

  const step = Math.max(1, Math.floor(Math.max(nx, ny) / 26));
  const cell = cellSizeDeg(bbox, ny, nx);
  const frag = document.createDocumentFragment();
  let bodyCount = 0;
  let cellCount = 0;

  for (let i = 0; i < ny; i += step) {
    for (let j = 0; j < nx; j += step) {
      const lowVal = toNumber(low?.[i]?.[j], 0);
      const midVal = toNumber(mid?.[i]?.[j], 0);
      const highVal = toNumber(high?.[i]?.[j], 0);
      const precipVal = toNumber(precip?.[i]?.[j], 0);
      const totalVal = toNumber(total?.[i]?.[j], Math.max(lowVal, midVal, highVal));
      if (totalVal < 22 && precipVal < 0.06 && lowVal < 18 && midVal < 16 && highVal < 14) continue;
      const { lat, lon } = latLonFromIndex(i, j, ny, nx, bbox);
      const layers = cloudBodiesForFeature({ lat, lon, cloud_low: lowVal, cloud_mid: midVal, cloud_high: highVal, cloud_total: totalVal, precip_rate: precipVal }, {
        lat: Math.max(cell.lat * 0.92, 0.05),
        lon: Math.max(cell.lon * 0.92, 0.05),
      });
      const sampledWindU = sampleGridBilinear(windUGrid, bbox, lat, lon);
      const sampledWindV = sampleGridBilinear(windVGrid, bbox, lat, lon);
      for (const layer of layers) {
        const body = makeCloudBody({ lat, lon, windU: sampledWindU, windV: sampledWindV, ...layer });
        if (!body) continue;
        frag.append(body.element);
        created.push(body.element);
        advected.push(body);
        const bodyParticles = buildCloudParticlesForBody(body, layer, particleBudget);
        applyCloudCarry(body, bodyParticles);
        for (const particle of bodyParticles) {
          particle.marker = createCloudParticleMarker(particle);
          frag.append(particle.marker);
          created.push(particle.marker);
          particles.push(particle);
        }
        bodyCount += 1;
        if (bodyCount >= maxCloudBodies) break;
      }
      cellCount += 1;
      if (bodyCount >= maxCloudBodies) break;
    }
    if (bodyCount >= maxCloudBodies) break;
  }

  // Append first; RendererLayer disposes the previous scene after this returns.
  map3DElement.append(frag);
  fadeInCloudElements(created, replacingExistingClouds ? 5200 : 1600);
  const stopAdvection = registerCloudSceneStop(startCloudAdvection(advected, particles));
  try { window.__gfsDebugEvent?.('clouds/render', { source: 'fields_grid', cells: cellCount, bodies: bodyCount, particles: particles.length, maxCloudParticles, maxCloudBodies, cloudParticleMode: cloudParticleDemandMode(), particleGovernor: 'balanced_default_high_body_count_lower_particle_pressure', renderedPathPoints: advected.reduce((n, b) => n + (Array.isArray(b.basePath) ? b.basePath.length : 0), 0), tier: 'stable_500_cloud_body_budget' }); } catch (_) {}
  console.info('[gfs clouds] rendered bodies', { cells: cellCount, bodies: bodyCount, particles: particles.length, particleCap: maxCloudParticles, bodyCap: maxCloudBodies, cloudParticleMode: cloudParticleDemandMode(), particleGovernor: 'balanced_default_high_body_count_lower_particle_pressure', sceneTier: 'stable_500_cloud_body_budget', renderBudget: rb, mode: 'shells_with_internal_ellipsoid_particles_uv_advected' });

  console.debug('[GFS] clouds/morph-existing-bodies', { version: stableSig, bodies: bodyCount, sprites: particles.length, spriteCap: maxCloudParticles });
  try { window.__gfsCloudStableRender = { signature: stableSig, ts: Date.now() }; } catch (_) {}
  return renderedDisposer(() => {
    fadeRemoveCloudElements(created, stopAdvection, 6000);
  });
}


// Public wrapper: never let one malformed cloud frame kill the whole renderer.


export function renderCloudZones(args = {}) {
  try {
    return renderCloudZonesImpl(args);
  } catch (err) {
    try {
      const payload = args?.payload || {};
      const msg = String(err?.message || err || 'unknown_cloud_render_error');
      console.error('[gfs clouds] render error guarded', { message: msg, stack: err?.stack, source: payload?.source, status: payload?.status, keys: Object.keys(payload || {}).slice(0, 30) });
      window.__gfsDebugEvent?.('clouds/render-error', { message: msg, source: payload?.source, status: payload?.status });
    } catch (_) {}
    // Do not mark this as rendered. RendererLayer will preserve the previous
    // cloud scene and will retry the next drawable payload instead of clearing
    // clouds because a single frame was malformed.
    return preserveCloudDisposer('render_error_guard');
  }
}
