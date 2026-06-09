import { createPolygon3D } from './polygon3d.js';
import { attachPolygonHover } from './hover_tip.js';
import { contourPolygonsFromPoints } from './marching_squares.js';

const VIRIDIAN = '#24ffe6';
const VIRIDIAN_FILL = '#37d9bd';
const VIRIDIAN_STROKE = '#24ffe6';
const INLAND_SHORELINE_HALO = '#00d8ff';
const INLAND_SHORELINE_CORE = '#7dfff2';
const BAIT_FILL = '#39ff5a';
const BAIT_STROKE = '#ff9b21';
const INLAND_GLOBAL_TEMP_LABEL_CAP = 18;
const INLAND_MIN_TEMP_LABEL_AREA_KM2 = 0.6475;



function tempLabelBudget(range) {
  const r = Number(range || 0);
  if (r >= 1500000) return 6;
  if (r >= 900000) return 8;
  if (r >= 350000) return 12;
  if (r >= 120000) return 18;
  return INLAND_GLOBAL_TEMP_LABEL_CAP;
}

function tempLabelStride(range) {
  const r = Number(range || 0);
  if (r >= 1500000) return 5;
  if (r >= 900000) return 4;
  if (r >= 350000) return 2;
  return 1;
}

function isRealTemperaturePoint(pt) {
  if (!pt || typeof pt !== 'object') return false;
  const lat = n(pt?.lat, NaN);
  const lng = n(pt?.lng ?? pt?.lon, NaN);
  const temp = n(pt?.water_temp_f ?? pt?.surface_temp_f ?? pt?.temp_f ?? pt?.temperature_f, NaN);
  if (!Number.isFinite(lat) || !Number.isFinite(lng) || !Number.isFinite(temp)) return false;
  if (lat < -90 || lat > 90 || lng < -180 || lng > 180) return false;
  if (temp < -40 || temp > 125) return false;
  const source = String(pt?.source || pt?.temperature_source || '').toLowerCase();
  if (pt?.fake === true || pt?.synthetic === true || source.includes('synthetic_random')) return false;
  return true;
}

function minTempLabelAreaKm2(range) {
  if (range < 350000) return 0.0;
  return INLAND_MIN_TEMP_LABEL_AREA_KM2;
}

function n(v, fallback = 0) {
  const out = Number(v);
  return Number.isFinite(out) ? out : fallback;
}

function cameraRangeMeters(map3DElement) {
  const raw = Number(map3DElement?.range ?? map3DElement?.getAttribute?.('range') ?? document.getElementById('globe')?.range ?? document.getElementById('globe')?.getAttribute?.('range'));
  return Number.isFinite(raw) && raw > 0 ? raw : 1800000;
}

function fmtMph(v) { const x = n(v, NaN); return Number.isFinite(x) ? `${x.toFixed(1)} mph` : 'n/a'; }
function fmtDeg(v) { const x = n(v, NaN); return Number.isFinite(x) ? `${Math.round(x)}°` : 'n/a'; }
function fmtFt(v) { const x = n(v, NaN); return Number.isFinite(x) ? `${x.toFixed(1)} ft` : 'n/a'; }

function normalizePath(path, altitude = 10) {
  const out = [];
  for (const p of Array.isArray(path) ? path : []) {
    const lat = n(p?.lat, NaN);
    const lng = n(p?.lng ?? p?.lon, NaN);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) continue;
    if (lat < -90 || lat > 90 || lng < -180 || lng > 180) continue;
    const prev = out[out.length - 1];
    if (prev && Math.abs(prev.lat - lat) < 1e-8 && Math.abs(prev.lng - lng) < 1e-8) continue;
    out.push({ lat, lng, altitude });
  }
  if (out.length >= 2) {
    const a = out[0];
    const b = out[out.length - 1];
    if (Math.abs(a.lat - b.lat) < 1e-8 && Math.abs(a.lng - b.lng) < 1e-8) out.pop();
  }
  return out;
}

function renderPathForFeature(item, altitude = 18) {
  // Raw item.path remains the mask/bounds truth.  draw_path/render_path is the
  // intentionally simplified, low-cost shoreline sent by the server.
  return normalizePath(item?.render_path || item?.draw_path || item?.path || [], altitude);
}

function linePathAttr(path) {
  return path.map((p) => `${p.lat.toFixed(8)},${p.lng.toFixed(8)},${n(p.altitude, 12).toFixed(2)}`).join(' ');
}

function pathHash(path) {
  return normalizePath(path, 0).map((p) => `${p.lat.toFixed(5)},${p.lng.toFixed(5)}`).join(';');
}

function stableInlandFeatureId(item, kind = 'waterbody', index = 0) {
  const explicit = item?.id || item?.permanent_identifier || item?.Permanent_Identifier || item?.gnis_id || item?.nhd_id || item?.waterbody_id || item?.reachcode || item?.source_id;
  if (explicit) return `inland:${kind}:${String(explicit)}`;
  const name = String(item?.name || item?.GNIS_Name || item?.fcode || kind || 'feature').toLowerCase().replace(/[^a-z0-9]+/g, '-').slice(0, 64);
  return `inland:${kind}:${name}:${pathHash(item?.path || []).slice(0, 120)}:${index % 997}`;
}

function stableInlandFeatureHash(item, path) {
  return [item?.kind || item?.fcode || '', item?.lod_tier || '', item?.source_path_points || '', pathHash(path || item?.path || [])].join('|');
}

function centroidFromRing(path) {
  const pts = normalizePath(path, 0);
  if (!pts.length) return null;
  let sumLat = 0;
  let sumLng = 0;
  for (const p of pts) { sumLat += p.lat; sumLng += p.lng; }
  return { lat: sumLat / pts.length, lng: sumLng / pts.length };
}

function boundsFromPath(path) {
  const pts = normalizePath(path, 0);
  if (!pts.length) return null;
  let west = Infinity, south = Infinity, east = -Infinity, north = -Infinity;
  for (const p of pts) {
    if (p.lng < west) west = p.lng;
    if (p.lng > east) east = p.lng;
    if (p.lat < south) south = p.lat;
    if (p.lat > north) north = p.lat;
  }
  return Number.isFinite(west) ? { west, south, east, north } : null;
}

function pointInPolygon(lat, lng, path) {
  const pts = normalizePath(path, 0);
  if (pts.length < 3) return false;
  let inside = false;
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
    const xi = pts[i].lng; const yi = pts[i].lat;
    const xj = pts[j].lng; const yj = pts[j].lat;
    const crosses = ((yi > lat) !== (yj > lat)) && (lng < ((xj - xi) * (lat - yi)) / (((yj - yi) || 1e-12)) + xi);
    if (crosses) inside = !inside;
  }
  return inside;
}

function inwardProjectToPolygon(lat, lng, shorelinePath, centroid) {
  if (pointInPolygon(lat, lng, shorelinePath)) return { lat, lng };
  const center = centroid || centroidFromRing(shorelinePath);
  if (!center) return null;
  let curLat = lat;
  let curLng = lng;
  for (let i = 0; i < 12; i += 1) {
    curLat = center.lat + (curLat - center.lat) * 0.72;
    curLng = center.lng + (curLng - center.lng) * 0.72;
    if (pointInPolygon(curLat, curLng, shorelinePath)) return { lat: curLat, lng: curLng };
  }
  return null;
}


