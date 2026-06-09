import { createPolygon3D } from './polygon3d.js';
import { contourPolygonsFromPoints } from './marching_squares.js';

const SHARK_RENDER_STATE = {
  polygons: new Map(),
  lastVersion: null,
};

function toNumber(v, fallback = 0) {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

function clamp01(v) { return Math.max(0, Math.min(1, toNumber(v, 0))); }

function finitePath(path) {
  const out = [];
  for (const p of Array.isArray(path) ? path : []) {
    const lat = Number(p?.lat ?? p?.latitude);
    const lng = Number(p?.lng ?? p?.lon ?? p?.longitude);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) continue;
    if (lat < -90 || lat > 90 || lng < -540 || lng > 540) continue;
    let lon = lng;
    while (lon < -180) lon += 360;
    while (lon >= 180) lon -= 360;
    out.push({ lat, lng: lon, altitude: Number(p?.altitude ?? p?.altitude_m ?? 0) || 0 });
  }
  return out.length >= 3 ? out : [];
}


function contourHasHycomSstTruth(contour) {
  const method = String(contour?.land_mask?.method || contour?.metrics?.sst_mask_source || contour?.sst_mask_source || '').toLowerCase();
  if (method.includes('hycom') && method.includes('sst') && !method.includes('proxy') && !method.includes('fallback')) return true;
  return false;
}

function payloadAllowsFrontendRecontour(payload) {
  return payload?.marching_squares?.frontend_can_recontour_score_points === true && String(payload?.sst_land_mask?.policy || '').toLowerCase().includes('hycom');
}

function bboxFromPayload(payload) {
  const b = payload?.bbox_used || payload?.bbox || payload?.visible_bbox;
  if (Array.isArray(b) && b.length >= 4) return { west: Number(b[0]), south: Number(b[1]), east: Number(b[2]), north: Number(b[3]) };
  if (b && typeof b === 'object') return { west: Number(b.west), south: Number(b.south), east: Number(b.east), north: Number(b.north) };
  return null;
}

function contourStyle(contour) {
  const p = clamp01(contour?.probability ?? contour?.score);
  const species = String(contour?.species || '').toLowerCase();
  const sizeClass = String(contour?.size_class || '').toLowerCase();
  const band = String(contour?.band || '').toLowerCase();
  let fillColor = '#42f5d7';
  let strokeColor = '#d7fff7';
  let fillOpacity = 0.08 + p * 0.25;
  let strokeWidth = 2.4 + p * 4.2;
  if (species.includes('tiger')) {
    fillColor = '#ff9f0a'; strokeColor = '#ffe066'; fillOpacity = 0.08 + p * 0.22; strokeWidth += 1.2;
  } else if (species.includes('sand')) {
    fillColor = '#d6b86a'; strokeColor = '#fff0a8'; fillOpacity = 0.07 + p * 0.20;
  } else if (sizeClass.includes('undersize') || band === 'caution') {
    fillColor = '#5f6bff'; strokeColor = '#ff5fd7'; fillOpacity = 0.055 + p * 0.18; strokeWidth = 2.8;
  } else if (sizeClass.includes('large') || band === 'large') {
    fillColor = '#bf5af2'; strokeColor = '#f7d7ff'; fillOpacity = 0.07 + p * 0.22; strokeWidth += 0.8;
  } else if (band === 'core') {
    fillColor = '#00ffd1'; strokeColor = '#ffffff'; fillOpacity = 0.16 + p * 0.36; strokeWidth += 1.6;
  } else if (band === 'inner') {
    fillColor = '#00d8ff'; strokeColor = '#b8fff4'; fillOpacity = 0.11 + p * 0.28;
  }
  return { fillColor, strokeColor, fillOpacity: clamp01(fillOpacity), strokeOpacity: 0.96, strokeWidth, extrudedHeight: 0, neonGlow: true };
}

