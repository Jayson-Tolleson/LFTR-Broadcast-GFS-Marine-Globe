import { normalizePolygonFieldPayload } from './polygon_math.js';
import { clamp01 } from './greek_math.js';
import { createPolygon3D } from './polygon3d.js';
import { contourPolygonsFromPoints } from './marching_squares.js';

const MAX_POLYGONS_PER_FRAME = 80; // reconciled bait batch budget; local/harbor can still draw detail without blocking frames
const MAX_BAIT_POLYGONS_WORLD = Number(window.GFS_BAIT_MAX_POLYGONS_WORLD || 48);
const MAX_BAIT_POLYGONS_REGIONAL = Number(window.GFS_BAIT_MAX_POLYGONS_REGIONAL || 80);
const MAX_BAIT_POLYGONS_LOCAL = Number(window.GFS_BAIT_MAX_POLYGONS_LOCAL || 120);
const BAIT_PULSE_INTERVAL_MS = Number(window.GFS_BAIT_PULSE_INTERVAL_MS || 250);
const BAIT_FADE_STEP = Number(window.GFS_BAIT_FADE_STEP || 0.08);
const BAIT_DRIFT_DEFAULT_TIME_SCALE = 420; // simulated seconds per real second; keeps schools visibly alive without teleporting.
const BAIT_DRIFT_MAX_STEP_DEG = 0.018;
// Keep false by default: Google Maps 3D beta can stringify post-append polygon.path
// mutations into a `path` attribute, causing huge LatLngAltitude parse errors.
// Bait still gets score-based opacity and U/V vectors in debug; live drift can be
// re-enabled from the console only after the path setter is verified safe.
const BAIT_MUTATE_PATHS_DEFAULT = false;
const EARTH_METERS_PER_DEG_LAT = 111320;

function polygonApiPath() {
  return window.google?.maps?.maps3d?.Polygon3DElement ? 'Polygon3DElement.path' : 'gmp-polygon-3d.path';
}

function toNumber(v, fallback = 0) {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

function wrapLongitude(lon) {
  const n = Number(lon);
  if (!Number.isFinite(n)) return n;
  return ((((n + 180) % 360) + 360) % 360) - 180;
}

function normalizeBbox(bbox) {
  if (Array.isArray(bbox) && bbox.length >= 4) {
    const [west, south, east, north] = bbox.map(Number);
    if ([west, south, east, north].every(Number.isFinite)) return { west, south, east, north };
  }
  if (bbox && typeof bbox === 'object') {
    const west = Number(bbox.west); const south = Number(bbox.south); const east = Number(bbox.east); const north = Number(bbox.north);
    if ([west, south, east, north].every(Number.isFinite)) return { west, south, east, north };
  }
  return null;
}

function pointNearBbox(lat, lng, bbox, pad = 0.75) {
  const b = normalizeBbox(bbox);
  if (!b) return true;
  if (lat < Math.min(b.south, b.north) - pad || lat > Math.max(b.south, b.north) + pad) return false;
  const x = wrapLongitude(lng);
  const west = wrapLongitude(b.west - pad);
  const east = wrapLongitude(b.east + pad);
  return west <= east ? (x >= west && x <= east) : (x >= west || x <= east);
}

function pathBbox(path) {
  let minLat = Infinity; let maxLat = -Infinity; let minLng = Infinity; let maxLng = -Infinity;
  for (const p of Array.isArray(path) ? path : []) {
    const lat = Number(p?.lat);
    const lng = Number(p?.lng);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) continue;
    minLat = Math.min(minLat, lat); maxLat = Math.max(maxLat, lat);
    minLng = Math.min(minLng, lng); maxLng = Math.max(maxLng, lng);
  }
  return [minLat, maxLat, minLng, maxLng].every(Number.isFinite) ? { minLat, maxLat, minLng, maxLng } : null;
}

function pathLooksLikeViewportBait(path, bbox = null) {
  if (!Array.isArray(path) || path.length < 3) return false;
  const b = normalizeBbox(bbox);
  const pb = pathBbox(path);
  if (!pb) return false;
  if (!b) return true;
  const spanLat = Math.max(0.0001, Math.abs(b.north - b.south));
  const spanLng = Math.max(0.0001, Math.abs(b.east - b.west));
  // Reject detached chlorophyll/parabola artifacts. True bait polygons should
  // stay inside/near the requested payload bbox; points/axis errors often create
  // long curved shapes that extend far outside the viewport or have huge jumps.
  const padLat = Math.max(0.15, spanLat * 0.22);
  const padLng = Math.max(0.15, spanLng * 0.22);
  if (pb.minLat < Math.min(b.south, b.north) - padLat || pb.maxLat > Math.max(b.south, b.north) + padLat) return false;
  const west = Math.min(b.west, b.east) - padLng;
  const east = Math.max(b.west, b.east) + padLng;
  if (pb.minLng < west || pb.maxLng > east) return false;
  const maxJumpLat = Math.max(0.35, spanLat * 0.55);
  const maxJumpLng = Math.max(0.35, spanLng * 0.55);
  for (let i = 1; i < path.length; i += 1) {
    if (Math.abs(path[i].lat - path[i - 1].lat) > maxJumpLat || Math.abs(path[i].lng - path[i - 1].lng) > maxJumpLng) return false;
  }
  return true;
}

function clampProbability(value) {
  return clamp01(toNumber(value, 0));
}

function toHexByte(value) {
  const clamped = Math.max(0, Math.min(255, Math.round(value)));
  return clamped.toString(16).padStart(2, '0');
}