function makePolyline3D({ path, strokeColor = VIRIDIAN_STROKE, strokeWidth = 5, altitude = 18, hover = null, strokeOpacity = 1 }) {
  const coords = normalizePath(path, altitude);
  if (coords.length < 2) return null;
  const el = document.createElement('gmp-polyline-3d');
  try { el.setAttribute('path', linePathAttr(coords)); } catch (_) {}
  try { el.path = coords; } catch (_) {}
  try { el.setAttribute('altitude-mode', 'relative-to-ground'); } catch (_) {}
  try { el.strokeColor = strokeColor; } catch (_) {}
  try { el.strokeWidth = strokeWidth; } catch (_) {}
  try { el.strokeOpacity = strokeOpacity; } catch (_) {}
  try { el.setAttribute('stroke-color', strokeColor); } catch (_) {}
  try { el.setAttribute('stroke-width', String(strokeWidth)); } catch (_) {}
  try { el.setAttribute('stroke-opacity', String(strokeOpacity)); } catch (_) {}
  try { el.setAttribute('draws-when-occluded', 'true'); } catch (_) {}
  try { el.setAttribute('data-gfs-layer', 'inland-water'); } catch (_) {}
  try { el.setAttribute('data-inland-water-kind', 'flowline'); } catch (_) {}
  if (hover) {
    try { attachPolygonHover(el, hover); } catch (_) {}
  }
  return el;
}

function makeGlowingInlandLine({ path, altitude = 18, coreWidth = 4.8, hover = null, kind = 'waterbody-shoreline' } = {}) {
  // Lowest draw-cost shoreline: one wide teal 3D polyline per lake.
  // Raw source vertices are still retained server-side/client-side for lake
  // bounds, point-in-polygon masks, temp labels, and bait contours.
  const width = Math.max(3.8, Math.min(7.2, Number(coreWidth || 4.8)));
  const el = makePolyline3D({
    path,
    altitude,
    strokeColor: VIRIDIAN_STROKE,
    strokeWidth: width,
    strokeOpacity: 0.94,
    hover,
  });
  if (!el) return [];
  try { el.setAttribute('data-inland-water-kind', kind); } catch (_) {}
  try { el.setAttribute('data-inland-teal-shoreline', 'single-low-cost-line'); } catch (_) {}
  return [el];
}


function waterHover(item, path, kind = 'water') {
  const temp = item.water_temp_f ?? item.surface_temp_f ?? null;
  const bait = item.bait_score ?? null;
  const current = item.current_speed_mph ?? null;
  const wind = item.speed_mph ?? null;
  const depth = item.bait_depth_ft ?? item.estimated_mean_depth_ft ?? null;
  return {
    title: `${item.name || 'Inland water'} (${item.kind || kind})`,
    lines: [
      `Source: ${item.source_class || 'NHD/NHDPlus HR'}`,
      `FCode: ${item.fcode ?? 'n/a'}`,
      temp != null ? `Surface temp: ${Number(temp).toFixed(1)} °F` : 'Surface temp: unavailable until real NCSS surface/t0m sample',
      bait != null ? `Inland bait score: ${Number(bait).toFixed(1)} / 5` : 'Inland bait: pending',
      wind != null ? `Surface wind: ${fmtMph(wind)} @ ${fmtDeg(item.heading_deg)}` : 'Surface wind: unavailable',
      current != null ? `Inland current: ${fmtMph(current)} @ ${fmtDeg(item.current_heading_deg)}` : 'Inland current: heuristic pending',
      depth != null ? `Bait depth: ${fmtFt(item.bait_depth_ft)} (mean depth ${fmtFt(item.estimated_mean_depth_ft)})` : 'Bait depth: pending',
      item.shoreline_truth ? `Shoreline: ${item.shoreline_truth}` : `Path points: ${path.length}`,
      `Source points: ${item.source_path_points ?? path.length}`,
      `Rendered points: ${item.render_path_points ?? path.length}`,
      item.lod_tile_deg ? `Tile detail: ${item.lod_tier || 'auto'} ${item.lod_tile_deg}°` : `LOD: ${item.lod_tier || 'auto'}`,
    ],
    metrics: { layer: 'inland-water', kind: item.kind || kind, path_points: path.length, source_points: item.source_path_points ?? path.length, render_points: item.render_path_points ?? path.length, source: item.source_class || 'NHDPlus HR' },
    payload: item,
  };
}

function makeWaterPolygon(item, index = 0) {
  const rawPath = normalizePath(item.path, 18);
  const path = renderPathForFeature(item, 18);
  if (rawPath.length < 3 || path.length < 3) return null;
  // Inland waterbodies render as a single shoreline-only teal line.  No lake top
  // fill polygon and no multi-pass halo: lowest draw cost wins here.
  const closedPath = [...path];
  const first = closedPath[0];
  const last = closedPath[closedPath.length - 1];
  if (first && last && (Math.abs(first.lat - last.lat) > 1e-8 || Math.abs(first.lng - last.lng) > 1e-8)) {
    closedPath.push({ ...first });
  }
  const elements = makeGlowingInlandLine({
    path: closedPath,
    altitude: 18,
    coreWidth: Math.min(7.2, Math.max(4.2, Number(item?.style?.strokeWidth ?? 4.8))),
    hover: waterHover(item, rawPath, 'waterbody'),
    kind: 'waterbody-shoreline',
  });
  for (const el of elements) {
    try { el?.setAttribute?.('data-gfs-layer', 'inland-water'); } catch (_) {}
    try { el?.setAttribute?.('data-inland-water-kind', 'waterbody-shoreline'); } catch (_) {}
    try { el?.setAttribute?.('data-inland-shoreline-only', 'true'); } catch (_) {}
    try { el?.setAttribute?.('data-inland-id', stableInlandFeatureId(item, 'waterbody', index)); } catch (_) {}
    try { el?.setAttribute?.('data-inland-geometry-hash', stableInlandFeatureHash(item, rawPath)); } catch (_) {}
  }
  return elements;
}


function baitZoneHover(zone = {}, path = []) {
  const safeZone = zone && typeof zone === 'object' ? zone : {};
  const safePath = Array.isArray(path) ? path : [];
  const temp = n(safeZone.water_temp_f ?? safeZone.surface_temp_f, NaN);
  const bait = n(safeZone.bait_score ?? safeZone.score, NaN);
  const source = String(safeZone.source || safeZone.temperature_source || 'freshwater thermal bait heuristic');
  const reason = String(safeZone.reason || safeZone.reasons?.[0] || 'freshwater bait zone');
  return {
    title: `${safeZone.name || 'Inland bait'} (${safeZone.kind || 'bait-zone'})`,
    lines: [
      Number.isFinite(bait) ? `Inland bait score: ${bait.toFixed(1)} / 5` : 'Inland bait score: pending',
      Number.isFinite(temp) ? `Water temp: ${temp.toFixed(1)} °F` : 'Water temp: unavailable',
      `Source: ${source}`,
      `Reason: ${reason}`,
      safeZone.shoreline_truth ? `Shoreline: ${safeZone.shoreline_truth}` : `Path points: ${safePath.length}`,
    ],
    metrics: { layer: 'inland-water', kind: 'bait-zone', path_points: safePath.length, source },
    payload: safeZone,
  };
}