function hoverText(contour) {
  const m = contour?.metrics || {};
  const d = contour?.depth_intel || {};
  const legal = contour?.legal || {};
  const species = String(contour?.species || 'shark');
  const size = contour?.size_class === 'primeSlot' ? '36–42 in prime slot' : String(contour?.size_class || 'watch');
  const mode = contour?.fishing_mode || m.fishing_mode || 'shore/pier/dock/ocean';
  const p = Math.round(clamp01(contour?.probability) * 100);
  const swim = Array.isArray(d.target_swim_depth_ft) ? `${d.target_swim_depth_ft[0]}–${d.target_swim_depth_ft[1]} ft` : 'estimated';
  return [
    `Shark Intel: ${species}`,
    `Size: ${size}`,
    `Probability: ${p}%`,
    `Mode: ${mode}`,
    `Bottom depth: ${d.bottom_depth_ft ?? m.bottom_depth_ft ?? 'n/a'} ft`,
    `Likely swim depth: ${swim}`,
    `SST: ${m.sst_f ?? 'n/a'} °F`,
    `Current: ${m.current_kt ?? 'n/a'} kt`,
    `Wind: ${m.wind_kt ?? 'n/a'} kt`,
    `Shore distance: ${m.distance_to_shore_yd ?? 'n/a'} yd`,
    `SST land mask: ${String(m.sst_mask_source || contour?.land_mask?.method || 'pending').replace(/_/g, ' ')}`,
    `Legal/caution: ${legal.status || 'check local rules / MPA'}`,
  ].join('\n');
}

function normalizeContours(payload) {
  const direct = Array.isArray(payload?.contours) ? payload.contours : (Array.isArray(payload?.polygons) ? payload.polygons : []);
  const strictDirect = direct.filter(contourHasHycomSstTruth);
  if (strictDirect.length) return strictDirect;
  const points = Array.isArray(payload?.score_points) ? payload.score_points : [];
  if (!points.length || !payloadAllowsFrontendRecontour(payload)) return [];
  const bbox = bboxFromPayload(payload);
  try {
    const generated = contourPolygonsFromPoints({
      points,
      valueAccessor: (row) => Number(row.leopard_slot_score ?? row.score ?? 0),
      thresholds: [0.42, 0.58, 0.72],
      bbox,
      maxGrid: 34,
      capPerBand: 48,
    });
    return (generated || []).map((poly, i) => ({
      ...poly,
      id: poly.id || `shark:leopard:frontend-ms:${i}`,
      species: 'leopard',
      size_class: 'primeSlot',
      fishing_mode: 'shore-pier-dock-ocean',
      probability: poly.probability ?? poly.score ?? 0.5,
      style_key: 'shark-leopard-prime-frontend-ms',
      path: poly.path || poly.ring || poly.coords,
      metrics: { ...(poly.metrics || {}), sst_mask_source: 'hycom_valid_sst_neighbor' },
      land_mask: { valid: true, method: 'hycom_valid_sst_neighbor' },
      depth_intel: poly.depth_intel || {},
    })).filter(contourHasHycomSstTruth);
  } catch (err) {
    console.info('[gfs shark] frontend marching-squares fallback skipped', err?.message || err);
    return [];
  }
}

export function clearSharkIntelLayer() {
  for (const entry of SHARK_RENDER_STATE.polygons.values()) {
    try { entry.el?.remove?.(); } catch (_) {}
  }
  SHARK_RENDER_STATE.polygons.clear();
  SHARK_RENDER_STATE.lastVersion = null;
}

