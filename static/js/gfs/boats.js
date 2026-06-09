import { getJsonSafe } from './api.js';

// Hard rule: keep boater layer light and predictable. Default to 10 boats.
const BOAT_COUNT_MAX = Number(window.GFS_BOAT_COUNT_MAX || 10);
const MODEL_SRC = String(window.GFS_BOAT_MODEL_SRC || '').trim();
// Clean model transform: keep the GLB down on the water, at a stable size.
// The old dynamic model scale could blow the 26 ft GLB up to huge billboard size.
const BOAT_WATER_ALTITUDE_M = Number(window.GFS_BOAT_WATER_ALTITUDE_M ?? 0.25);
const BOAT_GLYPH_ALTITUDE_M = Number(window.GFS_BOAT_GLYPH_ALTITUDE_M ?? 7.5);
const BOAT_UNDERGLOW_ALTITUDE_M = Number(window.GFS_BOAT_UNDERGLOW_ALTITUDE_M ?? 1.2);
const MODEL_SCALE = Number(window.GFS_BOAT_MODEL_SCALE ?? 1.0);
const MODEL_SCALE_MAX = Number(window.GFS_BOAT_MODEL_SCALE_MAX ?? MODEL_SCALE);
const MODEL_SCALE_GROWTH = Number(window.GFS_BOAT_MODEL_SCALE_GROWTH ?? 0.0);
const VIEWPORT_BOAT_COUNT = Number(window.GFS_VIEWPORT_BOAT_COUNT || 10);
// Tune only this when the GLB export forward axis is off. Common test values: 0, 90, 180, 270.
const MODEL_YAW_OFFSET_DEG = Number(window.GFS_BOAT_MODEL_YAW_OFFSET_DEG ?? 0);
// Boat GLB axis correction. The 26 ft boat model is treated as a normal Z-up GLB:
// bottom flat to the local tangent surface, heading only rotates the bow.
// If a future GLB export uses a different forward/up axis, tune these live in the
// browser console with window.GFS_BOAT_MODEL_TILT_DEG / ROLL_DEG / YAW_OFFSET_DEG.
const MODEL_TILT_DEG = Number(window.GFS_BOAT_MODEL_TILT_DEG ?? 0);
const MODEL_ROLL_DEG = Number(window.GFS_BOAT_MODEL_ROLL_DEG ?? 0);
const LON_SCALE = 0.0;
const LAT_SCALE = 0.0;
const MAX_VISUAL_DRIFT_DEG = 0.0;
const MARKER_MIN_SCALE = 0.85;
const MARKER_MAX_SCALE = 2.8;
const RANGE_NEAR_M = 450000;
const RANGE_FAR_M = 3500000;

function tagLayer(el) {
  try { el?.setAttribute?.('data-gfs-layer', 'boater'); } catch (_) {}
  return el;
}

function tagBoatVisual(el, kind) {
  tagLayer(el);
  try { el?.setAttribute?.('data-gfs-sub-layer', kind || 'boat-visual'); } catch (_) {}
  return el;
}