function contourPath(poly, altitude = 15) {
  const coords = Array.isArray(poly?.coordinates) ? poly.coordinates : [];
  const out = [];
  for (const c of coords) {
    if (!Array.isArray(c) || c.length < 2) continue;
    const lng = n(c[0], NaN);
    const lat = n(c[1], NaN);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) continue;
    out.push({ lat, lng, altitude });
  }
  if (out.length >= 2) {
    const a = out[0];
    const b = out[out.length - 1];
    if (Math.abs(a.lat - b.lat) < 1e-7 && Math.abs(a.lng - b.lng) < 1e-7) out.pop();
  }
  return out;
}

function baitScoreFromTemp(tempF) {
  const t = Number(tempF);
  if (!Number.isFinite(t)) return NaN;
  // Freshwater first pass: best bait activity in a mild warm band. This is a
  // visualization score, not a fish guarantee; it lets NCSS surface temperature
  // drive the marching-square bait drawing instead of round synthetic boils.
  const distance = Math.abs(t - 68);
  return Math.max(0, Math.min(5, 5 - distance * 0.22));
}

function makeThermalBaitContourPolygon(poly) {
  const path = contourPath(poly, 15);
  if (path.length < 3) return null;
  const band = poly.band || 'thermal';
  const score = n(poly.value ?? poly.probability, 2.5);
  const depthFt = n(poly?.bait_depth_ft ?? poly?.preferred_bait_depth_ft, NaN);
  const bandFill = band === 'hot' ? '#63ff7f' : (band === 'good' ? '#3cff62' : (band === 'fair' ? '#76ff4b' : '#88ff6a'));
  const opacity = band === 'hot' ? 0.30 : (band === 'good' ? 0.22 : 0.16);
  const strokeWidth = band === 'hot' ? 2.4 : (band === 'good' ? 1.85 : 1.45);
  const altitude = band === 'hot' ? 18 : (band === 'good' ? 16 : 14);
  const extrudedHeight = Number.isFinite(depthFt)
    ? Math.max(9, Math.min(42, depthFt * 0.55))
    : (band === 'hot' ? 22 : (band === 'good' ? 18 : 12));
  const hover = zoneHoverPayload(poly, {
    band,
    value: score,
    source: poly?.source || 'inland_lake_advanced_bait_grid_squares',
    extraLines: [
      'Style: bright green bait squares with orange glow outline',
      Number.isFinite(depthFt) ? `Positive extrusion: ${extrudedHeight.toFixed(1)} m above water from bait depth` : 'Positive extrusion: heuristic above-water bait depth',
    ],
  });
  const el = createPolygon3D({
    path,
    altitude,
    altitudeMode: 'relative',
    extrudedHeight,
    extrudeToGround: false,
    fillColor: bandFill,
    fillOpacity: opacity,
    strokeColor: BAIT_STROKE,
    strokeOpacity: 0.98,
    strokeWidth,
    neonGlow: true,
    hover,
  });
  if (!el) return null;
  try { el.setAttribute('data-gfs-layer', 'inland-water'); } catch (_) {}
  try { el.setAttribute('data-inland-water-kind', 'bait-thermal-marching-squares'); } catch (_) {}
  try { el.setAttribute('data-inland-bait-style', 'bright-green-orange-glow-squares'); } catch (_) {}
  try { el.setAttribute('data-bait-band', String(band)); } catch (_) {}
  return el;
}