function rgbHex(r, g, b) {
  return `#${toHexByte(r)}${toHexByte(g)}${toHexByte(b)}`;
}

function interpolateColor(a, b, t) {
  const p = clampProbability(t);
  return {
    r: a.r + ((b.r - a.r) * p),
    g: a.g + ((b.g - a.g) * p),
    b: a.b + ((b.b - a.b) * p),
  };
}

function probabilityBaseColor(probability) {
  const p = clampProbability(probability);
  // Water-first bait palette: weak = aqua/green, medium = yellow/orange, hot = magenta/red.
  const aqua = { r: 64, g: 231, b: 255 };
  const green = { r: 64, g: 214, b: 92 };
  const yellow = { r: 255, g: 232, b: 64 };
  const orange = { r: 255, g: 147, b: 43 };
  const magenta = { r: 255, g: 45, b: 171 };
  if (p < 0.30) return interpolateColor(aqua, green, p / 0.30);
  if (p < 0.58) return interpolateColor(green, yellow, (p - 0.30) / 0.28);
  if (p < 0.78) return interpolateColor(yellow, orange, (p - 0.58) / 0.20);
  return interpolateColor(orange, magenta, (p - 0.78) / 0.22);
}

function probabilityColorRamp(probability) {
  const base = probabilityBaseColor(probability);
  const toLayerHex = (mix) => rgbHex(
    base.r + ((255 - base.r) * mix),
    base.g + ((255 - base.g) * mix),
    base.b + ((255 - base.b) * mix),
  );
  return {
    coreColor: toLayerHex(0),
    innerColor: toLayerHex(0.16),
    outerColor: toLayerHex(0.38),
  };
}

function baitOpacity(probability, band) {
  const p = clampProbability(probability);
  const p2 = p * p;
  if (band === 'outer') return clampProbability(0.035 + (0.26 * p2));
  if (band === 'inner') return clampProbability(0.055 + (0.42 * p2));
  return clampProbability(0.09 + (0.68 * p2));
}

function baitStrokeOpacity(probability, band) {
  const p = clampProbability(probability);
  const base = band === 'outer' ? 0.12 : band === 'inner' ? 0.22 : 0.36;
  return clampProbability(base + (0.48 * p));
}


function schoolScore(row) {
  return clampProbability(
    row?.schoolScore
    ?? row?.school_score
    ?? row?.baitSchoolScore
    ?? row?.school
    ?? row?.probability
    ?? row?.baitScore
    ?? row?.bait_score
    ?? row?.score
    ?? row?.confidence
    ?? 0.0,
  );
}

function vectorComponent(row, names) {
  for (const name of names) {
    const n = Number(row?.[name]);
    if (Number.isFinite(n)) return n;
  }
  return NaN;
}

function currentVectorFromRow(row) {
  if (!row || typeof row !== 'object') return null;
  const u = vectorComponent(row, ['u', 'u_ms', 'uMps', 'u_mps', 'current_u', 'currentU', 'eastward_current', 'water_u', 'ucur', 'ugos']);
  const v = vectorComponent(row, ['v', 'v_ms', 'vMps', 'v_mps', 'current_v', 'currentV', 'northward_current', 'water_v', 'vcur', 'vgos']);
  if (Number.isFinite(u) && Number.isFinite(v) && Math.hypot(u, v) > 0.001) return { u, v, source: row.source || 'row_uv' };

  const speed = vectorComponent(row, ['speed', 'speed_ms', 'current_speed', 'currentSpeed', 'current_speed_ms']);
  const dir = vectorComponent(row, ['dir', 'direction', 'dirDeg', 'direction_deg', 'current_dir', 'currentDirectionDeg']);
  if (Number.isFinite(speed) && Number.isFinite(dir) && speed > 0.001) {
    const rad = (dir * Math.PI) / 180;
    return { u: Math.sin(rad) * speed, v: Math.cos(rad) * speed, source: row.source || 'row_speed_dir' };
  }
  return null;
}

function averageCurrentVector(rows) {
  let uSum = 0;
  let vSum = 0;
  let wSum = 0;
  for (const row of Array.isArray(rows) ? rows : []) {
    const vec = currentVectorFromRow(row);
    if (!vec) continue;
    const w = Math.max(0.08, schoolScore(row));
    uSum += vec.u * w;
    vSum += vec.v * w;
    wSum += w;
  }
  if (wSum <= 0) return { u: 0, v: 0, source: 'no_uv_available' };
  return { u: uSum / wSum, v: vSum / wSum, source: 'weighted_payload_uv' };
}

function motionVectorForPolygon(poly, fallbackVector) {
  return currentVectorFromRow(poly) || fallbackVector || { u: 0, v: 0, source: 'no_uv_available' };
}

function centroidOfPath(path) {
  let lat = 0;
  let lng = 0;
  let count = 0;
  for (const p of path || []) {
    const la = Number(p?.lat);
    const ln = Number(p?.lng);
    if (!Number.isFinite(la) || !Number.isFinite(ln)) continue;
    lat += la;
    lng += ln;
    count += 1;
  }
  return count ? { lat: lat / count, lng: lng / count } : { lat: 0, lng: 0 };
}

function metersToLatLngDelta({ eastMeters, northMeters, lat }) {
  const safeLat = Number.isFinite(lat) ? lat : 0;
  const cosLat = Math.max(0.12, Math.cos((safeLat * Math.PI) / 180));
  return {
    dLat: northMeters / EARTH_METERS_PER_DEG_LAT,
    dLng: eastMeters / (EARTH_METERS_PER_DEG_LAT * cosLat),
  };
}