function finiteNumber(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function finiteLatLon(point) {
  const lat = finiteNumber(point?.lat);
  const lon = finiteNumber(point?.lon ?? point?.lng);
  if (lat == null || lon == null) return null;
  if (lat < -90 || lat > 90 || lon < -180 || lon > 180) return null;
  return { lat, lon };
}

function setMarkerPositionObject(el, lat, lon, altitude) {
  const la = Number(lat);
  const ln = Number(lon);
  const alt = Number(altitude) || 0;
  if (!Number.isFinite(la) || !Number.isFinite(ln)) return;
  const pos = { lat: la, lng: ln, altitude: alt };
  try { el.position = pos; } catch (_) {}
  try { el.setAttribute('position', `${la},${ln},${alt}`); } catch (_) {}
}

function setInlineSvgTemplate(el, svg) {
  // Google Maps 3D markers are picky: slot a direct <template><svg>...</svg></template>.
  // A template-wrapped <img data-url> can silently fail on some API builds, which
  // made boats appear as only current/boating squares.
  el.textContent = '';
  const tpl = document.createElement('template');
  tpl.innerHTML = svg.trim();
  el.append(tpl);
}

function maps3d() {
  return window.google?.maps?.maps3d || null;
}

function currentRangeMeters() {
  const globe = document.getElementById('globe');
  const raw = Number(globe?.range ?? globe?.getAttribute('range'));
  return Number.isFinite(raw) && raw > 0 ? raw : 1800000;
}

function zoomOutFactorForRange(rangeM) {
  // Map3DElement exposes camera distance as `range`, not a normal 2D zoom.
  // Near/close range keeps the GLB near real 26 ft scale; far/wide range
  // enlarges it as a readable symbol so the boat does not disappear.
  return Math.max(0, Math.min(1, (Number(rangeM || RANGE_FAR_M) - RANGE_NEAR_M) / (RANGE_FAR_M - RANGE_NEAR_M)));
}

function visibleScaleForRange(rangeM) {
  const t = zoomOutFactorForRange(rangeM);
  return MARKER_MIN_SCALE + ((MARKER_MAX_SCALE - MARKER_MIN_SCALE) * t);
}

function modelScaleForRange(rangeM) {
  const t = zoomOutFactorForRange(rangeM);
  const growth = Math.max(0, Number(MODEL_SCALE_GROWTH) || 0);
  const scale = MODEL_SCALE * (1 + (growth * t));
  const hi = Math.max(MODEL_SCALE, MODEL_SCALE_MAX);
  const lo = Math.min(MODEL_SCALE, hi);
  return Math.max(lo, Math.min(hi, scale));
}

function dominantWaveHeightFt(waves = {}) {
  const values = [
    waves.sigHeightFt, waves.heightFt, waves.waveHeightFt,
    waves.primary?.heightFt, waves.secondary?.heightFt, waves.tertiary?.heightFt,
  ].map(Number).filter(Number.isFinite);
  return values.length ? Math.max(...values) : null;
}

function boatingBandForWaves(waves = {}) {
  const ft = dominantWaveHeightFt(waves);
  if (ft == null) return { color: 'green', label: 'Wave height unavailable; showing calm default', waveHeightFt: null };
  if (ft > 4) return { color: 'red', label: '4+ ft seas: hazardous boating zone', waveHeightFt: ft };
  if (ft > 3) return { color: 'yellow', label: '3–4 ft seas: caution boating zone', waveHeightFt: ft };
  return { color: 'green', label: '0–3 ft seas: favorable boating zone', waveHeightFt: ft };
}

function colorForSafety(color, speedKt = 0, waves = null) {
  // Boating layer color is intentionally keyed to sea-state thresholds:
  // 0–3 ft green, >3–4 ft yellow, >4 ft red. Current speed can still be
  // displayed in hover/HUD, but it should not repaint the boating zone by itself.
  if (waves) {
    const band = boatingBandForWaves(waves);
    if (band.color === 'red') return '#ff5f57';
    if (band.color === 'yellow') return '#ffd866';
    return '#57d46f';
  }
  const c = String(color || '').toLowerCase();
  if (c === 'red') return '#ff5f57';
  if (c === 'yellow' || c === 'orange') return '#ffd866';
  return '#57d46f';
}

function knotsToMph(knots) {
  const kt = Number(knots) || 0;
  return kt * 1.15077945;
}

function withCurrentHeading(boat) {
  // Current dirDeg is treated as the direction the current vector is moving toward.
  // Point the bow with the current, not 180° against it.
  const dir = Number(boat?.current?.dirDeg ?? boat?.current_dir_deg ?? boat?.headingDeg ?? boat?.heading ?? 0);
  return normalizeDeg(dir);
}

function modelOrientation(headingDeg) {
  const heading = normalizeDeg((Number(headingDeg) || 0) + MODEL_YAW_OFFSET_DEG);
  // Keep hull bottom flat to the Maps 3D local water surface. Heading is the only
  // moving axis; tilt/roll default to zero for a normal boat GLB.
  return { heading, tilt: MODEL_TILT_DEG, roll: MODEL_ROLL_DEG };
}

function modelPosition(boat, altitude = BOAT_WATER_ALTITUDE_M) {
  return { lat: Number(boat.lat), lng: Number(boat.lon), altitude: Number(altitude) || 0 };
}

function tagBoatElement(el) {
  try { el?.setAttribute?.('data-gfs-layer', 'boater'); } catch (_) {}
  try { el?.setAttribute?.('data-gfs-sub-layer', 'boater'); } catch (_) {}
  try { el?.classList?.add?.('gfs-boater-node'); } catch (_) {}
  return el;
}

function setHudContent(html) {
  const hud = document.getElementById('hudHoverWeather');
  if (hud) hud.innerHTML = html;
}

function formatSwell(label, swell) {
  if (!swell || (swell.heightFt == null && swell.periodS == null && swell.dirDeg == null)) return '';
  const bits = [];
  if (swell.heightFt != null) bits.push(`${swell.heightFt} ft`);
  if (swell.periodS != null) bits.push(`${swell.periodS} s`);
  if (swell.dirDeg != null) bits.push(`${swell.dirDeg}°`);
  return `<div><strong>${label}:</strong> ${bits.join(' / ')}</div>`;
}


function setFallbackModelPosition(el, boat) {
  const lat = Number(boat.lat);
  const lon = Number(boat.lon);
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
  const pos = { lat, lng: lon, altitude: BOAT_WATER_ALTITUDE_M };
  try { el.position = pos; } catch (_) {}
  // Attribute fallback mirrors the <gmp-model-3d position="lat,lng,alt"> web-component form.
  try { el.setAttribute('position', `${lat},${lon},${BOAT_WATER_ALTITUDE_M}`); } catch (_) {}
}

function setFallbackModelOrientation(el, headingDeg) {
  const orientation = modelOrientation(headingDeg);
  try { el.orientation = orientation; } catch (_) {}
  // Attribute fallback for custom-element builds that parse heading/orientation attributes.
  try { el.setAttribute('orientation', `${orientation.heading},${orientation.tilt},${orientation.roll}`); } catch (_) {}
  try { el.setAttribute('heading', String(orientation.heading)); } catch (_) {}
  try { el.setAttribute('tilt', String(orientation.tilt)); } catch (_) {}
  try { el.setAttribute('roll', String(orientation.roll)); } catch (_) {}
}

function setFallbackModelScale(el, scale) {
  const safeScale = Math.max(MODEL_SCALE, Math.min(MODEL_SCALE_MAX, Number(scale) || MODEL_SCALE));
  el.scale = safeScale;
  el.setAttribute('scale', String(safeScale));
}

function createModelElement(boat) {
  // GitHub upload path rejects binary assets in this repo, so the bundled GLB is
  // intentionally omitted. Operators may still provide a model at runtime with
  // window.GFS_BOAT_MODEL_SRC; otherwise the SVG glyph/underglow renderers below
  // provide the boat visual without requiring binary files in git.
  if (!MODEL_SRC) return null;
  const api = maps3d();
  const position = modelPosition(boat, BOAT_WATER_ALTITUDE_M);
  const orientation = modelOrientation(withCurrentHeading(boat));
  const displayScale = modelScaleForRange(currentRangeMeters());

  // Preferred Google Maps 3D model path. It requires both src and position.
  // The <gmp-model-3d> custom element fallback below uses the same src/position/scale contract.
  if (api?.Model3DElement) {
    try {
      const model = new api.Model3DElement({
        src: MODEL_SRC,
        position,
        orientation,
        scale: displayScale,
        altitudeMode: api.AltitudeMode?.RELATIVE_TO_GROUND || api.AltitudeMode?.CLAMP_TO_GROUND || 'RELATIVE_TO_GROUND',
      });
      try { model.setAttribute?.('data-model-src', MODEL_SRC); } catch (_) {}
      return tagBoatVisual(model, 'boat-glb-model');
    } catch (err) {
      console.info('[gfs boats] Model3DElement constructor failed; using gmp-model-3d fallback', { message: err?.message || String(err) });
    }
  }

  const el = document.createElement('gmp-model-3d');
  try { el.src = MODEL_SRC; } catch (_) {}
  try { el.position = position; } catch (_) {}
  try { el.orientation = orientation; } catch (_) {}
  try { el.scale = displayScale; } catch (_) {}
  el.setAttribute('src', MODEL_SRC);
  el.setAttribute('position', `${position.lat},${position.lng},${position.altitude}`);
  el.setAttribute('scale', String(displayScale));
  el.setAttribute('heading', String(orientation.heading));
  el.setAttribute('tilt', String(orientation.tilt));
  el.setAttribute('roll', String(orientation.roll));
  el.setAttribute('orientation', `${orientation.heading},${orientation.tilt},${orientation.roll}`);
  try { el.altitudeMode = 'RELATIVE_TO_GROUND'; } catch (_) {}
  el.setAttribute('altitude-mode', 'RELATIVE_TO_GROUND');
  return tagBoatVisual(el, 'boat-glb-model-fallback-element');
}

function createBoatGlyph(boat) {
  const color = colorForSafety(boat.safety?.color, boat.current?.speedKt, boat.waves);
  const heading = modelOrientation(withCurrentHeading(boat)).heading;
  const el = document.createElement('gmp-marker-3d');
  el.drawsWhenOccluded = true;
  el.sizePreserved = true;
  try { el.altitudeMode = maps3d()?.AltitudeMode?.RELATIVE_TO_GROUND || 'RELATIVE_TO_GROUND'; } catch (_) {}
  setMarkerPositionObject(el, boat.lat, boat.lon, BOAT_GLYPH_ALTITUDE_M);
  setInlineSvgTemplate(el, `<svg xmlns="http://www.w3.org/2000/svg" width="116" height="116" viewBox="0 0 116 116" aria-hidden="true" style="pointer-events:none;overflow:visible">
    <defs>
      <filter id="boatGlow" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="4" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
    </defs>
    <g transform="rotate(${heading} 58 58)" filter="url(#boatGlow)">
      <ellipse cx="58" cy="75" rx="28" ry="10" fill="${color}" fill-opacity="0.26"/>
      <path d="M58 12 C72 34 83 58 78 82 C72 92 44 92 38 82 C33 58 44 34 58 12 Z" fill="#f7fbff" stroke="${color}" stroke-width="5"/>
      <path d="M58 20 L69 75 L58 86 L47 75 Z" fill="${color}" fill-opacity="0.35"/>
      <path d="M48 60 L68 60" stroke="#073044" stroke-width="4" stroke-linecap="round"/>
      <circle cx="58" cy="47" r="7" fill="#073044" fill-opacity="0.8"/>
    </g>
  </svg>`);
  return tagBoatVisual(el, 'boat-visible-glyph');
}

function createUnderglow(boat) {
  const el = tagBoatVisual(document.createElement('gmp-marker-3d'), 'boat-underglow');
  setMarkerPositionObject(el, boat.lat, boat.lon, BOAT_UNDERGLOW_ALTITUDE_M);
  el.drawsWhenOccluded = true;
  el.sizePreserved = true;
  const color = colorForSafety(boat.safety?.color, boat.current?.speedKt, boat.waves);
  setInlineSvgTemplate(el, `<svg xmlns="http://www.w3.org/2000/svg" width="72" height="72" viewBox="0 0 72 72" aria-hidden="true" style="pointer-events:none;overflow:visible">
    <circle cx="36" cy="36" r="24" fill="${color}" fill-opacity="0.34" stroke="${color}" stroke-opacity="0.9" stroke-width="3"/>
    <path d="M24 41 L36 22 L48 41 L42 41 L42 50 L30 50 L30 41 Z" fill="white" fill-opacity="0.96" stroke="#062033" stroke-opacity="0.45" stroke-width="1.5"/>
  </svg>`);
  return el;
}

function attachHudHandlers(target, boat) {
  // Hover/title intel is globally disabled for performance. Keep only a tiny
  // non-hover data tag so debug/tools can still identify boat visuals.
  try { target?.removeAttribute?.('title'); } catch (_) {}
  try { target?.setAttribute?.('data-boat-id', String(boat?.id || 'boat')); } catch (_) {}
}

function boatSceneTier(viewport) {
  const width = Math.abs(Number(viewport?.east) - Number(viewport?.west));
  const height = Math.abs(Number(viewport?.north) - Number(viewport?.south));
  const span = Math.max(width || 0, height || 0);
  const area = Math.max(0, width * height);
  if (span <= 1.6 && area <= 2.6) return 'harbor';
  if (span <= 4.0 && area <= 14.0) return 'coastal';
  if (span <= 12.0 && area <= 90.0) return 'regional';
  return 'world';
}

export async function fetchBoatsPayload({ bbox, viewport }) {
  const bboxQ = encodeURIComponent(`${bbox.west.toFixed(4)},${bbox.south.toFixed(4)},${bbox.east.toFixed(4)},${bbox.north.toFixed(4)}`);
  const safeViewport = {
    west: Number(viewport.west),
    south: Number(viewport.south),
    east: Number(viewport.east),
    north: Number(viewport.north),
    quality: String(viewport.quality || 'coarse'),
    camera: viewport.camera && viewport.camera.center ? {
      center: {
        lat: Number(viewport.camera.center.lat),
        lon: Number(viewport.camera.center.lon),
      },
      range: Number(viewport.camera.range),
      source: String(viewport.camera.source || ''),
    } : null,
  };
  safeViewport.visibleBbox = viewport?.visibleBbox || viewport?.visible_bbox || viewport || null;
  safeViewport.scene_tier = boatSceneTier(safeViewport.visibleBbox || safeViewport);
  const vpQ = encodeURIComponent(JSON.stringify(safeViewport));
  const visible = safeViewport.visibleBbox || safeViewport;
  const visibleQ = encodeURIComponent(`${Number(visible.west).toFixed(4)},${Number(visible.south).toFixed(4)},${Number(visible.east).toFixed(4)},${Number(visible.north).toFixed(4)}`);
  // Direct /gfs/api/boats is debug/manual only now. Read the boater layer from
  // the shared scene cache, then let /gfs/api/cache/refresh warm it in background.
  return getJsonSafe(`/gfs/api/scene-cache?bbox=${bboxQ}&visible_bbox=${visibleQ}&scene_tier=${encodeURIComponent(safeViewport.scene_tier)}&layers=boater&mode=fast&fast=1&refresh=0&reason=boats_layer_cache_read`, null).then((payload) => {
    const out = payload?.layers?.boater || payload?.layers?.boats || { boats: [] };
    try { out.__requestedViewport = safeViewport; } catch (_) {}
    try { getJsonSafe(`/gfs/api/cache/refresh?bbox=${bboxQ}&visible_bbox=${visibleQ}&scene_tier=${encodeURIComponent(safeViewport.scene_tier)}&layers=boater&reason=boats_layer_background_refresh`, null, { abortPrevious: false, timeoutMs: 2500 }); } catch (_) {}
    return out;
  });
}


function payloadOceanPoints(payload) {
  if (Array.isArray(payload?.oceanAnalysisPoints?.points || payload?.oceanPoints?.points)) return payload.oceanPoints.points;
  if (Array.isArray(payload?.ocean_points)) return payload.ocean_points;
  if (Array.isArray(payload?.points)) return payload.points;
  if (Array.isArray(payload?.ocean?.points)) return payload.ocean.points;
  return [];
}

function normalizeDeg(value, fallback = 0) {
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  return ((n % 360) + 360) % 360;
}

function n(value, fallback = null) {
  const out = Number(value);
  return Number.isFinite(out) ? out : fallback;
}

function compactRound(value, digits = 2) {
  const out = Number(value);
  if (!Number.isFinite(out)) return null;
  return Number(out.toFixed(digits));
}

function pointCurrent(point) {
  const u = n(point?.u ?? point?.current?.u, 0) || 0;
  const v = n(point?.v ?? point?.current?.v, 0) || 0;
  let speedKt = n(point?.speedKt ?? point?.current?.speedKt ?? point?.current_speed_kt, null);
  if (speedKt == null) speedKt = Math.hypot(u, v);
  let dirDeg = n(point?.heading ?? point?.headingDeg ?? point?.current?.dirDeg ?? point?.current_direction_deg, null);
  if (dirDeg == null && (Math.abs(u) > 1e-6 || Math.abs(v) > 1e-6)) dirDeg = normalizeDeg(Math.atan2(u, v) * 180 / Math.PI);
  return { u, v, speedKt: speedKt || 0, dirDeg: normalizeDeg(dirDeg || 0) };
}

function normalizeSample(point, index = 0) {
  const ll = finiteLatLon(point);
  if (!ll) return null;
  const { lat, lon } = ll;
  const cur = pointCurrent(point);
  const waves = point?.waves || point?.wave || {};
  const wind = point?.wind || {};
  const water = point?.water || {};
  return {
    id: point?.id || `sample-${index}`,
    lat,
    lon,
    current: cur,
    waves,
    wind,
    water,
    safety: point?.safety || {},
    marineStation: point?.marineStation || null,
    source: point?.source || 'ocean_current_sample',
  };
}

function allBoatSamples(payload) {
  const samples = [];
  const rejected = { nan_lat_lon: 0, no_current: 0 };
  payloadOceanPoints(payload).forEach((p, i) => {
    const s = normalizeSample(p, i);
    if (s) samples.push(s); else rejected.nan_lat_lon += 1;
  });
  (Array.isArray(payload?.boats) ? payload.boats : []).forEach((p, i) => {
    const s = normalizeSample(p, i + samples.length);
    if (s) samples.push(s); else rejected.nan_lat_lon += 1;
  });
  const currentBacked = samples.filter((s) => s.current?.speedKt != null || s.current?.u != null || s.current?.v != null);
  rejected.no_current = Math.max(0, samples.length - currentBacked.length);
  try { payload.__frontendBoatRejected = rejected; } catch (_) {}
  return currentBacked;
}

function safetyFromSeaState(currentSpeedKt, waves, wind) {
  const band = boatingBandForWaves(waves);
  const windKt = Number(wind?.speedKt);
  let color = band.color;
  let label = band.label;
  if (Number.isFinite(windKt) && windKt >= 30 && color !== 'red') {
    label += ' / high wind advisory';
  }
  return { color, label, waveHeightFt: band.waveHeightFt, thresholds: '0-3ft green; >3-4ft yellow; >4ft red', derivedFrom: 'interpolated_current_wave_field' };
}

function normalizeBoatFromPoint(point, index) {
  const s = normalizeSample(point, index);
  if (!s) return null;
  const current = s.current;
  const waves = s.waves || {};
  const wind = s.wind || {};
  const water = s.water || {};
  return {
    id: `boat-${s.id || index}`,
    lat: compactRound(jitterBoatSamplePosition(s, index).lat, 5),
    lon: compactRound(jitterBoatSamplePosition(s, index).lon, 5),
    displayLengthFt: 26,
    headingDeg: current.dirDeg,
    current: {
      u: compactRound(current.u, 4) || 0,
      v: compactRound(current.v, 4) || 0,
      speedKt: compactRound(current.speedKt, 2) || 0,
      speedMph: compactRound(knotsToMph(current.speedKt || 0), 2),
      dirDeg: compactRound(current.dirDeg, 1) || 0,
    },
    water,
    safety: s.safety?.color ? s.safety : safetyFromSeaState(current.speedKt, waves, wind),
    waves,
    wind,
    marineStation: s.marineStation,
    source: s.source || 'boat_sample',
  };
}


function hasValidSstWater(sample) {
  // Strict display gate: boats are not allowed to pop up from generic current,
  // wind, wave, station, or server "water" hints. They must carry finite HYCOM SST.
  const ov = sample?.ocean_vars || {};
  const values = [
    sample?.water?.sst_c, sample?.water?.sstC, sample?.water?.sst_f, sample?.water?.sstF,
    sample?.water?.tempF, sample?.water?.tempC, sample?.sst, sample?.sst_c, sample?.sst_f,
    sample?.water_temp_f, sample?.water_temp_c, ov?.sst_f, ov?.sst_c,
  ];
  return values.some((value) => Number.isFinite(Number(value)));
}

function hasUsableCurrent(sample) {
  return Number.isFinite(Number(sample?.current?.speedKt))
    || Number.isFinite(Number(sample?.current?.u))
    || Number.isFinite(Number(sample?.current?.v));
}

function dedupeByCellOrPosition(samples) {
  const out = [];
  const seen = new Set();
  for (const sample of samples || []) {
    const key = sample?.gridIndex
      ? `${sample.gridIndex.iy}:${sample.gridIndex.ix}`
      : `${Number(sample.lat).toFixed(4)}:${Number(sample.lon).toFixed(4)}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(sample);
  }
  return out;
}

function establishedWaterCell(sample) {
  const valid = Number(sample?.cell?.validNeighbors ?? sample?.cell?.valid_neighbors ?? 9);
  const possible = Number(sample?.cell?.possibleNeighbors ?? sample?.cell?.possible_neighbors ?? 9);
  if (Number.isFinite(possible) && possible >= 6 && Number.isFinite(valid)) return valid >= Math.min(6, possible);
  return true;
}

function boatScatterRand(seed) {
  const s = String(seed || 'boat');
  let h = 2166136261;
  for (let i = 0; i < s.length; i += 1) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
  h ^= h >>> 15; h = Math.imul(h, 2246822507); h ^= h >>> 13;
  return ((h >>> 0) % 1000000) / 1000000;
}

function jitterBoatSamplePosition(sample, index) {
  // If the backend already placed the boat randomly inside the live water cell,
  // do not double-jitter it on the client. Double-jitter can push boats toward
  // padded viewport edges and make bottom-edge rows visible on tilted loads.
  if (String(sample?.placement?.mode || '').includes('scattered') || String(sample?.cell?.placement || '').includes('deterministic_random')) {
    return { lat: Number(sample?.lat), lon: Number(sample?.lon ?? sample?.lng) };
  }
  const lat = Number(sample?.lat);
  const lon = Number(sample?.lon ?? sample?.lng);
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return { lat, lon };
  const dLat = Math.max(0.002, Math.min(0.08, Math.abs(Number(sample?.cell?.dLat ?? sample?.cell?.dlat ?? 0.018))));
  const dLon = Math.max(0.002, Math.min(0.08, Math.abs(Number(sample?.cell?.dLon ?? sample?.cell?.dlon ?? 0.018))));
  const seed = `${sample?.id || index}:${lat.toFixed(5)}:${lon.toFixed(5)}`;
  const jy = (boatScatterRand(`${seed}:lat`) - 0.5) * dLat * 0.72;
  const jx = (boatScatterRand(`${seed}:lon`) - 0.5) * dLon * 0.72;
  return { lat: lat + jy, lon: lon + jx };
}

function geoDistance2(a, b) {
  const alat = Number(a?.lat); const blat = Number(b?.lat);
  const alon = Number(a?.lon ?? a?.lng); const blon = Number(b?.lon ?? b?.lng);
  if (![alat, blat, alon, blon].every(Number.isFinite)) return Infinity;
  const meanLat = ((alat + blat) * 0.5) * Math.PI / 180;
  const dx = (alon - blon) * Math.max(0.25, Math.cos(meanLat));
  const dy = alat - blat;
  return dx * dx + dy * dy;
}

function scatterRank(sample, index, payload = null) {
  const vp = payload?.__requestedViewport || payload?.viewport_bbox || payload?.viewport || payload?.bbox_object || payload?.bbox || {};
  return boatScatterRand(`${sample?.id || index}:${sample?.lat}:${sample?.lon}:${vp?.west}:${vp?.south}:${vp?.east}:${vp?.north}`);
}

function selectSstBackedBoatPoints(samples, limit = VIEWPORT_BOAT_COUNT, payload = null) {
  const candidates = dedupeByCellOrPosition(samples)
    .filter((s) => finiteLatLon(s))
    .filter((s) => hasValidSstWater(s) && hasUsableCurrent(s) && establishedWaterCell(s));
  if (candidates.length <= limit) return candidates;

  const b = payload?.__requestedViewport || payload?.viewport_bbox || payload?.viewport || payload?.bbox_object || payload?.bbox || null;
  let span = 1.0;
  if (b) {
    const west = Number(Array.isArray(b) ? b[0] : b.west);
    const south = Number(Array.isArray(b) ? b[1] : b.south);
    const east = Number(Array.isArray(b) ? b[2] : b.east);
    const north = Number(Array.isArray(b) ? b[3] : b.north);
    if ([west, south, east, north].every(Number.isFinite)) span = Math.max(Math.abs(north - south), Math.abs(east - west), 0.05);
  }
  const minSep = Math.max(0.012, Math.min(0.5, span / Math.max(3.0, Math.sqrt(limit) * 3.4)));
  const minSep2 = minSep * minSep;

  const ranked = candidates
    .map((sample, index) => ({ sample, rank: scatterRank(sample, index, payload), speed: Number(sample?.current?.speedKt || 0) }))
    .sort((a, b) => (a.rank - b.rank) || (b.speed - a.speed));

  const selected = [];
  for (const row of ranked) {
    if (selected.every((prev) => geoDistance2(row.sample, prev) >= minSep2)) {
      selected.push(row.sample);
      if (selected.length >= limit) break;
    }
  }
  // Harbor/narrow bboxes may have fewer candidates than spacing allows. Fill the
  // remainder randomly rather than falling back to a row/stride pattern.
  if (selected.length < limit) {
    const seen = new Set(selected.map((s) => s?.id || `${s?.lat}:${s?.lon}`));
    for (const row of ranked) {
      const key = row.sample?.id || `${row.sample?.lat}:${row.sample?.lon}`;
      if (seen.has(key)) continue;
      selected.push(row.sample);
      if (selected.length >= limit) break;
    }
  }
  try {
    if (payload) payload.__frontendBoatScatter = { candidates: candidates.length, selected: selected.length, minSepDeg: Number(minSep.toFixed(5)), mode: 'visible_viewport_random_spacing' };
  } catch (_) {}
  return selected;
}

function hasStrictLandmaskContract(payload) {
  const contract = String(payload?.landmask_contract || payload?.source_meta?.landmask_contract || payload?.sst_landmask?.contract || '').toLowerCase();
  const method = String(payload?.source_meta?.sst_landmask?.method || payload?.grid?.coastline_guard || '').toLowerCase();
  return contract.includes('sst') || contract.includes('landmask') || method.includes('sst') || method.includes('landmask');
}

function boatPassesStrictSstMask(boat) {
  if (!boat || boat.valid === false || boat.water === false) return false;
  if (!hasValidSstWater(boat) || !hasUsableCurrent(boat)) return false;
  const mask = String(boat?.cell?.mask || boat?.mask || '').toLowerCase();
  if (mask && !(mask.includes('sst') || mask.includes('water') || mask.includes('hycom'))) return false;
  const vn = Number(boat?.cell?.validNeighbors ?? boat?.validNeighbors ?? 9);
  const pn = Number(boat?.cell?.possibleNeighbors ?? boat?.possibleNeighbors ?? 9);
  if (Number.isFinite(pn) && pn >= 6 && Number.isFinite(vn) && vn < 8) return false;
  return true;
}

function isLiveWaterPayload(payload) {
  const source = String(payload?.source || payload?.ocean?.source || payload?.oceanPoints?.source || '').toLowerCase();
  const mode = String(payload?.mode || payload?.ocean?.mode || '').toLowerCase();
  // Never draw GLB boats from old marker/proxy/random fallback payloads. Boats
  // should exist only where the live SST/current grid has rolled in.
  if (source.includes('fallback') || source.includes('marker_ocean_solve')) return false;
  if (mode.includes('fallback') || mode.includes('proxy')) return false;
  return hasStrictLandmaskContract(payload);
}


function boatPayloadSstReady(payload) {
  if (!isLiveWaterPayload(payload)) return false;
  const source = String(payload?.source || payload?.ocean?.source || payload?.oceanPoints?.source || '').toLowerCase();
  if (!source.includes('hycom')) return false;
  const validTime = payload?.validTime || payload?.valid_time || payload?.resolvedTime || payload?.resolved_time || payload?.sourceTime || payload?.source_time;
  const oceanPointCount = Number(payload?.ocean_point_count ?? payload?.oceanPoints?.count ?? (Array.isArray(payload?.oceanAnalysisPoints?.points || payload?.oceanPoints?.points) ? payload.oceanPoints.points.length : NaN) ?? (Array.isArray(payload?.ocean_points) ? payload.ocean_points.length : NaN) ?? (Array.isArray(payload?.points) ? payload.points.length : NaN));
  const minOceanPoints = Number(window.GFS_BOAT_MIN_OCEAN_POINTS || 48);
  const samples = allBoatSamples(payload);
  const sstCurrentSamples = samples.filter((s) => hasValidSstWater(s) && hasUsableCurrent(s) && establishedWaterCell(s)).length;
  try {
    payload.__frontendBoatReadiness = {
      source,
      validTime: validTime || null,
      oceanPointCount: Number.isFinite(oceanPointCount) ? oceanPointCount : 0,
      minOceanPoints,
      sstCurrentSamples,
      ready: Number.isFinite(oceanPointCount) && oceanPointCount >= minOceanPoints && sstCurrentSamples >= Math.min(VIEWPORT_BOAT_COUNT, 6),
      validTimePolicy: validTime ? 'hycom_valid_time_present' : 'hycom_time_alias_missing_but_sst_current_samples_present',
      contract: 'boats_render_after_live_hycom_sst_current_samples_and_ocean_points',
    };
  } catch (_) {}
  return Boolean(payload.__frontendBoatReadiness?.ready);
}

function rejectedBoatCount(payload) {
  const raw = Array.isArray(payload?.boats) ? payload.boats.length : 0;
  return isLiveWaterPayload(payload) ? 0 : raw;
}

function insidePayloadBbox(sample, payload) {
  // Use the actual screen viewport when the caller passed it. The backend fetch
  // bbox is intentionally larger for tilted screens/cache warming, but boat GLBs
  // should be scattered inside the visible view, not along the lower padded edge.
  const b = payload?.__requestedViewport || payload?.viewport_bbox || payload?.viewport || payload?.bbox_object || payload?.bbox || payload?.ocean?.bbox || payload?.oceanPoints?.bbox_object || payload?.oceanPoints?.bbox;
  if (!b) return true;
  const west = Number(Array.isArray(b) ? b[0] : b.west);
  const south = Number(Array.isArray(b) ? b[1] : b.south);
  const east = Number(Array.isArray(b) ? b[2] : b.east);
  const north = Number(Array.isArray(b) ? b[3] : b.north);
  const lat = Number(sample?.lat);
  const lon = Number(sample?.lon ?? sample?.lng);
  if (![west, south, east, north, lat, lon].every(Number.isFinite)) return true;
  const padLat = Math.max(0.01, Math.abs(north - south) * 0.035);
  const padLon = Math.max(0.01, Math.abs(east - west) * 0.035);
  return lat >= (south + padLat) && lat <= (north - padLat) && lon >= (Math.min(west, east) + padLon) && lon <= (Math.max(west, east) - padLon);
}

function boatsFromPayload(payload) {
  if (!boatPayloadSstReady(payload) && !Array.isArray(payload?.boats)) return [];
  const samples = allBoatSamples(payload).filter((s) => insidePayloadBbox(s, payload));
  const sstBacked = selectSstBackedBoatPoints(samples, VIEWPORT_BOAT_COUNT, payload)
    .map(normalizeBoatFromPoint)
    .filter(Boolean);
  if (sstBacked.length) return sstBacked;

  const rawBoats = (Array.isArray(payload?.boats) ? payload.boats : [])
    .map(normalizeBoatFromPoint)
    .filter((boat) => boat && insidePayloadBbox(boat, payload) && boatPassesStrictSstMask(boat))
    .slice(0, VIEWPORT_BOAT_COUNT);
  if (rawBoats.length) return rawBoats;

  return [];
}


const BOAT_RENDER_STATE = window.__gfsBoatRenderState || {
  map: null,
  instances: new Map(),
  frame: null,
  lastTick: 0,
  lastStats: null,
};
try { window.__gfsBoatRenderState = BOAT_RENDER_STATE; } catch (_) {}

function stableBoatId(boat, index = 0) {
  if (boat?.id) return String(boat.id);
  const lat = Number(boat?.lat);
  const lon = Number(boat?.lon ?? boat?.lng);
  const qLat = Number.isFinite(lat) ? (Math.round(lat * 4) / 4).toFixed(2) : 'na';
  const qLon = Number.isFinite(lon) ? (Math.round(lon * 4) / 4).toFixed(2) : 'na';
  return `boat:${qLon}:${qLat}:${index % BOAT_COUNT_MAX}`;
}

function boatRenderableKey(boat) {
  return [
    stableBoatId(boat, 0),
    Number(boat?.lat).toFixed(5),
    Number(boat?.lon).toFixed(5),
    Number(withCurrentHeading(boat)).toFixed(1),
    boat?.safety?.color || '',
    dominantWaveHeightFt(boat?.waves || {}) ?? '',
  ].join('|');
}

function updateBoatGlyphSvg(instance) {
  if (!instance?.glyph || !instance?.boat) return;
  const key = boatRenderableKey(instance.boat);
  if (instance.glyph.__boatRenderableKey === key) return;
  const replacement = createBoatGlyph(instance.boat);
  try { instance.glyph.replaceWith(replacement); } catch (_) {
    try { instance.glyph.remove(); } catch (_) {}
    try { BOAT_RENDER_STATE.map?.append(replacement); } catch (_) {}
  }
  instance.glyph = replacement;
  instance.glyph.__boatRenderableKey = key;
  attachHudHandlers(instance.glyph, instance.boat);
}

function createBoatInstance(boat, map3DElement) {
  const model = createModelElement(boat);
  const glyph = createBoatGlyph(boat);
  const underglow = createUnderglow(boat);
  if (model) {
    try { tagBoatElement(model); map3DElement.append(model); } catch (_) {}
    attachHudHandlers(model, boat);
  }
  try { tagBoatElement(underglow); map3DElement.append(underglow); } catch (_) {}
  try { tagBoatElement(glyph); map3DElement.append(glyph); } catch (_) {}
  attachHudHandlers(underglow, boat);
  attachHudHandlers(glyph, boat);
  return {
    id: boat.id,
    boat,
    model,
    glyph,
    underglow,
    currentLat: Number(boat.lat),
    currentLon: Number(boat.lon),
    currentHeading: withCurrentHeading(boat),
    targetLat: Number(boat.lat),
    targetLon: Number(boat.lon),
    targetHeading: withCurrentHeading(boat),
    lastSeen: performance.now(),
  };
}

function lerp(a, b, t) {
  const aa = Number(a); const bb = Number(b);
  if (!Number.isFinite(aa)) return bb;
  if (!Number.isFinite(bb)) return aa;
  return aa + (bb - aa) * t;
}

function lerpAngleDeg(a, b, t) {
  const aa = normalizeDeg(a || 0);
  const bb = normalizeDeg(b || 0);
  const delta = ((bb - aa + 540) % 360) - 180;
  return normalizeDeg(aa + delta * t);
}

function updateBoatInstance(instance, boat) {
  if (!instance || !boat) return;
  instance.boat = boat;
  instance.targetLat = Number(boat.lat);
  instance.targetLon = Number(boat.lon);
  instance.targetHeading = withCurrentHeading(boat);
  instance.lastSeen = performance.now();
  if (instance.model) attachHudHandlers(instance.model, boat);
  if (instance.underglow) attachHudHandlers(instance.underglow, boat);
  updateBoatGlyphSvg(instance);
}

function removeBoatInstance(instance) {
  if (!instance) return;
  try { instance.model?.remove(); } catch (_) {}
  try { instance.glyph?.remove(); } catch (_) {}
  try { instance.underglow?.remove(); } catch (_) {}
}

export function clearBoatRenderState() {
  if (BOAT_RENDER_STATE.frame) {
    try { window.cancelAnimationFrame(BOAT_RENDER_STATE.frame); } catch (_) {}
  }
  BOAT_RENDER_STATE.frame = null;
  BOAT_RENDER_STATE.instances.forEach(removeBoatInstance);
  BOAT_RENDER_STATE.instances.clear();
  try { BOAT_RENDER_STATE.map?.querySelectorAll?.('[data-gfs-layer="boater"]')?.forEach((el) => el.remove()); } catch (_) {}
}

try {
  window.clearGfsBoatsLayer = clearBoatRenderState;
  window.__gfsBoatsRenderState = BOAT_RENDER_STATE;
} catch (_) {}


function tickBoatRender(now) {
  const last = BOAT_RENDER_STATE.lastTick || now;
  const dt = Math.min(0.25, Math.max(0.001, (now - last) / 1000));
  BOAT_RENDER_STATE.lastTick = now;
  const t = Math.min(1, dt * 3.5);
  const rangeM = currentRangeMeters();
  const visibleScale = visibleScaleForRange(rangeM);
  const modelScale = modelScaleForRange(rangeM);

  for (const instance of BOAT_RENDER_STATE.instances.values()) {
    instance.currentLat = lerp(instance.currentLat, instance.targetLat, t);
    instance.currentLon = lerp(instance.currentLon, instance.targetLon, t);
    instance.currentHeading = lerpAngleDeg(instance.currentHeading, instance.targetHeading, t);
    const boat = { ...instance.boat, lat: instance.currentLat, lon: instance.currentLon, displayHeadingDeg: instance.currentHeading };
    if (instance.model) {
      if (instance.model.tagName && String(instance.model.tagName).toLowerCase() === 'gmp-model-3d') {
        setFallbackModelPosition(instance.model, boat);
        setFallbackModelOrientation(instance.model, boat.displayHeadingDeg);
        setFallbackModelScale(instance.model, modelScale);
      } else {
        try { instance.model.position = modelPosition(boat, BOAT_WATER_ALTITUDE_M); } catch (_) {}
        try { instance.model.orientation = modelOrientation(boat.displayHeadingDeg); } catch (_) {}
        try { instance.model.scale = modelScale; } catch (_) {}
      }
    }
    if (instance.glyph) {
      setMarkerPositionObject(instance.glyph, boat.lat, boat.lon, BOAT_GLYPH_ALTITUDE_M);
      try { if (typeof instance.glyph.scale !== 'undefined') instance.glyph.scale = Math.max(0.72, Math.min(1.35, visibleScale * 0.70)); } catch (_) {}
    }
    if (instance.underglow) {
      setMarkerPositionObject(instance.underglow, boat.lat, boat.lon, BOAT_UNDERGLOW_ALTITUDE_M);
      try { if (typeof instance.underglow.scale !== 'undefined') instance.underglow.scale = Math.max(0.55, Math.min(1.10, visibleScale * 0.62)); } catch (_) {}
    }
  }
  BOAT_RENDER_STATE.frame = window.requestAnimationFrame(tickBoatRender);
}

function ensureBoatAnimation() {
  if (BOAT_RENDER_STATE.frame) return;
  BOAT_RENDER_STATE.lastTick = performance.now();
  BOAT_RENDER_STATE.frame = window.requestAnimationFrame(tickBoatRender);
}

export function renderBoatsLayer({ payload, map3DElement }) {
  BOAT_RENDER_STATE.map = map3DElement;
  const raw = boatsFromPayload(payload);
  const oceanRows = Array.isArray(payload?.oceanAnalysisPoints?.points) ? payload.oceanAnalysisPoints.points
    : (Array.isArray(payload?.oceanPoints?.points) ? payload.oceanPoints.points
      : (Array.isArray(payload?.ocean_analysis_points) ? payload.ocean_analysis_points
        : (Array.isArray(payload?.ocean_points) ? payload.ocean_points : (Array.isArray(payload?.points) ? payload.points : []))));
  const oceanPointCount = oceanRows.length;
  const warmingEmpty = raw.length === 0 && (
    String(payload?.source || '').includes('scene_cache_empty')
    || String(payload?.status || payload?.payload_state || '').toLowerCase().includes('warming')
    || payload?.cache?.hit === false
  );
  if (warmingEmpty) {
    const stats = {
      payloadBoats: 0,
      rawSamples: 0,
      selected: 0,
      rendered: BOAT_RENDER_STATE.instances.size,
      updated: 0,
      created: 0,
      removed: 0,
      cap: BOAT_COUNT_MAX,
      source: payload?.source,
      oceanPointCount,
      placement: 'preserve_existing_boats_while_hycom_ocean_points_warm',
    };
    BOAT_RENDER_STATE.lastStats = stats;
    console.info('[gfs boats] preserved existing boats during empty HYCOM warm', stats);
    try { window.__gfsDebugEvent?.('boats/preserve-empty', stats); } catch (_) {}
    const disposer = () => clearBoatRenderState();
    disposer.__gfsKeepExisting = true;
    disposer.__gfsDidRender = BOAT_RENDER_STATE.instances.size > 0;
    return disposer;
  }
  const boats = raw.slice(0, BOAT_COUNT_MAX).map((boat, index) => ({
    ...boat,
    id: stableBoatId(boat, index),
    originLat: Number(boat.lat),
    originLon: Number(boat.lon),
    displayHeadingDeg: withCurrentHeading(boat),
  }));
  const validTime = payload?.resolvedTime || payload?.sourceTime || null;
  const incomingIds = new Set();
  let created = 0;
  let updated = 0;
  let removed = 0;

  boats.forEach((boat, index) => {
    const id = stableBoatId(boat, index);
    boat.id = id;
    incomingIds.add(id);
    const existing = BOAT_RENDER_STATE.instances.get(id);
    if (existing) {
      updateBoatInstance(existing, boat);
      updated += 1;
    } else {
      const instance = createBoatInstance(boat, map3DElement);
      BOAT_RENDER_STATE.instances.set(id, instance);
      created += 1;
    }
  });

  for (const [id, instance] of BOAT_RENDER_STATE.instances.entries()) {
    if (!incomingIds.has(id)) {
      removeBoatInstance(instance);
      BOAT_RENDER_STATE.instances.delete(id);
      removed += 1;
    }
  }

  ensureBoatAnimation();

  if (boats.length) {
    setHudContent(`<div><strong>Boater Awareness</strong>${validTime ? `<div><strong>Valid:</strong> ${validTime}</div>` : ''}<div>${boats.length} boat markers active.</div></div>`);
  }

  const stats = {
    payloadBoats: Array.isArray(payload?.boats) ? payload.boats.length : 0,
    rawSamples: raw.length,
    selected: boats.length,
    rendered: BOAT_RENDER_STATE.instances.size,
    updated,
    created,
    removed,
    cap: BOAT_COUNT_MAX,
    rejectedFallbackBoats: rejectedBoatCount(payload),
    backendRejections: payload?.rejection_counts || payload?.grid?.rejection_counts || null,
    frontendRejections: payload?.__frontendBoatRejected || null,
    model: MODEL_SRC || null,
    glbAttempted: Boolean(MODEL_SRC),
    clampToGround: true,
    modelTransform: {
      waterAltitudeM: BOAT_WATER_ALTITUDE_M,
      glyphAltitudeM: BOAT_GLYPH_ALTITUDE_M,
      underglowAltitudeM: BOAT_UNDERGLOW_ALTITUDE_M,
      scale: modelScaleForRange(currentRangeMeters()),
      minScale: MODEL_SCALE,
      maxScale: MODEL_SCALE_MAX,
      growth: MODEL_SCALE_GROWTH,
      yawOffsetDeg: MODEL_YAW_OFFSET_DEG,
      tiltDeg: MODEL_TILT_DEG,
      rollDeg: MODEL_ROLL_DEG,
      contract: 'water_hugging_stable_scale_single_yaw_offset',
    },
    dynamicModelScale: { min: MODEL_SCALE, max: MODEL_SCALE_MAX },
    visibleGlyphFallback: true,
    validTime,
    source: payload?.oceanPoints?.source || payload?.source,
    orientationCorrection: { tilt: MODEL_TILT_DEG, roll: MODEL_ROLL_DEG },
    oceanPointCount,
    sstReadiness: payload?.__frontendBoatReadiness || null,
    properBoatSquares: oceanPointCount > raw.length && Boolean(payload?.__frontendBoatReadiness?.ready),
    placement: 'reconciled_boats_after_live_hycom_sst_current_sample_gate',
  };
  BOAT_RENDER_STATE.lastStats = stats;
  console.info('[gfs boats] reconciled SST/current-backed GLB boats', stats);
  try { window.__gfsDebugEvent?.('boats/reconcile', stats); } catch (_) {}

  const disposer = () => clearBoatRenderState();
  // RendererLayer should keep the existing disposer/state when boater receives a
  // new payload. This renderer owns reconciliation internally; disposal is only
  // for pill-off or full layer clear.
  disposer.__gfsKeepExisting = true;
  disposer.__gfsDidRender = true;
  return disposer;
}