function dedupeTemperaturePoints(points) {
  const seen = new Set();
  const out = [];
  for (const pt of Array.isArray(points) ? points : []) {
    if (!isRealTemperaturePoint(pt)) continue;
    const lat = n(pt?.lat, NaN);
    const lng = n(pt?.lng ?? pt?.lon, NaN);
    const temp = n(pt?.water_temp_f ?? pt?.surface_temp_f, NaN);
    if (!Number.isFinite(lat) || !Number.isFinite(lng) || !Number.isFinite(temp)) continue;
    if (lat < -90 || lat > 90 || lng < -180 || lng > 180) continue;
    const key = `${lat.toFixed(4)}:${lng.toFixed(4)}:${Math.round(temp)}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ ...pt, lat, lng, water_temp_f: temp, real: true });
  }
  return out;
}

function centroidFromPath(path) {
  const pts = normalizePath(path, 14);
  if (!pts.length) return null;
  const lat = pts.reduce((a, p) => a + p.lat, 0) / pts.length;
  const lng = pts.reduce((a, p) => a + p.lng, 0) / pts.length;
  return { lat, lng };
}


function synthesizeTemperaturePointsFromGeometry({ polygons, lines, bait, conditions }) {
  const out = [];
  const candidates = [
    ...(Array.isArray(conditions?.temperature_points) ? conditions.temperature_points : []),
    ...(Array.isArray(bait?.temperature_points) ? bait.temperature_points : []),
  ];
  for (const pt of candidates) {
    if (isRealTemperaturePoint(pt)) out.push(pt);
  }
  return out;
}

function featureTempPoint(item, idx = 0) {
  const c = centroidFromPath(item?.path || []);
  if (!c) return null;
  const direct = n(item?.water_temp_f ?? item?.surface_temp_f, NaN);
  if (!Number.isFinite(direct)) return null;
  const source = item?.temperature_source || item?.source || 'gfs_ncss_surface_t0m_candidate';
  return {
    name: item?.name || 'Lake',
    lat: c.lat,
    lng: c.lng,
    lon: c.lng,
    water_temp_f: direct,
    surface_temp_f: direct,
    bait_score: baitScoreFromTemp(direct),
    source,
    temperature_source: source,
    confidence: 'medium',
    estimated: false,
    real: true,
    area_km2: n(item?.area_km2 ?? item?.AREASQKM, NaN),
    shape_area: n(item?.shape_area ?? item?.Shape_Area, NaN),
    permanent_identifier: item?.permanent_identifier || item?.Permanent_Identifier || null,
    current_speed_mph: n(item?.current_speed_mph, NaN),
    current_speed_m_s: n(item?.current_speed_m_s, NaN),
    current_heading_deg: n(item?.current_heading_deg, NaN),
    speed_mph: n(item?.speed_mph, NaN),
    speed_m_s: n(item?.speed_m_s, NaN),
    heading_deg: n(item?.heading_deg, NaN),
    bait_depth_ft: n(item?.bait_depth_ft, NaN),
    estimated_mean_depth_ft: n(item?.estimated_mean_depth_ft, NaN),
    bait_depth_band_ft: item?.bait_depth_band_ft || null,
    colorado_corridor: item?.colorado_corridor === true,
  };
}

function stableLabelHash(value) {
  const s = String(value || '');
  let h = 0;
  for (let i = 0; i < s.length; i += 1) h = ((h * 31) + s.charCodeAt(i)) >>> 0;
  return h >>> 0;
}

function selectTemperaturePoints(points, range) {
  const budget = Math.min(INLAND_GLOBAL_TEMP_LABEL_CAP, tempLabelBudget(range));
  const stride = tempLabelStride(range);
  const normalized = (Array.isArray(points) ? points : []).filter(Boolean).map((pt) => ({
    ...pt,
    area_km2: n(pt?.area_km2 ?? pt?.AREASQKM, NaN),
    shape_area: n(pt?.shape_area ?? pt?.Shape_Area, NaN),
  }));
  let eligible = normalized;
  if (range >= 350000) {
    eligible = eligible.filter((pt) => {
      const area = Number(pt?.area_km2);
      return Number.isFinite(area) ? area >= minTempLabelAreaKm2(range) : true;
    });
    eligible.sort((a, b) => {
      const aa = Number.isFinite(Number(a?.area_km2)) ? Number(a.area_km2) : -1;
      const bb = Number.isFinite(Number(b?.area_km2)) ? Number(b.area_km2) : -1;
      return bb - aa;
    });
    eligible = eligible.filter((pt, idx) => {
      const key = pt?.permanent_identifier || pt?.name || `${pt?.lat},${pt?.lng}`;
      return idx < 6 || (stableLabelHash(key) % 2 === 0);
    });
  }
  const stepped = eligible.filter((_, idx) => idx % Math.max(1, stride) === 0);
  return stepped.slice(0, Math.max(1, budget));
}

function temperaturePointsFromPayload({ polygons, bait, conditions, payload }) {
  const raw = [
    ...(Array.isArray(payload?.temperature_points) ? payload.temperature_points : []),
    ...(Array.isArray(payload?.tempLabels) ? payload.tempLabels : []),
    ...(Array.isArray(conditions?.temperature_points) ? conditions.temperature_points : []),
    ...(Array.isArray(conditions?.tempLabels) ? conditions.tempLabels : []),
    ...(Array.isArray(bait?.temperature_points) ? bait.temperature_points : []),
  ];
  const scoreSeeds = [
    ...(Array.isArray(payload?.bait_score) ? payload.bait_score : []),
    ...(Array.isArray(payload?.bait?.bait_score) ? payload.bait.bait_score : []),
    ...(Array.isArray(payload?.inland_bait?.bait_score) ? payload.inland_bait.bait_score : []),
    ...(Array.isArray(bait?.bait_score) ? bait.bait_score : []),
    ...(Array.isArray(bait?.targets) ? bait.targets.map((t) => ({
      ...t,
      lat: t?.lat ?? t?.centroid?.lat,
      lng: t?.lng ?? t?.lon ?? t?.centroid?.lng,
      lon: t?.lon ?? t?.lng ?? t?.centroid?.lng,
    })) : []),
  ];
  const withScores = [];
  for (const pt of raw) {
    const lat = n(pt?.lat, NaN);
    const lng = n(pt?.lng ?? pt?.lon, NaN);
    const temp = n(pt?.water_temp_f ?? pt?.surface_temp_f, NaN);
    const score = Number.isFinite(Number(pt?.bait_score)) ? Number(pt.bait_score) : baitScoreFromTemp(temp);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) continue;
    if (!Number.isFinite(temp) && !Number.isFinite(score)) continue;
    withScores.push({
      ...pt,
      lat,
      lng,
      lon: lng,
      water_temp_f: Number.isFinite(temp) ? temp : null,
      surface_temp_f: Number.isFinite(temp) ? temp : null,
      bait_score: Number.isFinite(score) ? score : baitScoreFromTemp(temp),
      bait_seed: !Number.isFinite(temp) && Number.isFinite(score),
      real: Number.isFinite(temp) && (pt.real === true || pt.estimated !== true),
      confidence: pt.confidence || (Number.isFinite(temp) ? (pt.estimated ? 'estimated' : 'medium') : 'bait-seed'),
      source: pt.source || (Number.isFinite(temp) ? 'gfs_ncss_surface_candidate' : 'inland_geometry_bait_score_seed'),
      current_speed_mph: n(pt?.current_speed_mph, NaN),
      current_speed_m_s: n(pt?.current_speed_m_s, NaN),
      current_heading_deg: n(pt?.current_heading_deg, NaN),
      speed_mph: n(pt?.speed_mph, NaN),
      speed_m_s: n(pt?.speed_m_s, NaN),
      heading_deg: n(pt?.heading_deg, NaN),
      bait_depth_ft: n(pt?.bait_depth_ft, NaN),
      estimated_mean_depth_ft: n(pt?.estimated_mean_depth_ft, NaN),
      bait_depth_band_ft: pt?.bait_depth_band_ft || null,
      colorado_corridor: pt?.colorado_corridor === true,
    });
  }
  for (const pt of scoreSeeds) {
    const lat = n(pt?.lat, NaN);
    const lng = n(pt?.lng ?? pt?.lon, NaN);
    const score = Number(pt?.bait_score ?? pt?.score_5 ?? pt?.probability);
    if (!Number.isFinite(lat) || !Number.isFinite(lng) || !Number.isFinite(score)) continue;
    withScores.push({
      ...pt,
      lat,
      lng,
      lon: lng,
      water_temp_f: Number.isFinite(Number(pt?.water_temp_f ?? pt?.surface_temp_f)) ? Number(pt?.water_temp_f ?? pt?.surface_temp_f) : null,
      surface_temp_f: Number.isFinite(Number(pt?.surface_temp_f ?? pt?.water_temp_f)) ? Number(pt?.surface_temp_f ?? pt?.water_temp_f) : null,
      bait_score: Math.max(0, Math.min(5, score)),
      bait_seed: true,
      real: false,
      confidence: pt?.confidence || 'bait-seed',
      source: pt?.source || 'inland_geometry_bait_score_seed',
      current_speed_mph: n(pt?.current_speed_mph, NaN),
      current_speed_m_s: n(pt?.current_speed_m_s, NaN),
      current_heading_deg: n(pt?.current_heading_deg, NaN),
      speed_mph: n(pt?.speed_mph, NaN),
      speed_m_s: n(pt?.speed_m_s, NaN),
      heading_deg: n(pt?.heading_deg, NaN),
      bait_depth_ft: n(pt?.bait_depth_ft, NaN),
      estimated_mean_depth_ft: n(pt?.estimated_mean_depth_ft, NaN),
      bait_depth_band_ft: pt?.bait_depth_band_ft || null,
      colorado_corridor: pt?.colorado_corridor === true,
    });
  }
  const seen = new Set();
  const out = [];
  for (const pt of withScores) {
    const lat = n(pt?.lat, NaN);
    const lng = n(pt?.lng ?? pt?.lon, NaN);
    const score = n(pt?.bait_score, NaN);
    const temp = n(pt?.water_temp_f ?? pt?.surface_temp_f, NaN);
    if (!Number.isFinite(lat) || !Number.isFinite(lng) || !Number.isFinite(score)) continue;
    if (lat < -90 || lat > 90 || lng < -180 || lng > 180) continue;
    const key = `${lat.toFixed(4)}:${lng.toFixed(4)}:${Math.round(score * 10)}:${Number.isFinite(temp) ? Math.round(temp) : 'seed'}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ ...pt, lat, lng, lon: lng, bait_score: score });
  }
  return out;
}



function nearestTemperaturePointsForLake(points, lake, range) {
  const center = centroidFromRing(lake?.path || []);
  const bounds = boundsFromPath(lake?.path || []);
  if (!center || !bounds) return [];
  const span = Math.max(0.02, Math.hypot(bounds.east - bounds.west, bounds.north - bounds.south));
  const maxDeg = Math.max(0.10, Math.min(0.85, span * (range < 350000 ? 1.75 : 2.65)));
  const ranked = [];
  for (const pt of Array.isArray(points) ? points : []) {
    const lat = n(pt?.lat, NaN);
    const lng = n(pt?.lng ?? pt?.lon, NaN);
    const temp = n(pt?.water_temp_f ?? pt?.surface_temp_f, NaN);
    if (!Number.isFinite(lat) || !Number.isFinite(lng) || !Number.isFinite(temp)) continue;
    const d = Math.hypot(lat - center.lat, lng - center.lng);
    if (d <= maxDeg) ranked.push({ ...pt, lat, lng, lon: lng, __lakeNearestDeg: d, estimated: pt.estimated === true, confidence: pt.confidence || 'nearest-grid' });
  }
  ranked.sort((a, b) => (a.__lakeNearestDeg || 0) - (b.__lakeNearestDeg || 0));
  return ranked.slice(0, range < 350000 ? 3 : 1);
}