function clampStep(value) {
  return Math.max(-BAIT_DRIFT_MAX_STEP_DEG, Math.min(BAIT_DRIFT_MAX_STEP_DEG, value));
}



function baitExtrudeHeight(probability, band) {
  const p = clampProbability(probability);
  if (band === 'outer') return 8 + (18 * p);
  if (band === 'inner') return 16 + (34 * p);
  return 28 + (58 * p);
}

function baitDepthFt(poly) {
  const direct = Number(poly?.bait_depth_ft ?? poly?.preferred_bait_depth_ft ?? poly?.depth_ft);
  if (Number.isFinite(direct) && direct > 0) return direct;
  const d = poly?.depth_intel || poly?.depth || {};
  const nested = Number(d?.preferred_bait_depth_ft ?? d?.bait_depth_ft ?? d?.target_depth_ft);
  if (Number.isFinite(nested) && nested > 0) return nested;
  const band = Array.isArray(poly?.bait_depth_band_ft) ? poly.bait_depth_band_ft : (Array.isArray(d?.bait_depth_band_ft) ? d.bait_depth_band_ft : null);
  if (band && Number.isFinite(Number(band[0])) && Number.isFinite(Number(band[1]))) return (Number(band[0]) + Number(band[1])) * 0.5;
  const bottom = Number(poly?.bottom_depth_ft ?? d?.bottom_depth_ft);
  if (Number.isFinite(bottom) && bottom > 0) return Math.max(3, Math.min(bottom * 0.42, 85));
  return NaN;
}

function baitAltitudeFromDepth(poly, fallback = 20) {
  const ft = baitDepthFt(poly);
  if (!Number.isFinite(ft)) return fallback;
  // Visualize bait depth as an above-water extrusion column height. Google 3D
  // polygons cannot extrude below the water plane, so depth is encoded upward.
  return Math.max(8, Math.min(260, ft * 0.62));
}

function baitExtrudeHeightForPolygon(poly, probability, band) {
  const depthAlt = baitAltitudeFromDepth(poly, NaN);
  const pHeight = baitExtrudeHeight(probability, band);
  if (!Number.isFinite(depthAlt)) return pHeight;
  const bandScale = band === 'outer' ? 0.38 : (band === 'inner' ? 0.62 : 0.88);
  return Math.max(8, Math.min(320, depthAlt * bandScale + pHeight * 0.35));
}

function sanitizePath(path) {
  const cleaned = [];
  for (const point of path || []) {
    const lat = Number(point?.lat);
    const lng = Number(point?.lng);
    const altitude = Number(point?.altitude ?? 20);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) continue;
    const prev = cleaned[cleaned.length - 1];
    if (prev && Math.abs(prev.lat - lat) < 1e-6 && Math.abs(prev.lng - lng) < 1e-6) continue;
    cleaned.push({ lat, lng, altitude: Number.isFinite(altitude) ? altitude : 20 });
  }
  if (cleaned.length >= 2) {
    const first = cleaned[0];
    const last = cleaned[cleaned.length - 1];
    if (Math.abs(first.lat - last.lat) < 1e-6 && Math.abs(first.lng - last.lng) < 1e-6) {
      cleaned.pop();
    }
  }
  return cleaned;
}

function toPath(coords, altitude = 20, bbox = null) {
  if (!Array.isArray(coords)) return [];
  const mapped = coords.map((p) => {
    if (Array.isArray(p) && p.length >= 2) {
      const a = Number(p[0]);
      const b = Number(p[1]);
      if (!Number.isFinite(a) || !Number.isFinite(b)) return null;
      // GeoJSON is [lon, lat], but some chlorophyll-derived payloads arrive as [lat, lon]
      // or 0..360 longitude. Pick the interpretation that lands inside the payload bbox.
      const lonLat = { lat: b, lng: wrapLongitude(a), altitude };
      const latLon = { lat: a, lng: wrapLongitude(b), altitude };
      const lonLatOk = lonLat.lat >= -90 && lonLat.lat <= 90 && pointNearBbox(lonLat.lat, lonLat.lng, bbox);
      const latLonOk = latLon.lat >= -90 && latLon.lat <= 90 && pointNearBbox(latLon.lat, latLon.lng, bbox);
      if (lonLatOk && !latLonOk) return lonLat;
      if (latLonOk && !lonLatOk) return latLon;
      if (lonLatOk) return lonLat;
      if (latLonOk) return latLon;
      return lonLat.lat >= -90 && lonLat.lat <= 90 ? lonLat : (latLon.lat >= -90 && latLon.lat <= 90 ? latLon : null);
    }
    if (p && typeof p === 'object') {
      let lat = Number(p.lat ?? p.latitude);
      let lng = Number(p.lng ?? p.lon ?? p.longitude);
      if (!Number.isFinite(lat) || !Number.isFinite(lng)) return null;
      if ((lat < -90 || lat > 90) && lng >= -90 && lng <= 90) { const tmp = lat; lat = lng; lng = tmp; }
      lng = wrapLongitude(lng);
      if (lat < -90 || lat > 90 || !pointNearBbox(lat, lng, bbox)) return null;
      return { lat, lng, altitude: toNumber(p.altitude, altitude) };
    }
    return null;
  }).filter(Boolean);
  return sanitizePath(mapped);
}