export function renderSharkIntelLayer({ payload, map3DElement }) {
  if (!map3DElement || !payload) return () => {};
  const version = payload?.cache?.version || payload?.cache_quality?.version || payload?.version || payload?.resolved_time || payload?.ts || null;
  const contours = normalizeContours(payload).slice(0, Number(window.GFS_SHARK_MAX_CONTOURS || 150));
  if (!contours.length) return () => {};
  const wanted = new Set();
  contours.forEach((contour, idx) => {
    const path = finitePath(contour.path || contour.ring || contour.coords);
    if (path.length < 3) return;
    const hash = contour.geometry_hash || contour.hash || path.map((p) => `${p.lat.toFixed(4)},${p.lng.toFixed(4)}`).join(';');
    const id = String(contour.id || `shark:${contour.species || 'species'}:${contour.size_class || 'slot'}:${idx}`);
    wanted.add(id);
    const existing = SHARK_RENDER_STATE.polygons.get(id);
    if (existing && existing.hash === hash && existing.version === version) return;
    if (existing?.el) { try { existing.el.remove(); } catch (_) {} }
    const style = contourStyle(contour);
    const el = createPolygon3D({
      path,
      altitude: 0,
      altitudeMode: 'relative',
      ...style,
      hover: {
        title: hoverText(contour),
        payload: { layer: 'shark-intel', contour, target: payload.target, species: payload.species },
      },
      preferAttributePath: true,
    });
    if (!el) return;
    try {
      el.setAttribute('data-gfs-layer', 'shark-intel');
      el.setAttribute('data-shark-species', String(contour.species || 'shark'));
      el.setAttribute('data-shark-size-class', String(contour.size_class || 'watch'));
      el.setAttribute('data-shark-fishing-mode', String(contour.fishing_mode || 'all'));
    } catch (_) {}
    try { map3DElement.append(el); } catch (err) { console.warn('[gfs shark] append failed', err); return; }
    SHARK_RENDER_STATE.polygons.set(id, { el, hash, version });
  });
  // Fade policy later; for now remove stale entries when a fresh shark payload succeeds.
  for (const [id, entry] of [...SHARK_RENDER_STATE.polygons.entries()]) {
    if (!wanted.has(id)) {
      try { entry.el?.remove?.(); } catch (_) {}
      SHARK_RENDER_STATE.polygons.delete(id);
    }
  }
  SHARK_RENDER_STATE.lastVersion = version;
  console.info('[gfs shark] render', { contours: contours.length, visible: SHARK_RENDER_STATE.polygons.size, version, source: payload.source });
  const dispose = () => clearSharkIntelLayer();
  dispose.__gfsKeepExisting = true;
  dispose.__gfsDidRender = true;
  return dispose;
}


function geoDistanceNmLocal(lat1, lon1, lat2, lon2) {
  const a1 = Number(lat1), o1 = Number(lon1), a2 = Number(lat2), o2 = Number(lon2);
  if (![a1, o1, a2, o2].every(Number.isFinite)) return NaN;
  const meanLat = ((a1 + a2) / 2) * Math.PI / 180;
  return Math.hypot((a2 - a1) * 60, (o2 - o1) * 60 * Math.cos(meanLat));
}

function nearestByDistance(rows, lat, lon, maxNm = Infinity) {
  if (!Array.isArray(rows)) return null;
  let best = null;
  let bestNm = Infinity;
  for (const row of rows) {
    const rlat = Number(row?.lat ?? row?.latitude ?? row?.center?.lat);
    const rlon = Number(row?.lon ?? row?.lng ?? row?.longitude ?? row?.center?.lng);
    if (!Number.isFinite(rlat) || !Number.isFinite(rlon)) continue;
    const nm = geoDistanceNmLocal(lat, lon, rlat, rlon);
    if (Number.isFinite(nm) && nm < bestNm) { best = row; bestNm = nm; }
  }
  return best && bestNm <= maxNm ? { row: best, distanceNm: bestNm } : null;
}

function contourCenter(contour) {
  const path = finitePath(contour?.path || contour?.ring || contour?.coords);
  if (!path.length) return null;
  let lat = 0, lon = 0;
  for (const p of path) { lat += p.lat; lon += p.lng; }
  return { lat: lat / path.length, lon: lon / path.length };
}

function contourRowsForNearest(payload) {
  return normalizeContours(payload).map((contour) => {
    const c = contourCenter(contour);
    return c ? { ...contour, lat: c.lat, lon: c.lon } : null;
  }).filter(Boolean);
}