function temperaturePointsForLake(points, lake, range) {
  const shorelinePath = normalizePath(lake?.path || [], 0);
  if (shorelinePath.length < 3) return [];
  const selected = [];
  for (const pt of Array.isArray(points) ? points : []) {
    const lat = n(pt?.lat, NaN);
    const lng = n(pt?.lng ?? pt?.lon, NaN);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) continue;
    if (pointInPolygon(lat, lng, shorelinePath)) selected.push({ ...pt, lat, lng, lon: lng });
  }
  const center = featureTempPoint(lake, 0);
  if (center && isRealTemperaturePoint(center)) selected.push(center);
  let realSelected = selected.filter(isRealTemperaturePoint);
  if (!realSelected.length) realSelected = nearestTemperaturePointsForLake(points, lake, range).filter(isRealTemperaturePoint);
  return selectTemperaturePoints(realSelected, range).slice(0, range < 350000 ? 3 : 1);
}

function appendElements(map3DElement, elements, created) {
  const frag = document.createDocumentFragment();
  let made = 0;
  for (const el of elements || []) {
    if (!el) continue;
    try { frag.append(el); created.push(el); made += 1; } catch (_) {}
  }
  if (frag.childNodes.length) {
    try { map3DElement.append(frag); } catch (_) {}
  }
  return made;
}



function inlandBaitRenderAllowed(payload, range) {
  const tier = String(payload?.scene_tier || payload?.sceneTier || payload?.tier || window.__gfsSceneTier || '').toLowerCase();
  if (payload?.inland_bait_render_allowed === true) return true;
  if (payload?.overview_only === true) return false;
  if (['harbor', 'local', 'coastal', 'regional'].includes(tier)) return true;
  // Range fallback when tier metadata is missing: keep global/world views outline+temp only.
  return Number(range || 0) > 0 && Number(range) < 650000;
}

function lakeBaitDetailProfile(range, lakeBounds) {
  const width = Math.max(0.0001, Math.abs((lakeBounds?.east ?? 0) - (lakeBounds?.west ?? 0)));
  const height = Math.max(0.0001, Math.abs((lakeBounds?.north ?? 0) - (lakeBounds?.south ?? 0)));
  const span = Math.max(width, height);
  if (range < 45000) {
    return { maxGrid: 120, maxGapDeg: Math.min(0.014, span / 26), capPerBand: 72, shorelineSamples: 72, jitterAmp: 0.14, capTotal: 240 };
  }
  if (range < 100000) {
    return { maxGrid: 96, maxGapDeg: Math.min(0.020, span / 22), capPerBand: 58, shorelineSamples: 54, jitterAmp: 0.11, capTotal: 190 };
  }
  if (range < 220000) {
    return { maxGrid: 76, maxGapDeg: Math.min(0.028, span / 16), capPerBand: 42, shorelineSamples: 38, jitterAmp: 0.09, capTotal: 140 };
  }
  if (range < 600000) {
    return { maxGrid: 54, maxGapDeg: Math.min(0.042, span / 10), capPerBand: 28, shorelineSamples: 24, jitterAmp: 0.06, capTotal: 92 };
  }
  return { maxGrid: 34, maxGapDeg: Math.min(0.070, span / 6), capPerBand: 16, shorelineSamples: 14, jitterAmp: 0.04, capTotal: 56 };
}

function stableNoise01(seed) {
  const x = Math.sin(Number(seed) * 12.9898 + 78.233) * 43758.5453;
  return x - Math.floor(x);
}

function variedLakeBaitScore(baseScore, lakeIndex, sampleIndex, tempF, range) {
  const base = Number.isFinite(Number(baseScore)) ? Number(baseScore) : baitScoreFromTemp(tempF);
  if (!Number.isFinite(base)) return NaN;
  const detail = range < 100000 ? 1.0 : (range < 220000 ? 0.75 : 0.45);
  const n1 = stableNoise01((lakeIndex + 1) * 1009 + (sampleIndex + 1) * 917);
  const n2 = stableNoise01((lakeIndex + 1) * 577 + (sampleIndex + 1) * 313);
  const wiggle = ((n1 - 0.5) * 0.70 + (n2 - 0.5) * 0.35) * detail;
  return Math.max(0.5, Math.min(5, base + wiggle));
}

function cellPolygonFromCenter(centerLat, centerLng, halfLat, halfLng, shorelinePath, center) {
  const raw = [
    { lat: centerLat - halfLat, lng: centerLng - halfLng },
    { lat: centerLat - halfLat, lng: centerLng + halfLng },
    { lat: centerLat + halfLat, lng: centerLng + halfLng },
    { lat: centerLat + halfLat, lng: centerLng - halfLng },
  ];
  const out = [];
  for (const pt of raw) {
    const fixed = pointInPolygon(pt.lat, pt.lng, shorelinePath) ? pt : inwardProjectToPolygon(pt.lat, pt.lng, shorelinePath, center);
    if (!fixed) continue;
    out.push([fixed.lng, fixed.lat]);
  }
  if (out.length < 3) return null;
  out.push([...out[0]]);
  return out;
}

function inverseDistanceWeightedScore(lat, lng, samples) {
  let wsum = 0;
  let ssum = 0;
  for (const pt of Array.isArray(samples) ? samples : []) {
    const plat = n(pt?.lat, NaN);
    const plng = n(pt?.lng ?? pt?.lon, NaN);
    const score = n(pt?.value ?? pt?.bait_score, NaN);
    if (!Number.isFinite(plat) || !Number.isFinite(plng) || !Number.isFinite(score)) continue;
    const dx = (plng - lng);
    const dy = (plat - lat);
    const d2 = (dx * dx) + (dy * dy);
    const w = d2 < 1e-9 ? 1e6 : (1 / Math.pow(d2, 0.85));
    wsum += w;
    ssum += score * w;
  }
  return wsum > 0 ? (ssum / wsum) : NaN;
}

function classifyLakeCellBand(score, range) {
  if (!Number.isFinite(score)) return null;
  if (range < 100000) {
    if (score >= 4.15) return 'hot';
    if (score >= 3.45) return 'good';
    if (score >= 2.9) return 'fair';
    if (score >= 2.25) return 'cool-fair';
    return null;
  }
  if (score >= 4.15) return 'hot';
  if (score >= 3.35) return 'good';
  if (score >= 2.5) return 'fair';
  return null;
}