function ringAreaScore(ring) {
  if (!Array.isArray(ring) || ring.length < 3) return -1;
  let area = 0;
  let valid = 0;
  for (let i = 0; i < ring.length; i += 1) {
    const a = ring[i];
    const b = ring[(i + 1) % ring.length];
    const ax = Array.isArray(a) ? Number(a[0]) : Number(a?.lng ?? a?.lon ?? a?.longitude);
    const ay = Array.isArray(a) ? Number(a[1]) : Number(a?.lat ?? a?.latitude);
    const bx = Array.isArray(b) ? Number(b[0]) : Number(b?.lng ?? b?.lon ?? b?.longitude);
    const by = Array.isArray(b) ? Number(b[1]) : Number(b?.lat ?? b?.latitude);
    if (![ax, ay, bx, by].every(Number.isFinite)) continue;
    area += ax * by - bx * ay;
    valid += 1;
  }
  return valid >= 3 ? Math.abs(area) : -1;
}

function flattenPolygonCoordinates(coords) {
  if (!Array.isArray(coords) || !coords.length) return [];
  // Already a simple ring: [[lon,lat], ...] or [{lat,lng}, ...]
  const first = coords[0];
  if ((Array.isArray(first) && typeof first[0] === 'number' && typeof first[1] === 'number') || (first && typeof first === 'object' && !Array.isArray(first) && ('lat' in first || 'latitude' in first))) {
    return coords;
  }
  // GeoJSON Polygon: [outerRing, hole1, ...]. MultiPolygon: [[outerRing...], ...].
  const rings = [];
  const collect = (node, depth = 0) => {
    if (!Array.isArray(node) || depth > 4) return;
    const f = node[0];
    if ((Array.isArray(f) && typeof f[0] === 'number' && typeof f[1] === 'number') || (f && typeof f === 'object' && !Array.isArray(f) && ('lat' in f || 'latitude' in f))) {
      rings.push(node);
      return;
    }
    for (const child of node) collect(child, depth + 1);
  };
  collect(coords);
  if (!rings.length) return [];
  // Draw the largest exterior ring; holes are ignored because gmp-polygon-3d
  // does not consistently support inner rings in the beta element.
  return rings.sort((a, b) => ringAreaScore(b) - ringAreaScore(a))[0];
}

function polygonCoordinates(poly) {
  const raw = Array.isArray(poly?.geometry?.coordinates) ? poly.geometry.coordinates
    : Array.isArray(poly?.coordinates) ? poly.coordinates
    : Array.isArray(poly?.path) ? poly.path
    : Array.isArray(poly?.points) ? poly.points
    : Array.isArray(poly?.ring) ? poly.ring
    : Array.isArray(poly) ? poly
    : [];
  return flattenPolygonCoordinates(raw);
}

function makePolygonLayer(path, fillColor, fillOpacity, extrudedHeight, strokeOpacity = null) {
  return tagBaitLayer(createPolygon3D({
    path,
    altitude: 20,
    altitudeMode: 'relative',
    fillColor,
    fillOpacity,
    strokeColor: fillColor,
    strokeOpacity: strokeOpacity == null ? Math.min(fillOpacity + 0.08, 0.92) : strokeOpacity,
    strokeWidth: 0.8,
    extrudedHeight,
    preferAttributePath: true,
  }));
}


function tagBaitLayer(el) {
  try { el?.setAttribute?.('data-gfs-layer', 'bait'); } catch (_) {}
  return el;
}

function baitProbability(row) {
  return clampProbability(row?.probability ?? row?.baitScore ?? row?.bait_score ?? row?.score ?? row?.confidence ?? 0.0);
}

function isWaterRow(row) {
  if (!row) return false;
  if (row.valid === false || row.water === false) return false;
  const mask = String(row.mask || row.water_mask_source || '').toLowerCase();
  if (mask.includes('land') || mask.includes('invalid')) return false;
  return true;
}

function normalizedBaitRowsFromOceanPoints(points) {
  return (Array.isArray(points) ? points : [])
    .filter(isWaterRow)
    .map((row, index) => ({
      row: { ...row, source: row.source || 'hycom_ocean_points_bait_probability' },
      index,
      lat: Number(row?.lat),
      lon: Number(row?.lon),
      probability: baitProbability(row),
      cell: row?.cell || null,
      waterMask: row?.mask || 'hycom_valid_water',
    }))
    .filter((x) => Number.isFinite(x.lat) && Number.isFinite(x.lon) && x.probability >= 0.10)
    .sort((a, b) => b.probability - a.probability)
    .slice(0, 96);
}

function normalizedBaitRowsFromScores(scoreRows) {
  return (Array.isArray(scoreRows) ? scoreRows : [])
    .filter(isWaterRow)
    .map((row, index) => ({
      row,
      index,
      lat: Number(row?.lat),
      lon: Number(row?.lon),
      probability: baitProbability(row),
      cell: row?.cell || null,
      waterMask: row?.water_mask_source || row?.mask || 'marker_or_bait_score',
    }))
    .filter((x) => Number.isFinite(x.lat) && Number.isFinite(x.lon) && x.probability >= 0.08)
    .sort((a, b) => b.probability - a.probability)
    .slice(0, 42);
}

function bboxSpan(bbox) {
  let west; let south; let east; let north;
  if (Array.isArray(bbox) && bbox.length >= 4) {
    [west, south, east, north] = bbox.map(Number);
  } else if (bbox && typeof bbox === 'object') {
    west = Number(bbox.west); south = Number(bbox.south); east = Number(bbox.east); north = Number(bbox.north);
  } else {
    return 6;
  }
  if (![west, south, east, north].every(Number.isFinite)) return 6;
  return Math.max(Math.abs(east - west), Math.abs(north - south));
}

