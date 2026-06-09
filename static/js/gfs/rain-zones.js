// normalizePolygonFieldPayload compatibility marker for compact polygon_field_v1 contract.
import { estimateCloudColumnAltitudes } from './cloud-zones.js';
import { attachPolygonHover } from './hover_tip.js';

const MAX_MARKERS_PER_FRAME = Number(window.GFS_RAIN_MAX_MARKERS || 420);
const MAX_COLUMNS = Number(window.GFS_RAIN_MAX_COLUMNS || 160);
// No artificial rain coverage cap: all real rain regions should appear.
// Performance is protected by adaptive animation density, not by hiding rain.
const RAIN_COLUMNS_PER_REGION_MAX = Number(window.GFS_RAIN_COLUMNS_PER_REGION_MAX || 18);
const RAIN_ANIMATION_COLUMNS_PER_VIEW_SOFT_MAX = Number(window.GFS_RAIN_ANIMATION_COLUMNS_PER_VIEW_SOFT_MAX || (/android|iphone|ipad|mobile/i.test(navigator.userAgent || '') ? 120 : 360));
const MAX_ADVECTION_STEP_SEC = 0.16;
const RAIN_FALL_SPEED_MPS = 7.4;
const RAIN_ADVECTION_INTERVAL_MS = Number(window.GFS_RAIN_ADVECTION_INTERVAL_MS ?? 350);
const RAIN_COLOR_JITTER_MIN = 0.62;
const RAIN_COLOR_JITTER_MAX = 1.42;

function polygonApiPath() {
  return 'gmp-marker-3d.template.svg';
}

function toNumber(v, fallback = 0) {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
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
  const bounds = items.map((item) => item?.bounds || {}).filter(Boolean);
  if (bounds.length) {
    const west = Math.min(...bounds.map((x) => toNumber(x.lon_min ?? x.west, Infinity)));
    const south = Math.min(...bounds.map((x) => toNumber(x.lat_min ?? x.south, Infinity)));
    const east = Math.max(...bounds.map((x) => toNumber(x.lon_max ?? x.east, -Infinity)));
    const north = Math.max(...bounds.map((x) => toNumber(x.lat_max ?? x.north, -Infinity)));
    if ([west, south, east, north].every(Number.isFinite)) return { west, south, east, north };
  }
  return null;
}