function thermalMarchingBaitContours({ polygons, temperaturePoints, bbox, range }) {
  if (!Array.isArray(polygons) || !polygons.length) return [];
  if (!Array.isArray(temperaturePoints) || temperaturePoints.length < 1) return [];
  const cells = [];
  let polyIdx = 0;
  for (const lake of polygons) {
    const lakeIndex = polyIdx;
    const shorelinePath = normalizePath(lake?.path || [], 0);
    if (shorelinePath.length < 3) { polyIdx += 1; continue; }
    const lakeBounds = boundsFromPath(shorelinePath);
    const lakeCenter = featureTempPoint(lake, polyIdx++) || centroidFromRing(shorelinePath);
    if (!lakeBounds || !lakeCenter) continue;
    if (!Array.isArray(temperaturePoints) || !temperaturePoints.some(isRealTemperaturePoint)) continue;

    const profile = lakeBaitDetailProfile(range, lakeBounds);
    const width = Math.max(0.0001, lakeBounds.east - lakeBounds.west);
    const height = Math.max(0.0001, lakeBounds.north - lakeBounds.south);
    const nx = Math.max(4, Math.min(profile.maxGrid, Math.round(Math.max(6, width / Math.max(0.0001, profile.maxGapDeg * 0.34)))));
    const ny = Math.max(4, Math.min(profile.maxGrid, Math.round(Math.max(6, height / Math.max(0.0001, profile.maxGapDeg * 0.34)))));
    const cellLng = width / nx;
    const cellLat = height / ny;
    const halfLng = cellLng * 0.48;
    const halfLat = cellLat * 0.48;

    const lakeSamples = [];
    let sampleIndex = 0;
    for (const pt of temperaturePoints) {
      const plat = n(pt?.lat, NaN);
      const plng = n(pt?.lng ?? pt?.lon, NaN);
      if (!Number.isFinite(plat) || !Number.isFinite(plng)) continue;
      if (!pointInPolygon(plat, plng, shorelinePath)) continue;
      const baseScore = Number.isFinite(Number(pt?.bait_score)) ? Number(pt.bait_score) : baitScoreFromTemp(pt?.water_temp_f ?? pt?.surface_temp_f);
      const score = variedLakeBaitScore(baseScore, lakeIndex, sampleIndex++, pt?.water_temp_f ?? pt?.surface_temp_f, range);
      if (!Number.isFinite(score)) continue;
      lakeSamples.push({ ...pt, lat: plat, lng: plng, lon: plng, value: score, bait_score: score });
    }
    const centerBaseScore = Number.isFinite(Number(lakeCenter.bait_score)) ? Number(lakeCenter.bait_score) : baitScoreFromTemp(lakeCenter.water_temp_f);
    const centerScore = variedLakeBaitScore(centerBaseScore, lakeIndex, sampleIndex++, lakeCenter.water_temp_f, range);
    if (Number.isFinite(centerScore)) lakeSamples.push({ ...lakeCenter, lon: lakeCenter.lng ?? lakeCenter.lon, value: centerScore, bait_score: centerScore });

    const shoreStep = Math.max(1, Math.floor(shorelinePath.length / Math.max(6, profile.shorelineSamples)));
    for (let i = 0; i < shorelinePath.length; i += shoreStep) {
      const v = shorelinePath[i];
      if (!Number.isFinite(centerBaseScore)) continue;
      const varied = variedLakeBaitScore(centerBaseScore, lakeIndex, sampleIndex++, lakeCenter.water_temp_f, range);
      lakeSamples.push({ ...lakeCenter, lat: v.lat, lng: v.lng, lon: v.lng, value: varied, bait_score: varied });
      if (range < 220000) {
        const t = 0.35 + stableNoise01((lakeIndex + 11) * 701 + i) * 0.30;
        const ilat = lakeCenter.lat + (v.lat - lakeCenter.lat) * t;
        const ilng = lakeCenter.lng + (v.lng - lakeCenter.lng) * t;
        if (pointInPolygon(ilat, ilng, shorelinePath)) {
          const varied2 = variedLakeBaitScore(centerBaseScore, lakeIndex, sampleIndex++, lakeCenter.water_temp_f, range);
          lakeSamples.push({ ...lakeCenter, lat: ilat, lng: ilng, lon: ilng, value: varied2, bait_score: varied2 });
        }
      }
    }
    if (lakeSamples.length < 3) continue;

    let madeForLake = 0;
    for (let iy = 0; iy < ny; iy += 1) {
      const lat = lakeBounds.south + ((iy + 0.5) * cellLat);
      for (let ix = 0; ix < nx; ix += 1) {
        const lng = lakeBounds.west + ((ix + 0.5) * cellLng);
        if (!pointInPolygon(lat, lng, shorelinePath)) continue;
        const baseScore = inverseDistanceWeightedScore(lat, lng, lakeSamples);
        const score = variedLakeBaitScore(baseScore, lakeIndex, sampleIndex++, lakeCenter.water_temp_f, range);
        const band = classifyLakeCellBand(score, range);
        if (!band) continue;
        const polyCoords = cellPolygonFromCenter(lat, lng, halfLat, halfLng, shorelinePath, lakeCenter);
        if (!polyCoords) continue;
        cells.push({
          coordinates: polyCoords,
          band,
          value: score,
          probability: score,
          cells: 1,
          source: 'inland_lake_advanced_bait_grid_squares',
          renderer: 'inland_advanced_bait_grid_squares_positive_depth_extrusion',
          style: 'bright_green_fill_orange_glow_outline',
          lake_name: lake?.name || null,
          grid_cell: true,
          grid_size: { nx, ny },
          cell_center: { lat, lng },
          water_temp_f: lakeCenter.water_temp_f,
          current_speed_mph: n(lake?.current_speed_mph ?? lakeCenter?.current_speed_mph, NaN),
          current_speed_m_s: n(lake?.current_speed_m_s ?? lakeCenter?.current_speed_m_s, NaN),
          current_heading_deg: n(lake?.current_heading_deg ?? lakeCenter?.current_heading_deg, NaN),
          speed_mph: n(lake?.speed_mph ?? lakeCenter?.speed_mph, NaN),
          speed_m_s: n(lake?.speed_m_s ?? lakeCenter?.speed_m_s, NaN),
          heading_deg: n(lake?.heading_deg ?? lakeCenter?.heading_deg, NaN),
          bait_depth_ft: n(lake?.bait_depth_ft ?? lakeCenter?.bait_depth_ft, NaN),
          estimated_mean_depth_ft: n(lake?.estimated_mean_depth_ft ?? lakeCenter?.estimated_mean_depth_ft, NaN),
          preferred_bait_depth_ft: n(lake?.bait_depth_ft ?? lakeCenter?.bait_depth_ft, NaN),
          bait_depth_band_ft: lake?.bait_depth_band_ft ?? lakeCenter?.bait_depth_band_ft ?? null,
          extrude_above_water: true,
          colorado_corridor: lake?.colorado_corridor === true || lakeCenter?.colorado_corridor === true,
        });
        madeForLake += 1;
        if (madeForLake >= profile.capTotal) break;
      }
      if (madeForLake >= profile.capTotal) break;
    }
  }
  const globalCap = range < 45000 ? 320 : (range < 100000 ? 240 : (range < 220000 ? 180 : (range < 600000 ? 120 : 72)));
  return cells.slice(0, globalCap);
}