function polygonFieldFromBaitRows({ oceanPoints = [], scoreRows = [], bbox = null } = {}) {
  // Preferred solve: SST/current-backed water samples. Fallback solve: the same
  // bait_score rows that feed the glass intel pane. The fallback is still
  // contoured into schools; it is not rendered as random point blobs.
  let rows = normalizedBaitRowsFromOceanPoints(oceanPoints);
  let source = 'hycom_ocean_points_bait_probability';
  let thresholds = [
    { name: 'outer', value: 0.12 },
    { name: 'inner', value: 0.27 },
    { name: 'core', value: 0.49 },
  ];

  if (rows.length < 4) {
    rows = normalizedBaitRowsFromScores(scoreRows);
    source = 'server_hycom_bait_score_depth_field_contour_solve';
    thresholds = [
      // Lower thresholds make low-viewport / sparse-marker bait cells show up
      // as broad schools instead of only appearing as HUD intel rows.
      { name: 'outer', value: 0.07 },
      { name: 'inner', value: 0.16 },
      { name: 'core', value: 0.32 },
    ];
  }

  const points = rows.map((x) => ({
    ...x.row,
    lat: x.lat,
    lon: x.lon,
    probability: x.probability,
    baitScore: x.probability,
    cell: x.cell,
    source,
    water_mask_source: x.waterMask,
  }));

  if (points.length < 4) {
    return { outer: [], inner: [], core: [], rows, source: 'waiting_for_bait_field_solve', grid: null };
  }

  const span = bboxSpan(bbox);
  const maxGrid = span > 18 ? 110 : span > 8 ? 140 : span > 3 ? 180 : 220;
  const contour = contourPolygonsFromPoints({
    points,
    bbox,
    valueAccessor: (row) => row?.probability ?? row?.baitScore ?? row?.bait_score ?? row?.score,
    thresholds,
    maxGrid,
    capPerBand: span > 12 ? 180 : 260,
  });

  return {
    outer: contour.bands?.outer || [],
    inner: contour.bands?.inner || [],
    core: contour.bands?.core || [],
    rows,
    source: `${source}+${contour.source}`,
    grid: contour.grid,
  };
}

function makeLineOverlay(line) {
  const el = tagBaitLayer(document.createElement('gmp-polyline-3d'));
  const pts = Array.isArray(line?.coordinates) ? line.coordinates : [];
  const path = sanitizePath(pts
    .filter((p) => Array.isArray(p) && p.length >= 2)
    .map((p) => ({ lat: toNumber(p[1]), lng: toNumber(p[0]), altitude: 15 })));
  if (path.length < 2) return null;
  el.path = path;
  el.setAttribute('altitude-mode', 'relative-to-ground');
  el.setAttribute('stroke-color', '#ffe38d');
  el.setAttribute('stroke-width', '2');
  el.setAttribute('stroke-opacity', '0.85');
  return el;
}

function enqueuePolygonBand(queue, polygons, band, payloadBbox = null) {
  (Array.isArray(polygons) ? polygons : []).forEach((poly) => {
    const pathBboxHint = poly?.bbox || poly?.bbox_used || payloadBbox || window.__gfsLastBaitBbox || null;
    const depthAltitude = baitAltitudeFromDepth(poly, 20);
    const path = toPath(polygonCoordinates(poly), depthAltitude, pathBboxHint);
    if (path.length < 3) return;
    if (!pathLooksLikeViewportBait(path, pathBboxHint)) {
      try { console.info('[gfs bait] rejected detached chlorophyll/bait polygon', { band, source: poly?.source, pathPoints: path.length, bbox: pathBboxHint }); } catch (_) {}
      return;
    }
    const probability = poly?.probability ?? poly?.score ?? poly?.value ?? poly?.confidence ?? 0.35;
    queue.push({
      ...poly,
      path,
      probability,
      schoolScore: schoolScore({ ...poly, probability }),
      band: poly?.band || band,
    });
  });
}


const baitRenderState = window.__gfsBaitRenderState || {
  polygons: new Map(),
  lines: new Map(),
  pulseRaf: null,
  pulseDisposed: false,
  lastCounters: null,
};
try { window.__gfsBaitRenderState = baitRenderState; } catch (_) {}

function payloadSceneTier(payload) {
  return String(payload?.scene_tier || payload?.sceneTier || payload?.scene_plan?.tier || window.__gfsSceneTier || 'world').toLowerCase();
}

function baitBudgetForPayload(payload) {
  const tier = payloadSceneTier(payload);
  if (tier.includes('local') || tier.includes('harbor')) return MAX_BAIT_POLYGONS_LOCAL;
  if (tier.includes('regional')) return MAX_BAIT_POLYGONS_REGIONAL;
  return MAX_BAIT_POLYGONS_WORLD;
}

function baitPathKey(path) {
  const pts = Array.isArray(path) ? path : [];
  const c = centroidOfPath(pts);
  return `${c.lng.toFixed(3)}:${c.lat.toFixed(3)}:${pts.length}`;
}