export function sharkIntelForLocation(payload, loc, options = {}) {
  const lat = Number(loc?.lat ?? loc?.latitude);
  const lon = Number(loc?.lon ?? loc?.lng ?? loc?.longitude);
  if (!payload || !Number.isFinite(lat) || !Number.isFinite(lon)) return null;
  const maxPointNm = Number(options.maxPointNm ?? 28);
  const maxContourNm = Number(options.maxContourNm ?? 36);
  const points = Array.isArray(payload?.score_points) ? payload.score_points : (Array.isArray(payload?.points) ? payload.points : []);
  const pointHit = nearestByDistance(points, lat, lon, maxPointNm);
  const contourHit = nearestByDistance(contourRowsForNearest(payload), lat, lon, maxContourNm);
  const row = contourHit?.row || pointHit?.row || null;
  if (!row) return null;
  const metrics = row.metrics || pointHit?.row?.metrics || {};
  const depth = row.depth_intel || {};
  const leopard = probabilityToPct(pointHit?.row?.leopard_slot_score ?? (row.species === 'leopard' ? row.probability : undefined));
  const tiger = probabilityToPct(pointHit?.row?.tiger_watch_score ?? (String(row.species || '').includes('tiger') ? row.probability : undefined));
  const sand = probabilityToPct(pointHit?.row?.sand_shark_watch_score ?? (String(row.species || '').includes('sand') ? row.probability : undefined));
  const bestPct = [leopard, tiger, sand, probabilityToPct(row.probability ?? row.score)].filter(Number.isFinite).reduce((a, b) => Math.max(a, b), 0);
  const swim = Array.isArray(depth.target_swim_depth_ft || metrics.target_swim_depth_ft)
    ? (depth.target_swim_depth_ft || metrics.target_swim_depth_ft)
    : null;
  const mask = payload?.sst_land_mask || {};
  const maskMethod = metrics.sst_mask_source || row.land_mask?.method || mask.method || 'pending';
  const fishingMode = row.fishing_mode || metrics.fishing_mode || 'shore/pier/dock/ocean';
  const distanceNm = Number(contourHit?.distanceNm ?? pointHit?.distanceNm);
  const summary = `${Math.round(bestPct)}% shark read • ${fishingMode} • ${Number.isFinite(distanceNm) ? `${distanceNm.toFixed(1)} nm from beacon` : 'near beacon'}`;
  const drivers = [
    Number.isFinite(leopard) ? `Leopard 36–42 in prime-slot ${Math.round(leopard)}%` : null,
    Number.isFinite(tiger) && tiger > 0 ? `Tiger migratory warm-water watch ${Math.round(tiger)}%` : null,
    Number.isFinite(sand) && sand > 0 ? `Sand shark / sandy nearshore watch ${Math.round(sand)}%` : null,
    Number.isFinite(Number(metrics.sst_f)) ? `SST ${Number(metrics.sst_f).toFixed(1)} °F • SST land mask ${String(maskMethod).replace(/_/g, ' ')}` : `SST land mask ${String(maskMethod).replace(/_/g, ' ')}`,
    Number.isFinite(Number(metrics.bottom_depth_ft ?? depth.bottom_depth_ft)) ? `Bottom depth ${(Number(metrics.bottom_depth_ft ?? depth.bottom_depth_ft)).toFixed(1)} ft` : null,
    swim ? `Likely shark swim depth ${Number(swim[0]).toFixed(1)}–${Number(swim[1]).toFixed(1)} ft` : null,
    Number.isFinite(Number(metrics.distance_to_shore_yd)) ? `Reach ${Number(metrics.distance_to_shore_yd).toFixed(0)} yd from shore/coastline edge` : null,
    Number.isFinite(Number(metrics.current_kt)) ? `Current ${Number(metrics.current_kt).toFixed(2)} kt` : null,
    Number.isFinite(Number(metrics.wind_kt)) ? `Wind ${Number(metrics.wind_kt).toFixed(1)} kt` : null,
    row.legal?.status ? `Legal/caution: ${String(row.legal.status).replace(/_/g, ' ')}` : 'Check CDFW / MPA / local pier-dock rules',
  ].filter(Boolean);
  return {
    summary,
    drivers,
    row,
    metrics,
    depth,
    leopardPct: leopard,
    tigerPct: tiger,
    sandPct: sand,
    bestPct,
    distanceNm,
    fishingMode,
    mask,
    maskMethod,
  };
}