function makeTempLabelMarker(pt, map3DElement) {
  const temp = n(pt?.water_temp_f ?? pt?.surface_temp_f, NaN);
  const lat = n(pt?.lat, NaN);
  const lng = n(pt?.lng ?? pt?.lon, NaN);
  if (!Number.isFinite(temp) || !Number.isFinite(lat) || !Number.isFinite(lng)) return null;
  const range = cameraRangeMeters(map3DElement);
  const fontSize = tempLabelFontSize(range);
  if (!isRealTemperaturePoint(pt)) return null;
  const label = `${Math.round(temp)}°F`;
  const width = Math.max(50, label.length * fontSize * 0.68);
  const height = fontSize + 14;
  const safeTitle = `${pt.name || 'Inland water'} surface temp`;
  const svg = `
    <svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">
      <filter id="glow"><feGaussianBlur stdDeviation="2.4" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
      <ellipse cx="${width/2}" cy="${height/2}" rx="${width*0.44}" ry="${height*0.30}" fill="rgba(0,30,60,.18)"/>
      <text x="${width/2}" y="${height/2 + fontSize*0.34}" text-anchor="middle"
        font-family="Inter, Arial, sans-serif" font-size="${fontSize}" font-weight="950"
        stroke="#ff7a00" stroke-width="7" fill="#009dff" paint-order="stroke fill"
        filter="url(#glow)">${label}</text>
    </svg>`;
  const marker = document.createElement('gmp-marker-3d');
  const altitude = tempLabelAltitude(range);
  try { marker.position = { lat, lng, altitude }; } catch (_) {}
  try { marker.setAttribute('position', `${lat},${lng},${altitude}`); } catch (_) {}
  try { marker.setAttribute('altitude-mode', 'relative-to-ground'); } catch (_) {}
  try { marker.setAttribute('draws-when-occluded', 'true'); } catch (_) {}
  try { marker.setAttribute('data-gfs-layer', 'inland-water'); } catch (_) {}
  try { marker.setAttribute('data-inland-water-kind', 'temp-label'); } catch (_) {}
  const template = document.createElement('template');
  template.innerHTML = svg.trim();
  marker.append(template);
  try {
    attachPolygonHover(marker, {
      title: safeTitle,
      lines: [
        `Water temp: ${temp.toFixed(1)} °F`,
        `Source: ${pt.source || pt.temperature_source || (pt?.estimated ? 'freshwater estimate' : 'real_surface_temperature')}`,
        `Confidence: ${pt.confidence || (pt?.estimated ? 'estimated' : 'medium')}`,
        Number.isFinite(n(pt?.speed_mph, NaN)) ? `Surface wind: ${fmtMph(pt.speed_mph)} @ ${fmtDeg(pt.heading_deg)}` : 'Surface wind: unavailable',
        Number.isFinite(n(pt?.current_speed_mph, NaN)) ? `Current: ${fmtMph(pt.current_speed_mph)} @ ${fmtDeg(pt.current_heading_deg)}` : 'Current: heuristic pending',
        Number.isFinite(n(pt?.bait_depth_ft, NaN)) ? `Bait depth: ${fmtFt(pt.bait_depth_ft)}` : 'Bait depth: pending',
      ],
      metrics: { layer: 'inland-water', kind: 'temp-label', source: pt.source || pt.temperature_source || 'real_surface_temperature' },
      payload: pt,
    });
  } catch (_) {}
  return marker;
}

function inlandBatchSize(range, total) {
  // Better render allotment: geometry batches are intentionally larger so a
  // 200-ish lake payload lands in 1–2 frames instead of 4–5 slow batches.
  if (total <= 260) return 260;
  if (range < 80000) return 190;
  if (range < 250000) return 160;
  return 132;
}

function scheduleProgressiveAppend({ factories, map3DElement, created, range, onDone }) {
  const state = { cancelled: false, index: 0, timer: null, raf: null };
  const batchSize = inlandBatchSize(range, factories.length);
  const run = () => {
    if (state.cancelled || !map3DElement) return;
    const frag = document.createDocumentFragment();
    let made = 0;
    while (state.index < factories.length && made < batchSize) {
      const fn = factories[state.index++];
      try {
        const result = fn();
        const els = Array.isArray(result) ? result : [result];
        for (const el of els) {
          if (!el) continue;
          frag.append(el);
          created.push(el);
          made += 1;
        }
      } catch (err) {
        if (made < 3) console.warn('[gfs inland-water] skipped draw factory', err);
      }
    }
    if (frag.childNodes.length) {
      try { map3DElement.append(frag); } catch (_) {}
    }
    try {
      window.__gfsDebugEvent?.('inland-water/progressive-batch', {
        index: state.index, total: factories.length, made, batchSize, rendered: created.length, done: state.index >= factories.length,
      });
    } catch (_) {}
    if (state.index < factories.length && !state.cancelled) {
      state.raf = window.requestAnimationFrame ? window.requestAnimationFrame(run) : null;
      if (!state.raf) state.timer = window.setTimeout(run, 16);
    } else if (!state.cancelled && typeof onDone === 'function') {
      try { onDone(created.length); } catch (_) {}
    }
  };
  // Render the first shoreline batch synchronously so RendererLayer can swap
  // old/new scenes without a blank RAF gap. Follow-up batches still yield.
  run();
  return () => {
    state.cancelled = true;
    if (state.raf && window.cancelAnimationFrame) { try { window.cancelAnimationFrame(state.raf); } catch (_) {} }
    if (state.timer) { try { window.clearTimeout(state.timer); } catch (_) {} }
  };
}