function baitFeatureId(poly, fallbackIndex = 0) {
  const explicit = poly?.id || poly?.feature_id || poly?.zone_id || poly?.waterbody_id || poly?.waterbodyId || poly?.nhd_id || poly?.source_id;
  if (explicit) return `bait:${poly?.band || 'zone'}:${String(explicit)}`;
  const p = Math.round(clampProbability(poly?.schoolScore ?? poly?.probability) * 100);
  return `bait:${poly?.water_type || poly?.waterType || 'ocean'}:${poly?.band || 'zone'}:${baitPathKey(poly?.path)}:${p}:${fallbackIndex % 997}`;
}

function baitOpacityForState(obj, nowMs = performance.now()) {
  const p = clampProbability(obj?.probability ?? obj?.schoolScore ?? 0.35);
  const base = baitOpacity(p, obj?.band || 'inner');
  const fade = clampProbability(obj?.fade ?? 1);
  const phase = Number(obj?.phase || 0);
  const pulse = 0.86 + (0.14 * Math.sin((nowMs / 900) + phase));
  return clampProbability(base * fade * pulse);
}

function applyBaitVisual(obj, nowMs = performance.now()) {
  if (!obj?.el) return;
  const el = obj.el;
  const p = clampProbability(obj.probability ?? obj.schoolScore ?? 0.35);
  const fillOpacity = baitOpacityForState(obj, nowMs);
  const strokeOpacity = clampProbability(baitStrokeOpacity(p, obj.band) * clampProbability(obj.fade ?? 1));
  try { el.fillOpacity = fillOpacity; } catch (_) {}
  try { el.strokeOpacity = strokeOpacity; } catch (_) {}
  try { el.setAttribute?.('fill-opacity', fillOpacity.toFixed(3)); } catch (_) {}
  try { el.setAttribute?.('stroke-opacity', strokeOpacity.toFixed(3)); } catch (_) {}
  try { el.setAttribute?.('data-bait-state', obj.fadingOut ? 'fading_out' : 'active'); } catch (_) {}
}

function ensureBaitPulseLoop() {
  if (baitRenderState.pulseRaf) return;
  baitRenderState.pulseDisposed = false;
  let last = 0;
  const tick = (now) => {
    if (baitRenderState.pulseDisposed) {
      baitRenderState.pulseRaf = null;
      return;
    }
    if ((now - last) >= BAIT_PULSE_INTERVAL_MS) {
      for (const [id, obj] of baitRenderState.polygons.entries()) {
        if (obj.fadingOut) obj.fade = Math.max(0, toNumber(obj.fade, 1) - BAIT_FADE_STEP);
        else obj.fade = Math.min(1, toNumber(obj.fade, 1) + BAIT_FADE_STEP);
        applyBaitVisual(obj, now);
        if (obj.fadingOut && obj.fade <= 0.001) {
          try { obj.el?.remove?.(); } catch (_) {}
          baitRenderState.polygons.delete(id);
        }
      }
      last = now;
    }
    baitRenderState.pulseRaf = requestAnimationFrame(tick);
  };
  baitRenderState.pulseRaf = requestAnimationFrame(tick);
}

function buildBaitFeatureQueue({ outerPolygons, innerPolygons, corePolygons, bbox, payload }) {
  const queue = [];
  enqueuePolygonBand(queue, outerPolygons, 'outer', bbox);
  enqueuePolygonBand(queue, innerPolygons, 'inner', bbox);
  enqueuePolygonBand(queue, corePolygons, 'core', bbox);
  const budget = baitBudgetForPayload(payload);
  queue.sort((a, b) => clampProbability(b.schoolScore ?? b.probability) - clampProbability(a.schoolScore ?? a.probability));
  return { features: queue.slice(0, budget), rejectedByCap: Math.max(0, queue.length - budget), budget };
}

function reconcileBaitPolygons({ features, map3DElement, payloadVector }) {
  const incoming = new Set();
  const counters = { payload: features.length, updated: 0, created: 0, fading: 0, invalid: 0 };
  const frag = document.createDocumentFragment();
  features.forEach((poly, index) => {
    if (!poly?.path || poly.path.length < 3) { counters.invalid += 1; return; }
    const id = baitFeatureId(poly, index);
    incoming.add(id);
    const p = clampProbability(poly?.schoolScore ?? poly?.probability);
    const { coreColor, innerColor, outerColor } = probabilityColorRamp(p);
    const color = poly.band === 'outer' ? outerColor : poly.band === 'inner' ? innerColor : coreColor;
    let obj = baitRenderState.polygons.get(id);
    if (!obj) {
      const el = makePolygonLayer(poly.path, color, baitOpacity(p, poly.band), baitExtrudeHeightForPolygon(poly, p, poly.band), baitStrokeOpacity(p, poly.band));
      if (!el) { counters.invalid += 1; return; }
      try {
        el.setAttribute?.('data-gfs-layer', 'bait');
        el.setAttribute?.('data-bait-id', id);
        el.setAttribute?.('data-bait-band', poly.band || 'zone');
        el.setAttribute?.('data-bait-probability', String(Math.round(p * 100)));
        el.setAttribute?.('data-bait-renderer', 'advanced-bait-depth-contours');
        const depthFt = baitDepthFt(poly);
        if (Number.isFinite(depthFt)) el.setAttribute?.('data-bait-depth-ft', String(Math.round(depthFt)));
        el.removeAttribute?.('title');
      } catch (_) {}
      obj = {
        id,
        el,
        band: poly.band || 'inner',
        probability: p,
        schoolScore: p,
        vector: motionVectorForPolygon(poly, payloadVector),
        phase: ((poly.path?.[0]?.lat || 0) * 7.13) + ((poly.path?.[0]?.lng || 0) * 3.17),
        fade: 0.25,
        fadingOut: false,
      };
      baitRenderState.polygons.set(id, obj);
      frag.append(el);
      counters.created += 1;
    } else {
      obj.band = poly.band || obj.band || 'inner';
      obj.probability = p;
      obj.schoolScore = p;
      obj.vector = motionVectorForPolygon(poly, payloadVector);
      obj.fadingOut = false;
      try {
        obj.el.fillColor = color;
        obj.el.strokeColor = color;
        obj.el.extrudedHeight = baitExtrudeHeightForPolygon(poly, p, obj.band);
        obj.el.setAttribute?.('data-bait-probability', String(Math.round(p * 100)));
        obj.el.setAttribute?.('data-bait-renderer', 'advanced-bait-depth-contours');
        const depthFt = baitDepthFt(poly);
        if (Number.isFinite(depthFt)) obj.el.setAttribute?.('data-bait-depth-ft', String(Math.round(depthFt)));
        obj.el.removeAttribute?.('title');
      } catch (_) {}
      counters.updated += 1;
    }
    applyBaitVisual(obj);
  });
  if (frag.childNodes.length) map3DElement.append(frag);
  for (const [id, obj] of baitRenderState.polygons.entries()) {
    if (!incoming.has(id)) {
      obj.fadingOut = true;
      counters.fading += 1;
    }
  }
  ensureBaitPulseLoop();
  return counters;
}