function latLonFromIndex(i, j, ny, nx, bbox) {
  const lat = bbox.south + ((i + 0.5) / Math.max(1, ny)) * (bbox.north - bbox.south);
  const lon = bbox.west + ((j + 0.5) / Math.max(1, nx)) * (bbox.east - bbox.west);
  return { lat, lon };
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function wrapLongitude(lon) {
  let value = Number(lon) || 0;
  while (value < -180) value += 360;
  while (value >= 180) value -= 360;
  return value;
}

function uniqueId(prefix = 'rain') {
  return `${prefix}-${Math.random().toString(36).slice(2, 10)}`;
}

function metersToLatDegrees(meters) {
  return meters / 111320;
}

function metersToLonDegrees(meters, lat) {
  const lonScale = Math.max(0.2, Math.cos((Number(lat) * Math.PI) / 180));
  return meters / (111320 * lonScale);
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

function rainStyleForRate(rate) {
  const r = Number(rate) || 0;
  // User-facing radar-like precip scale.  Trace is light blue, then green/yellow/orange/red/black.
  if (r <= 0.10) return { color: '#8fdcff', core: '#caf2ff', halo: '#6ecfff', size: 13, opacity: 0.72, label: 'trace', band: 'trace rain' };
  if (r <= 0.75) return { color: '#38d978', core: '#a5ffbf', halo: '#22b95d', size: 15, opacity: 0.78, label: 'light', band: 'light rain' };
  if (r <= 2.5) return { color: '#ffe44d', core: '#fff5a8', halo: '#ffd21f', size: 17, opacity: 0.84, label: 'moderate', band: 'moderate rain' };
  if (r <= 7.5) return { color: '#ff982e', core: '#ffd09a', halo: '#ff7a00', size: 20, opacity: 0.9, label: 'heavy', band: 'heavy rain' };
  if (r <= 20) return { color: '#ff3030', core: '#ff9a9a', halo: '#e00000', size: 23, opacity: 0.94, label: 'very_heavy', band: 'very heavy rain' };
  return { color: '#050505', core: '#2b0000', halo: '#ff0000', size: 26, opacity: 0.98, label: 'extreme', band: 'extreme rain core' };
}

function stableHashString(value) {
  const text = String(value || 'rain');
  let h = 2166136261;
  for (let i = 0; i < text.length; i += 1) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

function rand01(seed, salt = 0) {
  let x = (Number(seed) || 1) + (salt * 0x9e3779b9);
  x ^= x << 13; x ^= x >>> 17; x ^= x << 5;
  return ((x >>> 0) % 100000) / 100000;
}

function regionRainTopAltitude(region) {
  const inten = region?.intensity || {};
  const high = densityPercent(inten.high ?? 0) / 100;
  const mid = densityPercent(inten.mid ?? 0) / 100;
  const low = densityPercent(inten.low ?? 0) / 100;
  const tower = densityPercent(inten.tower ?? 0) / 100;
  const type = String(region?.cloud_type || region?.family || '').toLowerCase();
  if (type.includes('cumulonimbus') || tower > 0.55) return 10500 + tower * 3500;
  if (type.includes('nimbostratus')) return 6200 + mid * 1800;
  if (type.includes('tower')) return 7800 + tower * 2500;
  if (high > 0.45) return 7600 + high * 2200;
  if (mid > 0.35) return 4300 + mid * 1800;
  return 1900 + low * 2200;
}

function densityPercent(value) {
  const n = toNumber(value, 0);
  if (n <= 1.25) return clamp(n * 100, 0, 100);
  return clamp(n, 0, 100);
}

function normalizePrecipRate(value) {
  const raw = toNumber(value, 0);
  if (raw <= 0) return 0;
  // Some tile payloads carry precipitation_factor/rain_factor as 0..1. Turn it into a useful mm/hr-like rate.
  if (raw <= 1.25) return raw * 18;
  return raw;
}

function cloudTileRainFeatures(payload) {
  const items = Array.isArray(payload?.items) ? payload.items : [];
  const out = [];
  for (const item of items) {
    const bounds = item?.bounds || {};
    const lat = toNumber(bounds.lat_center ?? item.lat, NaN);
    const lon = toNumber(bounds.lon_center ?? item.lon ?? item.lng, NaN);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
    const precipRate = normalizePrecipRate(item.precip_rate ?? item.estimated_precip_rate_mm_hr ?? item.precipitation_factor ?? item.rain_factor ?? item.precip);
    if (precipRate <= 0.03) continue;
    const low = densityPercent(item.low_density ?? item.cloud_low ?? item.bands?.low?.density);
    const mid = densityPercent(item.mid_density ?? item.cloud_mid ?? item.bands?.mid?.density);
    const high = densityPercent(item.high_density ?? item.cloud_high ?? item.bands?.high?.density);
    const total = densityPercent(item.coverage ?? item.cloud_total ?? Math.max(low, mid, high));
    const wind = item.wind || {};
    const windMid = wind.mid || wind.low || wind.high || {};
    out.push({
      lat,
      lon,
      precipRate,
      cloudTotal: total,
      layerMix: { low, mid, high },
      windU: toNumber(item.wind_u ?? windMid.u ?? wind.u ?? item.u, 0),
      windV: toNumber(item.wind_v ?? windMid.v ?? wind.v ?? item.v, 0),
      source: 'cloud_items_tiles',
    });
  }
  return out;
}

function cloudRegionRainFeatures(payload) {
  const regions = Array.isArray(payload?.cloud_regions) ? payload.cloud_regions : [];
  const out = [];
  for (const region of regions) {
    const inten = region?.intensity || {};
    const rainIntensity = toNumber(inten.rain ?? region?.rain_intensity ?? region?.precipitation_factor, 0);
    const precipRate = normalizePrecipRate(region?.precip_rate ?? region?.estimated_precip_rate_mm_hr ?? rainIntensity);
    if (precipRate <= 0.05) continue;
    const center = region?.center || {};
    const bbox = region?.bbox || {};
    const baseLat = toNumber(center.lat, (toNumber(bbox.south, 0) + toNumber(bbox.north, 0)) / 2);
    const baseLon = toNumber(center.lon ?? center.lng, (toNumber(bbox.west, 0) + toNumber(bbox.east, 0)) / 2);
    if (!Number.isFinite(baseLat) || !Number.isFinite(baseLon)) continue;
    const low = densityPercent(inten.low);
    const mid = densityPercent(inten.mid);
    const high = densityPercent(inten.high);
    const total = densityPercent(inten.total ?? Math.max(low, mid, high));
    const wind = region.wind || {};
    const seed = stableHashString(region.id || `${baseLat}:${baseLon}:${precipRate}`);
    const cellCount = Math.max(1, toNumber(region.cell_count, 1));
    const n = clamp(Math.round(1 + Math.sqrt(cellCount) / 3.5 + precipRate / 3.2), 1, RAIN_COLUMNS_PER_REGION_MAX);
    const latSpan = Math.max(0.01, Math.abs(toNumber(bbox.north, baseLat + 0.02) - toNumber(bbox.south, baseLat - 0.02)));
    const lonSpan = Math.max(0.01, Math.abs(toNumber(bbox.east, baseLon + 0.02) - toNumber(bbox.west, baseLon - 0.02)));
    const topAltitude = regionRainTopAltitude(region);
    for (let i = 0; i < n; i += 1) {
      const angle = Math.PI * 2 * rand01(seed, i + 1);
      const radius = Math.sqrt(rand01(seed, i + 41)) * 0.42;
      const lat = baseLat + Math.sin(angle) * latSpan * radius;
      const lon = baseLon + Math.cos(angle) * lonSpan * radius;
      out.push({
        lat,
        lon,
        precipRate,
        cloudTotal: total,
        layerMix: { low, mid, high },
        windU: toNumber(wind.u, 0),
        windV: toNumber(wind.v, 0),
        source: 'cloud_region_marching_squares',
        regionId: region.id,
        cloudType: region.cloud_type || region.family,
        topAltitude,
      });
    }
  }
  return out;
}

function makeRainTemplate({ rate, scale = 1, streakAngle = -10, lengthScale = 1, opacityBoost = 1 }) {
  const style = rainStyleForRate(rate);
  const uid = uniqueId('rain-drop');
  const tpl = document.createElement('template');
  const width = Math.round(style.size * clamp(scale, 0.72, 1.55));
  const height = Math.round(style.size * clamp(2.0 * lengthScale, 1.4, 3.3));
  const opacity = clamp(style.opacity * opacityBoost, 0.48, 0.98);
  tpl.innerHTML = `
    <svg width="${width}" height="${height}" viewBox="0 0 44 94" xmlns="http://www.w3.org/2000/svg" aria-hidden="true" style="pointer-events:none; overflow:visible">
      <defs>
        <radialGradient id="${uid}-mist" cx="50%" cy="70%" r="64%">
          <stop offset="0%" stop-color="${style.halo}" stop-opacity="0.22"/>
          <stop offset="100%" stop-color="${style.halo}" stop-opacity="0"/>
        </radialGradient>
        <linearGradient id="${uid}-streak" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#ffffff" stop-opacity="0.72"/>
          <stop offset="28%" stop-color="${style.core}" stop-opacity="0.96"/>
          <stop offset="70%" stop-color="${style.color}" stop-opacity="0.86"/>
          <stop offset="100%" stop-color="${style.color}" stop-opacity="0.18"/>
        </linearGradient>
      </defs>
      <g transform="rotate(${streakAngle.toFixed(1)} 22 47)" opacity="${opacity.toFixed(2)}">
        <ellipse cx="22" cy="71" rx="17" ry="19" fill="url(#${uid}-mist)"/>
        <path d="M22 5 C16 20 12 33 12 48 C12 58 16 67 22 72 C28 67 32 58 32 48 C32 33 28 20 22 5 Z" fill="url(#${uid}-streak)" stroke="${style.color}" stroke-opacity="0.38" stroke-width="1.2"/>
        <path d="M18 17 C15 28 15 39 16 50" stroke="#ffffff" stroke-width="2.7" stroke-linecap="round" opacity="0.48"/>
        <line x1="22" y1="-2" x2="22" y2="18" stroke="${style.color}" stroke-width="2.0" stroke-linecap="round" opacity="0.38"/>
      </g>
    </svg>`;
  return tpl;
}


function makeStormCoreTemplate(rate) {
  const style = rainStyleForRate(rate);
  const uid = uniqueId('storm-core');
  const size = rate >= 20 ? 74 : rate >= 7.5 ? 58 : 44;
  const tpl = document.createElement('template');
  tpl.innerHTML = `
    <svg width="${size}" height="${size}" viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg" aria-hidden="true" style="pointer-events:none;overflow:visible">
      <defs>
        <radialGradient id="${uid}" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stop-color="${style.core}" stop-opacity="0.58"/>
          <stop offset="42%" stop-color="${style.color}" stop-opacity="0.28"/>
          <stop offset="100%" stop-color="${style.halo}" stop-opacity="0"/>
        </radialGradient>
      </defs>
      <circle cx="50" cy="50" r="48" fill="url(#${uid})"/>
      <circle cx="50" cy="50" r="20" fill="${style.color}" opacity="0.16"/>
    </svg>`;
  return tpl;
}


function makeRainPresenceTemplate(rate) {
  const style = rainStyleForRate(rate);
  const uid = uniqueId('rain-presence');
  const size = rate >= 7.5 ? 34 : rate >= 2.5 ? 28 : 22;
  const tpl = document.createElement('template');
  tpl.innerHTML = `
    <svg width="${size}" height="${size}" viewBox="0 0 60 60" xmlns="http://www.w3.org/2000/svg" aria-hidden="true" style="pointer-events:none;overflow:visible">
      <defs>
        <radialGradient id="${uid}" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stop-color="${style.core}" stop-opacity="0.42"/>
          <stop offset="58%" stop-color="${style.color}" stop-opacity="0.20"/>
          <stop offset="100%" stop-color="${style.halo}" stop-opacity="0"/>
        </radialGradient>
      </defs>
      <circle cx="30" cy="30" r="28" fill="url(#${uid})"/>
      <circle cx="30" cy="30" r="8" fill="${style.color}" opacity="0.18"/>
    </svg>`;
  return tpl;
}

function createRainPresenceMarker({ lat, lon, rate, source = 'rain_presence' }) {
  const marker = document.createElement('gmp-marker-3d');
  const style = rainStyleForRate(rate);
  marker.position = { lat, lng: lon, altitude: 120 };
  marker.drawsWhenOccluded = true;
  marker.sizePreserved = true;
  marker.setAttribute('data-gfs-layer', 'rain');
  marker.setAttribute('data-gfs-sub-layer', 'rain-presence');
  marker.title = `Rain area ${style.label}: ${Number(rate).toFixed(2)} mm/hr`;
  marker.append(makeRainPresenceTemplate(rate));
  attachPolygonHover(marker, {
    title: `Rain area ${style.label}`,
    detail: `${lat.toFixed(3)}, ${lon.toFixed(3)}`,
    lines: [
      `Rain rate: ${Number(rate).toFixed(2)} mm/hr`,
      'Visual policy: all rain areas draw; droplet animation density adapts to device/zoom.',
    ],
    metrics: { layer: 'rain', source, kind: 'rain-presence' },
    payload: { lat, lon, rate_mm_hr: rate, label: style.label, kind: 'rain_presence' },
  });
  return marker;
}

function createStormCoreMarker({ lat, lon, altitude, rate, source = 'storm_core' }) {
  const marker = document.createElement('gmp-marker-3d');
  const style = rainStyleForRate(rate);
  marker.position = { lat, lng: lon, altitude };
  marker.drawsWhenOccluded = true;
  marker.sizePreserved = true;
  marker.setAttribute('data-gfs-layer', 'rain');
  marker.setAttribute('data-gfs-sub-layer', 'storm-core');
  marker.title = `Storm core ${style.label}: ${rate.toFixed(2)} mm/hr`;
  marker.append(makeStormCoreTemplate(rate));
  attachPolygonHover(marker, {
    title: `Storm core ${style.label}`,
    detail: `${lat.toFixed(3)}, ${lon.toFixed(3)}`,
    lines: [
      `Core rain rate: ${rate.toFixed(2)} mm/hr`,
      'Visual: storm-cell halo under falling streaks',
      'Use Lightning pill for electrical risk layer',
    ],
    metrics: { layer: 'rain', source, kind: 'storm-core' },
    payload: { lat, lon, altitude_m: altitude, rate_mm_hr: rate, label: style.label, kind: 'storm_core' },
  });
  return marker;
}

function createRainMarker({ lat, lon, altitude, rate, source = 'rain_column', cloudTotal = null, windU = 0, windV = 0, dropScale = 1, streakAngle = -10, lengthScale = 1, opacityBoost = 1, parentRate = null }) {
  const marker = document.createElement('gmp-marker-3d');
  const style = rainStyleForRate(rate);
  marker.position = { lat, lng: lon, altitude };
  marker.drawsWhenOccluded = true;
  marker.sizePreserved = true;
  marker.setAttribute('data-gfs-layer', 'rain');
  marker.setAttribute('data-rain-rate', rate.toFixed(2));
  marker.title = `Rain ${style.label}: ${rate.toFixed(2)} mm/hr`;
  marker.append(makeRainTemplate({ rate, scale: dropScale, streakAngle, lengthScale, opacityBoost }));
  attachPolygonHover(marker, {
    title: `Rain ${style.label} streak`,
    detail: `${lat.toFixed(3)}, ${lon.toFixed(3)} @ ${Math.round(altitude)} m`,
    lines: [
      `Drop rate: ${rate.toFixed(2)} mm/hr`,
      Number.isFinite(Number(parentRate)) ? `Cell rate: ${Number(parentRate).toFixed(2)} mm/hr` : null,
      Number.isFinite(Number(cloudTotal)) ? `Cloud total: ${Number(cloudTotal).toFixed(0)}%` : null,
      `Wind U/V: ${Number(windU || 0).toFixed(2)} / ${Number(windV || 0).toFixed(2)} m/s`,
      'Style: radar color scale with per-drop intensity variation',
    ].filter(Boolean),
    metrics: { layer: 'rain', source },
    payload: { lat, lon, altitude_m: altitude, rate_mm_hr: rate, cell_rate_mm_hr: parentRate, label: style.label, cloud_total: cloudTotal, wind_u: windU, wind_v: windV },
  });
  return marker;
}

function buildRainColumn({ lat, lon, precipRate, cloudTotal, layerMix, windU = 0, windV = 0, source = 'rain_column', topAltitudeOverride = null, surfaceAltitudeOverride = null, seedHint = '' }) {
  const cloudAltitudes = estimateCloudColumnAltitudes(cloudTotal, layerMix);
  const topOverride = Number(topAltitudeOverride);
  const surfaceOverride = Number(surfaceAltitudeOverride);
  const topRaw = Number.isFinite(topOverride) && topOverride > 0 ? topOverride : cloudAltitudes.cloudTopAltitude - 180;
  const bottomRaw = Number.isFinite(surfaceOverride) && surfaceOverride >= 0 ? surfaceOverride : cloudAltitudes.cloudBaseAltitude * 0.16;
  const topAltitude = Math.max(900, Math.round(topRaw));
  const bottomAltitude = Math.max(60, Math.round(bottomRaw));
  const drops = clamp(Math.round(2 + (precipRate * 0.55) + Math.sqrt(Math.max(0, cloudTotal || 0)) * 0.055), 2, 6);
  const spacing = (topAltitude - bottomAltitude) / Math.max(1, drops - 1);
  const baseSpread = 0.006 + Math.min(0.055, precipRate * 0.018);
  const windMag = Math.hypot(toNumber(windU, 0), toNumber(windV, 0));
  const windAngle = Math.atan2(toNumber(windV, 0), toNumber(windU, 0));
  const seed = stableHashString(`${seedHint || source}:${lat.toFixed(4)}:${lon.toFixed(4)}:${precipRate.toFixed(2)}`);
  const markers = [];
  for (let idx = 0; idx < drops; idx += 1) {
    const phase = (idx + rand01(seed, idx + 71) * 0.74) / Math.max(1, drops);
    const theta = (Math.PI * 2 * rand01(seed, idx + 11)) + (windMag > 0.6 ? windAngle * 0.25 : 0);
    const radius = Math.pow(rand01(seed, idx + 29), 0.72);
    const bandNoise = 0.58 + rand01(seed, idx + 37) * 0.95;
    const ellipseX = baseSpread * (0.42 + Math.min(0.58, windMag / 20));
    const ellipseY = baseSpread * (0.28 + rand01(seed, idx + 53) * 0.56);
    const windSkewLat = metersToLatDegrees(toNumber(windV, 0) * (idx / Math.max(1, drops - 1)) * 28);
    const windSkewLon = metersToLonDegrees(toNumber(windU, 0) * (idx / Math.max(1, drops - 1)) * 28, lat);
    // Random elliptical scatter avoids the old perfect ring/circle look.
    const latOffset = (Math.sin(theta) * ellipseY * radius * bandNoise) + windSkewLat;
    const lonOffset = (Math.cos(theta) * ellipseX * radius * bandNoise) + windSkewLon;
    const localRate = clamp(precipRate * (RAIN_COLOR_JITTER_MIN + rand01(seed, idx + 97) * (RAIN_COLOR_JITTER_MAX - RAIN_COLOR_JITTER_MIN)), 0.04, Math.max(0.08, precipRate * 1.65));
    const dropScale = clamp(0.72 + rand01(seed, idx + 131) * 0.78 + Math.min(0.35, precipRate / 28), 0.68, 1.68);
    const lengthScale = clamp(0.78 + (windMag / 32) + rand01(seed, idx + 139) * 0.55, 0.74, 1.85);
    const streakAngle = clamp(-6 - (windMag * 1.7) + ((rand01(seed, idx + 151) - 0.5) * 20), -48, 28);
    markers.push({
      lat: lat + latOffset,
      lon: lon + lonOffset,
      anchorLat: lat + latOffset,
      anchorLon: lon + lonOffset,
      altitude: Math.round(topAltitude - (spacing * idx)),
      topAltitude,
      bottomAltitude,
      fallPhase: phase % 1,
      rate: localRate,
      parentRate: precipRate,
      windU: toNumber(windU, 0),
      windV: toNumber(windV, 0),
      cloudTotal,
      source,
      dropScale,
      lengthScale,
      streakAngle,
      opacityBoost: 0.86 + rand01(seed, idx + 163) * 0.28,
      latOffsetDeg: 0,
      lonOffsetDeg: 0,
    });
  }
  return markers;
}

function startFrameBatch({ queue, map3DElement, created, advected }) {
  let rafId = null;
  let disposed = false;

  const pump = () => {
    if (disposed) return;
    let injected = 0;
    const frag = document.createDocumentFragment();
    const batch = Math.max(24, Math.min(96, Number(window.GFS_RAIN_MARKERS_PER_FRAME || 72)));
    while (queue.length && injected < batch) {
      const item = queue.shift();
      const marker = createRainMarker(item);
      item.marker = marker;
      frag.append(marker);
      created.push(marker);
      advected.push(item);
      injected += 1;
    }
    if (injected) map3DElement.append(frag);
    if (queue.length) rafId = requestAnimationFrame(pump);
  };

  rafId = requestAnimationFrame(pump);
  return () => {
    disposed = true;
    if (rafId) cancelAnimationFrame(rafId);
  };
}

function startRainAdvection(items) {
  if (!items.length) return () => {};
  let rafId = 0;
  let stopped = false;
  let lastTs = 0;
  let lastDrawTs = 0;

  const tick = (ts) => {
    if (stopped) return;
    if (!lastTs) lastTs = ts;
    if (lastDrawTs && (ts - lastDrawTs) < RAIN_ADVECTION_INTERVAL_MS) {
      rafId = requestAnimationFrame(tick);
      return;
    }
    lastDrawTs = ts;
    const dtSec = Math.min(MAX_ADVECTION_STEP_SEC, Math.max(0.01, (ts - lastTs) * 0.001));
    lastTs = ts;
    for (const item of items) {
      if (!item.marker) continue;
      item.latOffsetDeg += metersToLatDegrees(item.windV * dtSec);
      item.lonOffsetDeg += metersToLonDegrees(item.windU * dtSec, item.anchorLat + item.latOffsetDeg);
      item.fallPhase = (item.fallPhase + ((RAIN_FALL_SPEED_MPS * dtSec) / Math.max(120, item.topAltitude - item.bottomAltitude))) % 1;
      const altitude = item.topAltitude - ((item.topAltitude - item.bottomAltitude) * item.fallPhase);
      item.marker.position = {
        lat: item.anchorLat + item.latOffsetDeg,
        lng: wrapLongitude(item.anchorLon + item.lonOffsetDeg),
        altitude: Math.max(item.bottomAltitude, Math.round(altitude)),
      };
    }
    rafId = requestAnimationFrame(tick);
  };

  rafId = requestAnimationFrame(tick);
  return () => {
    stopped = true;
    if (rafId) cancelAnimationFrame(rafId);
  };
}


function trimRainColumns(columns, maxColumns = MAX_COLUMNS) {
  if (!Array.isArray(columns) || columns.length <= maxColumns) return columns;
  columns.sort((a, b) => Number(b.precipRate || 0) - Number(a.precipRate || 0));
  return columns.slice(0, maxColumns);
}

function trimRainQueue(queue, maxMarkers = MAX_MARKERS_PER_FRAME) {
  if (!Array.isArray(queue) || queue.length <= maxMarkers) return queue;
  queue.sort((a, b) => Number(b.parentRate || b.rate || 0) - Number(a.parentRate || a.rate || 0));
  const kept = queue.slice(0, maxMarkers);
  queue.length = 0;
  queue.push(...kept);
  return queue;
}

function isDrawableViewportReason(reason) {
  const r = String(reason || 'steady').toLowerCase();
  return r === 'boot' || r === 'steady' || r === 'settled' || r === 'manual' || r === 'refresh' || r.includes('steady') || r.includes('settled') || r.includes('update') || r.includes('deferred');
}

export function renderRainZones({ payload, map3DElement, viewportReason = 'steady' }) {
  const created = [];
  const advected = [];
  if (!map3DElement || !payload) return () => {};
  console.info('[gfs rain] polygon api', { api: polygonApiPath() });
  if (!isDrawableViewportReason(viewportReason)) {
    console.info('[gfs rain] hold visible layer during non-draw reason', { reason: viewportReason });
    return () => {};
  }

  const bbox = bboxFromPayload(payload);
  if (!bbox) return () => {};

  const queue = [];
  const columns = [];
  let source = 'none';
  const contractFeatures = Array.isArray(payload?.polygon_field_v1?.features) ? payload.polygon_field_v1.features : [];
  const windUGrid = to2DGrid(payload?.fields?.wind_u);
  const windVGrid = to2DGrid(payload?.fields?.wind_v);
  const precipColumns = Array.isArray(payload?.precip_columns) && payload.precip_columns.length
    ? payload.precip_columns
    : (Array.isArray(payload?.scene?.precip) ? payload.scene.precip : []);
  const regionFeatures = cloudRegionRainFeatures(payload);
  const tileFeatures = cloudTileRainFeatures(payload);

  const preferDerivedContours = payload?.prefer_derived_contours === true || payload?.derived_render_geometry === true;

  if (preferDerivedContours && contractFeatures.length) {
    source = 'scene_cache_engine_derived_contours';
    const count = contractFeatures.length;
    for (let i = 0; i < count; i += 1) {
      const f = contractFeatures[i]?.properties || contractFeatures[i] || {};
      const precipRate = normalizePrecipRate(f.precip_rate ?? f.rain_rate ?? f.precip);
      if (precipRate <= 0.03) continue;
      const lat = toNumber(f.lat, NaN);
      const lon = toNumber(f.lon, NaN);
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
      const columnMarkers = buildRainColumn({
        lat,
        lon,
        precipRate,
        cloudTotal: Math.max(0, toNumber(f.cloud_total, 0)),
        layerMix: { low: toNumber(f.cloud_low, 0), mid: toNumber(f.cloud_mid, 0), high: toNumber(f.cloud_high, 0) },
        windU: sampleGridBilinear(windUGrid, bbox, lat, lon),
        windV: sampleGridBilinear(windVGrid, bbox, lat, lon),
        source: 'scene_cache_engine_derived_contours',
        seedHint: f.id || f.key || `${lat}:${lon}`,
      });
      queue.push(...columnMarkers);
      columns.push({ lat, lon, precipRate, drops: columnMarkers.length });

    }
  } else if (regionFeatures.length) {
    source = 'cloud_regions_marching_squares';
    const count = regionFeatures.length;
    for (let i = 0; i < count; i += 1) {
      const f = regionFeatures[i];
      const columnMarkers = buildRainColumn({
        lat: f.lat,
        lon: f.lon,
        precipRate: f.precipRate,
        cloudTotal: f.cloudTotal,
        layerMix: f.layerMix,
        windU: f.windU,
        windV: f.windV,
        source: f.source,
        topAltitudeOverride: f.topAltitude,
        seedHint: f.regionId || `${f.lat}:${f.lon}`,
      });
      queue.push(...columnMarkers);
      columns.push({ lat: f.lat, lon: f.lon, precipRate: f.precipRate, drops: columnMarkers.length, regionId: f.regionId });

    }
  } else if (precipColumns.length) {
    source = 'precip_columns';
    const count = precipColumns.length;
    for (let i = 0; i < count; i += 1) {
      const f = precipColumns[i] || {};
      const lat = toNumber(f.lat ?? f.center?.lat, NaN);
      const lon = toNumber(f.lon ?? f.lng ?? f.center?.lon, NaN);
      const precipRate = normalizePrecipRate(f.estimated_precip_rate_mm_hr ?? f.precip_rate ?? f.rate ?? f.intensity_mm_hr);
      if (!Number.isFinite(lat) || !Number.isFinite(lon) || precipRate <= 0.03) continue;
      const cloudTotal = Math.max(0, toNumber(f.cloud_total ?? f.coverage, 60));
      const columnMarkers = buildRainColumn({
        lat,
        lon,
        precipRate,
        cloudTotal,
        layerMix: { low: cloudTotal * 0.52, mid: cloudTotal * 0.38, high: cloudTotal * 0.24 },
        windU: toNumber(f.wind_u, sampleGridBilinear(windUGrid, bbox, lat, lon) ?? 0),
        windV: toNumber(f.wind_v, sampleGridBilinear(windVGrid, bbox, lat, lon) ?? 0),
        source: 'precip_columns',
        seedHint: f.id || f.key || `${lat}:${lon}`,
        topAltitudeOverride: toNumber(f.estimated_top_altitude_m ?? f.top_altitude_m ?? f.cloud_top_altitude_m, null),
        surfaceAltitudeOverride: toNumber(f.estimated_surface_altitude_m ?? f.surface_altitude_m, null),
      });
      queue.push(...columnMarkers);
      columns.push({ lat, lon, precipRate, drops: columnMarkers.length });

    }
  } else if (contractFeatures.length) {
    source = 'polygon_field_v1';
    const count = contractFeatures.length;
    for (let i = 0; i < count; i += 1) {
      const f = contractFeatures[i]?.properties || contractFeatures[i] || {};
      const precipRate = normalizePrecipRate(f.precip_rate ?? f.rain_rate ?? f.precip);
      if (precipRate <= 0.03) continue;
      const lat = toNumber(f.lat, NaN);
      const lon = toNumber(f.lon, NaN);
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
      const columnMarkers = buildRainColumn({
        lat,
        lon,
        precipRate,
        cloudTotal: Math.max(0, toNumber(f.cloud_total, 0)),
        layerMix: { low: toNumber(f.cloud_low, 0), mid: toNumber(f.cloud_mid, 0), high: toNumber(f.cloud_high, 0) },
        windU: sampleGridBilinear(windUGrid, bbox, lat, lon),
        windV: sampleGridBilinear(windVGrid, bbox, lat, lon),
        source: 'polygon_field_v1',
        seedHint: f.id || f.key || `${lat}:${lon}`,
      });
      queue.push(...columnMarkers);
      columns.push({ lat, lon, precipRate, drops: columnMarkers.length });

    }
  } else if (tileFeatures.length) {
    source = 'cloud_items_tiles';
    const count = tileFeatures.length;
    for (let i = 0; i < count; i += 1) {
      const f = tileFeatures[i];
      const columnMarkers = buildRainColumn({
        lat: f.lat,
        lon: f.lon,
        precipRate: f.precipRate,
        cloudTotal: f.cloudTotal,
        layerMix: f.layerMix,
        windU: f.windU,
        windV: f.windV,
        source: f.source || 'cloud_items_tiles',
        seedHint: f.id || `${f.lat}:${f.lon}`,
      });
      queue.push(...columnMarkers);
      columns.push({ lat: f.lat, lon: f.lon, precipRate: f.precipRate, drops: columnMarkers.length });

    }
  } else {
    source = 'fields_grid';
    const precip = to2DGrid(payload?.fields?.precip_rate || payload?.fields?.prate);
    const clouds = to2DGrid(payload?.fields?.cloud_total);
    if (!precip.length) {
      console.info('[gfs rain] suppressed render', { reason: 'no_precip_renderable_payload', payloadKeys: Object.keys(payload || {}) });
      return () => {};
    }
    const ny = precip.length;
    const nx = Array.isArray(precip[0]) ? precip[0].length : 0;
    const step = Math.max(1, Math.floor(Math.max(nx, ny) / 14));
    for (let i = 0; i < ny; i += step) {
      for (let j = 0; j < nx; j += step) {
        const precipRate = normalizePrecipRate(precip?.[i]?.[j]);
        if (precipRate <= 0.03) continue;
        const { lat, lon } = latLonFromIndex(i, j, ny, nx, bbox);
        const cloudTotal = Math.max(0, toNumber(clouds?.[i]?.[j], 0));
        const columnMarkers = buildRainColumn({
          lat,
          lon,
          precipRate,
          cloudTotal,
          layerMix: { low: cloudTotal * 0.55, mid: cloudTotal * 0.4, high: cloudTotal * 0.2 },
          windU: sampleGridBilinear(windUGrid, bbox, lat, lon),
          windV: sampleGridBilinear(windVGrid, bbox, lat, lon),
          source: 'fields_grid',
          seedHint: `${i}:${j}:${lat.toFixed(3)}:${lon.toFixed(3)}`,
        });
        queue.push(...columnMarkers);
        columns.push({ lat, lon, precipRate, drops: columnMarkers.length });

      }

    }
  }

  // Cheap rain policy: keep all layers truthful but cap expensive markers.
  // Presence markers show the strongest rain cells; falling streaks animate only
  // a priority subset so rain is cheaper than clouds/bait/boats.
  if (columns.length > MAX_COLUMNS) {
    const keptColumns = trimRainColumns(columns, MAX_COLUMNS);
    columns.length = 0;
    columns.push(...keptColumns);
  }
  trimRainQueue(queue, Math.min(MAX_MARKERS_PER_FRAME, RAIN_ANIMATION_COLUMNS_PER_VIEW_SOFT_MAX));

  if (columns.length) {
    const presenceFrag = document.createDocumentFragment();
    for (const c of columns) {
      const el = createRainPresenceMarker({ lat: c.lat, lon: c.lon, rate: Number(c.precipRate || 0), source });
      if (!el) continue;
      presenceFrag.append(el);
      created.push(el);
    }
    map3DElement.append(presenceFrag);
  }

  trimRainQueue(queue, Math.min(MAX_MARKERS_PER_FRAME, RAIN_ANIMATION_COLUMNS_PER_VIEW_SOFT_MAX));

  const stormCores = columns
    .filter((c) => Number(c.precipRate) >= 7.5)
    .slice(0, Number(window.GFS_RAIN_HOVER_SAMPLE_MAX || 12))
    .map((c) => createStormCoreMarker({ lat: c.lat, lon: c.lon, altitude: 220, rate: Number(c.precipRate), source }))
    .filter(Boolean);
  if (stormCores.length) {
    const stormFrag = document.createDocumentFragment();
    stormCores.forEach((el) => { stormFrag.append(el); created.push(el); });
    map3DElement.append(stormFrag);
  }

  const stopBatch = startFrameBatch({ queue, map3DElement, created, advected });
  const stopAdvection = startRainAdvection(advected);
  try { window.__gfsDebugEvent?.('storm-fx/render', { columns: columns.length, drops: queue.length, stormCores: stormCores.length, source }); } catch (_) {}
  console.info('[gfs rain] queued columns', { columns: columns.length, markers: queue.length, stormCores: stormCores.length, source, batchSize: Number(window.GFS_RAIN_MARKERS_PER_FRAME || 72), mode: 'cheap_priority_rain_presence_plus_capped_droplet_animation' });
  return () => {
    stopBatch();
    stopAdvection();
    created.forEach((el) => { try { el.remove(); } catch (_) {} });
  };
}