export function renderInlandWaterLayer({ payload, map3DElement }) {
  const created = [];
  const cancelers = [];
  if (!map3DElement || !payload) return () => {};
  const polygons = Array.isArray(payload.polygons) ? payload.polygons : [];
  const lines = Array.isArray(payload.lines) ? payload.lines : [];
  const hasTempOnly = Array.isArray(payload.temperature_points) && payload.temperature_points.length > 0;
  const isEmptyGeometry = polygons.length === 0 && lines.length === 0;
  if (isEmptyGeometry && !hasTempOnly) {
    const source = String(payload?.source || payload?.status || 'empty').toLowerCase();
    const disposer = () => {};
    disposer.__gfsKeepExisting = true;
    try {
      window.__gfsDebugEvent?.('inland-water/preserve-empty-geometry', {
        source: payload?.source || payload?.status || 'unknown',
        status: payload?.status || null,
        cacheMode: payload?.cache?.mode || null,
        policy: 'keep_last_good_shoreline_until_real_geometry_arrives',
      });
    } catch (_) {}
    if (/cache_miss|missing|empty|warming|runtime_nhd/.test(source)) return disposer;
  }
  // Round synthetic boil zones are intentionally disabled. Inland bait is now
  // drawn only as temperature-driven marching-square contours.
  // Visual style: many smaller bright-green bait squares with orange glow outlines,
  // positively extruded above the water from inland bait depth.
  const rawBaitZones = [];
  const baitByName = new Map((Array.isArray(payload.bait?.targets) ? payload.bait.targets : []).map((x) => [String(x.name || '').toLowerCase(), x]));
  const conditionsByName = new Map((Array.isArray(payload.conditions?.items) ? payload.conditions.items : []).map((x) => [String(x.name || '').toLowerCase(), x]));
  let cameraRange = 1800000;
  let candidateTempPoints = [];
  let temperaturePoints = [];
  let thermalContours = [];
  try {
    cameraRange = cameraRangeMeters(map3DElement);
    candidateTempPoints = temperaturePointsFromPayload({ polygons, bait: payload.bait, conditions: payload.conditions, payload });
    temperaturePoints = selectTemperaturePoints(candidateTempPoints, cameraRange);
    const baitAllowed = inlandBaitRenderAllowed(payload, cameraRange);
    thermalContours = baitAllowed && polygons.length ? thermalMarchingBaitContours({ polygons, temperaturePoints: candidateTempPoints, bbox: payload.bbox || payload.bbox_used, range: cameraRange }) : [];
  } catch (err) {
    console.warn('[gfs inland-water] temp/bait enrichment setup failed; preserving shoreline render', { message: err?.message || String(err) });
    try { window.__gfsDebugEvent?.('inland-water/enrichment-setup-failed', { message: err?.message || String(err), policy: 'shoreline_geometry_continues_without_temp_bait' }); } catch (_) {}
  }

  // Lake-by-lake two-stage Inland Waters render:
  // 1) each lake shoreline ring is drawn as soon as its factory runs;
  // 2) that same lake immediately schedules its own temp label + clipped
  //    marching-square bait, so enrichment follows geometry progressively.
  const geometryFactories = [];
  const enrichmentTimers = [];

  let baitZoneFailures = 0;
  let enrichmentScheduled = 0;
  let enrichmentRendered = 0;

  const scheduleLakeEnrichment = (lake, lakeIndex) => {
    if (!lake || !Array.isArray(lake.path) || lake.path.length < 3) return;
    const delay = Math.max(70, Math.min(420, Math.round(cameraRange / 3400))) + ((lakeIndex % 5) * 28);
    const timer = window.setTimeout(() => {
      const lakeTemps = temperaturePointsForLake(candidateTempPoints, lake, cameraRange);
      const lakeBounds = lake?.lake_bounds || boundsFromPath(lake?.path || []);
      const baitAllowed = inlandBaitRenderAllowed(payload, cameraRange);
      const lakeContours = baitAllowed ? thermalMarchingBaitContours({
        polygons: [lake],
        temperaturePoints: lakeTemps.length ? lakeTemps : candidateTempPoints,
        bbox: lakeBounds || payload.bbox || payload.bbox_used,
        range: cameraRange,
      }) : [];
      const elements = [];
      for (const poly of lakeContours) {
        try {
          const el = makeThermalBaitContourPolygon(poly);
          if (el) elements.push(el);
        } catch (err) {
          baitZoneFailures += 1;
          if (baitZoneFailures <= 3) console.warn('[gfs inland-water] skipped malformed lake-by-lake thermal contour', { message: err?.message || String(err), band: poly?.band || null });
        }
      }
      for (const pt of lakeTemps) {
        const el = makeTempLabelMarker(pt, map3DElement);
        if (el) elements.push(el);
      }
      const made = appendElements(map3DElement, elements, created);
      enrichmentRendered += made;
      try {
        window.__gfsDebugEvent?.('inland-water/lake-enrichment', {
          lake: lake?.name || null,
          index: lakeIndex,
          made,
          contours: lakeContours.length,
          tempLabels: lakeTemps.length,
          source: payload.source,
        });
      } catch (_) {}
    }, delay);
    enrichmentScheduled += 1;
    enrichmentTimers.push(timer);
    cancelers.push(() => { try { window.clearTimeout(timer); } catch (_) {} });
  };

  polygons.forEach((p, lakeIndex) => {
    geometryFactories.push(() => {
      const key = String(p.name || '').toLowerCase();
      const merged = { ...p, ...(conditionsByName.get(key) || {}), ...(baitByName.get(key) || {}) };
      const el = makeWaterPolygon(merged, lakeIndex);
      if (el) {
        try { if (lakeIndex < 6 || lakeIndex % 24 === 0) window.__gfsDebugEvent?.('inland-water/shoreline-complete', { lake: merged?.name || null, index: lakeIndex, mode: 'shoreline_only_single_teal_line', trigger: 'start_temp_and_bait_enrichment', sampled: true }); } catch (_) {}
        scheduleLakeEnrichment(merged, lakeIndex);
      }
      return el;
    });
  });
  for (const l of lines) {
    geometryFactories.push(() => {
      const path = normalizePath(l.path, 18);
      if (path.length < 2) return null;
      const key = String(l.name || '').toLowerCase();
      const merged = { ...l, ...(conditionsByName.get(key) || {}), ...(baitByName.get(key) || {}) };
      const lineEls = makeGlowingInlandLine({ path, altitude: 18, coreWidth: 3.25, hover: waterHover(merged, path, 'flowline'), kind: 'flowline' });
      for (const lineEl of lineEls) {
        try { lineEl?.setAttribute?.('data-inland-id', stableInlandFeatureId(merged, 'flowline', geometryFactories.length)); } catch (_) {}
        try { lineEl?.setAttribute?.('data-inland-geometry-hash', stableInlandFeatureHash(merged, path)); } catch (_) {}
      }
      return lineEls;
    });
  }

  const cancelGeometry = scheduleProgressiveAppend({
    factories: geometryFactories,
    map3DElement,
    created,
    range: cameraRange,
    onDone: (rendered) => {
      try {
        window.__gfsDebugEvent?.('inland-water/geometry-complete', {
          rendered,
          geometryFactories: geometryFactories.length,
          enrichmentScheduled,
          enrichmentRendered,
          source: payload.source,
          mode: 'shoreline_complete_lake_by_lake_enrichment',
        });
      } catch (_) {}
    },
  });
  cancelers.push(cancelGeometry);
  try {
    window.__gfsDebugEvent?.('inland-water/render', {
      polygons: polygons.length,
      lines: lines.length,
      unfilteredCount: payload.unfiltered_count || null,
      prominenceFilter: payload.prominence_filter || null,
      worldQuantityFilter: payload.world_quantity_filter || payload.diagnostics?.quantity_filter || null,
      rawVerticesPreserved: true,
      baitZones: inlandBaitRenderAllowed(payload, cameraRange) ? 'lake_by_lake_shoreline_complete' : 'zoom_gated_outline_temp_only',
      baitZoneFailures,
      tempLabels: 'lake_by_lake_shoreline_complete',
      geometryScheduled: geometryFactories.length,
      enrichmentScheduled: polygons.length,
      source: payload.source,
      progressive: true,
      twoStage: true,
      lakeByLake: true,
      polygonPolicy: 'shoreline_only_single_teal_line_temp_and_bait_are_lake_bound_child_overlays',
      cacheQuality: payload.cache_quality || null,
    });
  } catch (_) {}
  console.info('[gfs inland-water] lake-by-lake render scheduled', {
    polygons: polygons.length,
    lines: lines.length,
    unfilteredCount: payload.unfiltered_count || null,
    prominenceFilter: payload.prominence_filter || null,
    worldQuantityFilter: payload.world_quantity_filter || payload.diagnostics?.quantity_filter || null,
    rawVerticesPreserved: true,
    rawTempPoints: candidateTempPoints.length,
    range: Math.round(cameraRange),
    geometryScheduled: geometryFactories.length,
    enrichmentScheduled: polygons.length,
    source: payload.source,
    baitTargets: inlandBaitRenderAllowed(payload, cameraRange) ? (payload.bait?.targets?.length || 0) : 0,
    inlandBaitRenderAllowed: inlandBaitRenderAllowed(payload, cameraRange),
    labelMode: cameraRange < 350000 ? 'close_many_small' : 'far_few_large',
    cacheQuality: payload.cache_quality || null,
  });
  const disposer = () => {
    enrichmentTimers.forEach((timer) => { try { window.clearTimeout(timer); } catch (_) {} });
    cancelers.forEach((fn) => { try { fn(); } catch (_) {} });
    created.forEach((el) => { try { el.remove(); } catch (_) {} });
  };
  // Tell RendererLayer this was a real draw. Without this flag, clearBeforeRender=false
  // layers can fail to track their disposer/signature and will flash/remap on each
  // cache heartbeat instead of holding the last-good lake scene.
  disposer.__gfsDidRender = true;
  return disposer;
}