function clearBaitLayer() {
  baitRenderState.pulseDisposed = true;
  if (baitRenderState.pulseRaf) {
    try { cancelAnimationFrame(baitRenderState.pulseRaf); } catch (_) {}
    baitRenderState.pulseRaf = null;
  }
  for (const obj of baitRenderState.polygons.values()) {
    try { obj.el?.remove?.(); } catch (_) {}
  }
  baitRenderState.polygons.clear();
  for (const obj of baitRenderState.lines.values()) {
    try { obj.el?.remove?.(); } catch (_) {}
  }
  baitRenderState.lines.clear();
}

function preserveBaitDisposer(reason = 'preserve_existing') {
  const disposer = () => clearBaitLayer();
  disposer.__gfsKeepExisting = true;
  disposer.__gfsDidRender = true;
  try { window.__gfsDebugEvent?.('bait/preserve', { reason, rendered: baitRenderState.polygons.size }); } catch (_) {}
  return disposer;
}



function isDrawableViewportReason(reason) {
  const r = String(reason || 'steady').toLowerCase();
  return r === 'boot' || r === 'steady' || r === 'settled' || r === 'manual' || r === 'refresh' || r.includes('steady') || r.includes('settled') || r.includes('update') || r.includes('deferred');
}

export function renderBaitZones({ payload, map3DElement, viewportReason = 'steady' }) {
  if (!map3DElement || !payload) return preserveBaitDisposer('missing_map_or_payload');
  console.info('[gfs bait] polygon api', { api: polygonApiPath() });
  if (!isDrawableViewportReason(viewportReason)) {
    console.info('[gfs bait] hold visible layer during non-draw reason', { reason: viewportReason });
    return preserveBaitDisposer('non_draw_viewport_reason');
  }

  const bait = payload?.bait || {};
  const rootBait = payload || {};
  const polygonFieldV1 = payload?.polygon_field_v1;
  if (polygonFieldV1) normalizePolygonFieldPayload(polygonFieldV1);

  const advancedRows = Array.isArray(payload?.advancedBaitRows) ? payload.advancedBaitRows
    : (Array.isArray(payload?.advanced_bait_rows) ? payload.advanced_bait_rows
      : (Array.isArray(payload?.dense_bait_field?.rows) ? payload.dense_bait_field.rows
        : (Array.isArray(payload?.bait?.advancedBaitRows) ? payload.bait.advancedBaitRows
          : (Array.isArray(payload?.bait?.advanced_bait_rows) ? payload.bait.advanced_bait_rows : []))));
  const scoreRows = advancedRows.length
    ? advancedRows
    : (Array.isArray(payload?.bait_score) ? payload.bait_score : (Array.isArray(payload?.bait?.bait_score) ? payload.bait.bait_score : []));
  const oceanPoints = Array.isArray(payload?.oceanPoints?.points) ? payload.oceanPoints.points
    : (Array.isArray(payload?.bait?.oceanPoints?.points) ? payload.bait.oceanPoints.points
      : (Array.isArray(payload?.ocean_points) ? payload.ocean_points
        : (Array.isArray(payload?.bait?.ocean_points) ? payload.bait.ocean_points
          : (Array.isArray(payload?.points) ? payload.points : []))));
  const bbox = payload?.bbox || payload?.bbox_used || payload?.oceanPoints?.bbox || payload?.oceanPoints?.bbox_used || null;
  try { window.__gfsLastBaitBbox = bbox; } catch (_) {}

  const allowClientBaitFallback = window.__GFS_DEBUG_CLIENT_BAIT_FALLBACK === true;
  const solvePolygons = allowClientBaitFallback ? polygonFieldFromBaitRows({ oceanPoints: [], scoreRows, bbox }) : { outer: [], inner: [], core: [], rows: [], source: 'disabled_strict_payload_only', grid: null };
  const hasServerPolygons = Boolean(
    (Array.isArray(bait.outer_polygons) && bait.outer_polygons.length)
    || (Array.isArray(bait.inner_polygons) && bait.inner_polygons.length)
    || (Array.isArray(bait.core_polygons) && bait.core_polygons.length)
    || (Array.isArray(bait.polygons) && bait.polygons.length)
    || (Array.isArray(rootBait.outer_polygons) && rootBait.outer_polygons.length)
    || (Array.isArray(rootBait.inner_polygons) && rootBait.inner_polygons.length)
    || (Array.isArray(rootBait.core_polygons) && rootBait.core_polygons.length)
    || (Array.isArray(rootBait.polygons) && rootBait.polygons.length)
  );
  const baitSource = String(bait?.source || payload?.source || '').toLowerCase();
  const baitMode = String(payload?.mode || '').toLowerCase();
  const hasFullStack = bait.status === 'ready' && hasServerPolygons && (baitSource === 'full_stack' || baitSource.includes('live_hycom') || baitSource.includes('coastwatch') || baitMode.includes('marching_squares'));

  const outerPolygons = Array.isArray(bait.outer_polygons) && bait.outer_polygons.length ? bait.outer_polygons : (Array.isArray(rootBait.outer_polygons) && rootBait.outer_polygons.length ? rootBait.outer_polygons : solvePolygons.outer);
  const innerPolygons = Array.isArray(bait.inner_polygons) && bait.inner_polygons.length ? bait.inner_polygons : (Array.isArray(rootBait.inner_polygons) && rootBait.inner_polygons.length ? rootBait.inner_polygons : (Array.isArray(bait.polygons) && bait.polygons.length ? bait.polygons : (Array.isArray(rootBait.polygons) && rootBait.polygons.length ? rootBait.polygons : solvePolygons.inner)));
  const corePolygons = Array.isArray(bait.core_polygons) && bait.core_polygons.length ? bait.core_polygons : (Array.isArray(rootBait.core_polygons) && rootBait.core_polygons.length ? rootBait.core_polygons : solvePolygons.core);

  if (!outerPolygons.length && !innerPolygons.length && !corePolygons.length) {
    const reason = payload?.inland_waiting_for_vertices || bait?.waiting_for_inland_water_vertices
      ? 'waiting_for_inland_water_vertices'
      : (allowClientBaitFallback ? 'debug_client_bait_fallback_no_visible_school_solve_yet' : 'strict_payload_has_no_server_polygons_yet');
    console.info('[gfs bait] waiting for field solve; preserving existing bait if any', {
      status: bait.status,
      source: bait.source || payload?.source,
      baitScoreRows: scoreRows.length,
      advancedBaitRows: advancedRows.length,
      oceanPointRows: oceanPoints.length,
      payloadKeys: Object.keys(payload || {}),
      baitKeys: Object.keys(bait || {}),
      reason,
      renderedExisting: baitRenderState.polygons.size,
    });
    return preserveBaitDisposer(reason);
  }

  const payloadVector = averageCurrentVector([...(oceanPoints || []), ...(scoreRows || [])]);
  const { features, rejectedByCap, budget } = buildBaitFeatureQueue({ outerPolygons, innerPolygons, corePolygons, bbox, payload });
  const counters = reconcileBaitPolygons({ features, map3DElement, payloadVector });

  // Front lines are metadata accents. Keep them lightweight and do not attach hover payloads.
  const lines = Array.isArray(payload?.front_lines) ? payload.front_lines.slice(0, 12) : [];
  for (const obj of baitRenderState.lines.values()) { try { obj.el?.remove?.(); } catch (_) {} }
  baitRenderState.lines.clear();
  if (lines.length) {
    const frag = document.createDocumentFragment();
    lines.forEach((line, index) => {
      const el = makeLineOverlay(line);
      if (!el) return;
      const id = `bait-line:${index}`;
      try { el.removeAttribute?.('title'); el.setAttribute?.('data-bait-line-id', id); } catch (_) {}
      baitRenderState.lines.set(id, { id, el });
      frag.append(el);
    });
    if (frag.childNodes.length) map3DElement.append(frag);
  }

  const summary = {
    payloadPolygons: outerPolygons.length + innerPolygons.length + corePolygons.length,
    considered: counters.payload,
    rendered: baitRenderState.polygons.size,
    updated: counters.updated,
    created: counters.created,
    fading: counters.fading,
    invalid: counters.invalid,
    cap: budget,
    rejectedByCap,
    lines: baitRenderState.lines.size,
    source: hasFullStack ? 'full_stack' : solvePolygons.source,
    baitScoreRows: scoreRows.length,
    advancedBaitRows: advancedRows.length,
    oceanPointRows: oceanPoints.length,
    animation: 'opacity_pulse_only_no_path_mutation',
    inlandPolicy: 'if_inland_vertices_missing_preserve_and_wait_no_free_float_bait',
    solveGrid: solvePolygons.grid || null,
    solveMethod: allowClientBaitFallback ? 'debug client contour solve from HYCOM ocean/bait rows' : 'strict server polygons only: sea contours plus advanced bait marching-squares',
    renderer: 'advanced-bait-depth-contours',
    depthPolicy: 'bait depth encoded as above-water extrusion height',
    seaResolution: payload?.sea_resolution || null,
  };
  baitRenderState.lastCounters = summary;
  console.info('[gfs bait] reconciled polygons', summary);
  try { window.__gfsDebugEvent?.('bait/reconcile', summary); } catch (_) {}

  const disposer = () => clearBaitLayer();
  disposer.__gfsKeepExisting = true;
  disposer.__gfsDidRender = true;
  return disposer;
}
