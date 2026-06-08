import { createPolygon3D } from './polygon3d.js';
import { contourPolygonsFromPoints } from './marching_squares.js';

const MAX_POLYGONS_PER_FRAME = 96; // tier-aware scene-cache current/boat corridors can carry more local detail
const CURRENT_ZONE_STATE = window.__gfsCurrentZoneState = window.__gfsCurrentZoneState || { polygons: new Map(), lastStats: null };

function n(v, fallback = 0) {
  const out = Number(v);
  return Number.isFinite(out) ? out : fallback;
}
function clamp01(v) { return Math.max(0, Math.min(1, Number(v) || 0)); }

function normalizeCurrentPoint(p, source = 'unknown') {
  if (!p || typeof p !== 'object') return null;
  const lat = n(p.lat ?? p.latitude ?? p.position?.lat, NaN);
  const lon = n(p.lon ?? p.lng ?? p.longitude ?? p.position?.lng ?? p.position?.lon, NaN);
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
  const current = p.current || {};
  const speed = n(p.speedKt ?? p.current_speed_kt ?? current.speedKt ?? current.speed_kt ?? p.speed_kt, NaN);
  const u = n(p.u ?? current.u ?? current.vector_u, NaN);
  const v = n(p.v ?? current.v ?? current.vector_v, NaN);
  const speedFromUv = (Number.isFinite(u) && Number.isFinite(v)) ? Math.hypot(u, v) * 1.9438444924 : NaN;
  const speedKt = Number.isFinite(speed) ? speed : speedFromUv;
  return { ...p, lat, lon, speedKt, current: { ...current, speedKt, u: Number.isFinite(u) ? u : current.u, v: Number.isFinite(v) ? v : current.v }, source };
}

function payloadPoints(payload) {
  const raw = [];
  const add = (items, source) => {
    if (!Array.isArray(items)) return;
    for (const item of items) {
      const p = normalizeCurrentPoint(item, source);
      if (p) raw.push(p);
    }
  };
  add(payload?.oceanPoints?.points, 'payload.oceanPoints.points');
  add(payload?.oceanPoints, 'payload.oceanPoints[]');
  add(payload?.points, 'payload.points');
  add(payload?.ocean_points, 'payload.ocean_points');
  add(payload?.current_points, 'payload.current_points');
  add(payload?.currentPoints, 'payload.currentPoints');
  add(payload?.ocean?.points, 'payload.ocean.points');
  add(payload?.boatsPayload?.points, 'payload.boatsPayload.points');
  add(payload?.boatsPayload?.current_points, 'payload.boatsPayload.current_points');
  add(payload?.boatsPayload?.ocean_points, 'payload.boatsPayload.ocean_points');
  add(payload?.grid?.points, 'payload.grid.points');
  // Last-resort fallback: each live HYCOM/SST-backed boat is itself a current sample.
  // This prevents “boats render but current-zones points=0” when the server payload
  // has not yet been upgraded to carry the full point field.
  add(payload?.boats, 'payload.boats_as_current_samples');
  add(payload?.items, 'payload.items_as_current_samples');
  const seen = new Set();
  return raw.filter((p) => {
    if (!Number.isFinite(p.lat) || !Number.isFinite(p.lon) || !Number.isFinite(speedKt(p))) return false;
    const key = `${p.lat.toFixed(5)},${p.lon.toFixed(5)}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function speedKt(point) {
  return n(point.speedKt ?? point.current?.speedKt ?? point.current?.speed_kt ?? point.current_speed_kt ?? point.speed_kt, 0);
}

function isStrictSstWaterPoint(point) {
  if (!point || point.valid === false || point.water === false) return false;
  const mask = String(point.mask || point.waterMask || point.cell?.mask || '').toLowerCase();
  if (mask && !(mask.includes('sst') || mask.includes('water') || mask.includes('hycom'))) return false;
  const sst = Number(point.sst ?? point.water?.sst_c ?? point.water?.sstC ?? point.water?.tempC);
  if (point.sst === null || point.sst === false) return false;
  if (Number.isFinite(sst) && (sst < -3 || sst > 40)) return false;
  const vn = Number(point.cell?.validNeighbors ?? point.validNeighbors ?? 4);
  const edge = Number(point.edgeConfidence ?? (Number.isFinite(vn) ? vn / 4 : 1));
  return (!Number.isFinite(vn) || vn >= 4) && (!Number.isFinite(edge) || edge >= 0.99);
}

function currentPointCellDeg(points) {
  const vals = [];
  for (const p of points || []) {
    const a = Number(p?.cell?.dLat);
    const b = Number(p?.cell?.dLon);
    if (Number.isFinite(a) && a > 0) vals.push(a);
    if (Number.isFinite(b) && b > 0) vals.push(b);
  }
  vals.sort((a, b) => a - b);
  return vals.length ? vals[Math.floor(vals.length * 0.5)] : 0.12;
}

function toPath(poly, altitude = 12) {
  const coords = Array.isArray(poly?.coordinates) ? poly.coordinates : [];
  const path = [];
  for (const p of coords) {
    if (!Array.isArray(p) || p.length < 2) continue;
    const lng = Number(p[0]);
    const lat = Number(p[1]);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) continue;
    path.push({ lat, lng, altitude });
  }
  if (path.length >= 2) {
    const a = path[0];
    const b = path[path.length - 1];
    if (Math.abs(a.lat - b.lat) < 1e-6 && Math.abs(a.lng - b.lng) < 1e-6) path.pop();
  }
  return path;
}

function colorForBand(band) {
  if (band === 'extreme') return '#050505';
  if (band === 'strong') return '#ff3333';
  if (band === 'active') return '#ffe16a';
  if (band === 'safe') return '#36ff7a';
  if (band === 'fast') return '#ff3333';
  if (band === 'medium') return '#ffe16a';
  return '#36ff7a';
}
function opacityForBand(band, value) {
  const p = clamp01((Number(value) || 0) / 2.4);
  if (band === 'extreme') return 0.24 + p * 0.34;
  if (band === 'strong' || band === 'fast') return 0.20 + p * 0.32;
  if (band === 'active' || band === 'medium') return 0.14 + p * 0.26;
  return 0.09 + p * 0.20;
}
function extrudeForBand(band, value) {
  const v = Math.max(0, Number(value) || 0);
  if (band === 'extreme') return 60 + v * 26;
  if (band === 'strong' || band === 'fast') return 42 + v * 22;
  if (band === 'active' || band === 'medium') return 22 + v * 15;
  return 10 + v * 9;
}

function tag(el) {
  try { el?.setAttribute?.('data-gfs-layer', 'current-zones'); } catch (_) {}
  try { el?.setAttribute?.('data-gfs-sub-layer', 'current-marching-squares'); } catch (_) {}
  return el;
}

function pathHash(path) {
  return (Array.isArray(path) ? path : []).map((p) => `${Number(p.lat).toFixed(4)},${Number(p.lng).toFixed(4)}`).join(';');
}

function currentPolygonId(poly, path, fallbackIndex = 0) {
  const explicit = poly?.id || poly?.feature_id || poly?.zone_id || poly?.cell_id || poly?.hash;
  const band = poly?.band || 'slow';
  if (explicit) return `current:${band}:${explicit}`;
  return `current:${band}:${pathHash(path).slice(0, 180)}:${fallbackIndex % 997}`;
}

function currentPolygonHash(poly, path) {
  return [poly?.band || 'slow', Number(poly?.value ?? poly?.probability ?? 0).toFixed(3), pathHash(path)].join('|');
}

function clearCurrentZoneState(map3DElement = null) {
  for (const obj of CURRENT_ZONE_STATE.polygons.values()) {
    try { obj.el?.remove?.(); } catch (_) {}
  }
  CURRENT_ZONE_STATE.polygons.clear();
  try { (map3DElement || document)?.querySelectorAll?.('[data-gfs-sub-layer="current-marching-squares"]')?.forEach((el) => el.remove()); } catch (_) {}
}

function reconcileCurrentPolygons({ polygons, map3DElement }) {
  const incoming = new Set();
  const frag = document.createDocumentFragment();
  const stats = { payload: polygons.length, created: 0, updated: 0, unchanged: 0, removed: 0, invalid: 0 };
  polygons.forEach((poly, index) => {
    const path = toPath(poly, 12);
    if (path.length < 3) { stats.invalid += 1; return; }
    const id = currentPolygonId(poly, path, index);
    const hash = currentPolygonHash(poly, path);
    incoming.add(id);
    const existing = CURRENT_ZONE_STATE.polygons.get(id);
    if (existing && existing.hash === hash) {
      stats.unchanged += 1;
      return;
    }
    if (existing) {
      try { existing.el?.remove?.(); } catch (_) {}
      CURRENT_ZONE_STATE.polygons.delete(id);
      stats.updated += 1;
    } else {
      stats.created += 1;
    }
    const el = makeCurrentPolygon(poly);
    if (!el) { stats.invalid += 1; return; }
    try { el.setAttribute?.('data-current-id', id); } catch (_) {}
    CURRENT_ZONE_STATE.polygons.set(id, { id, hash, el });
    frag.append(el);
  });
  for (const [id, obj] of CURRENT_ZONE_STATE.polygons.entries()) {
    if (!incoming.has(id)) {
      try { obj.el?.remove?.(); } catch (_) {}
      CURRENT_ZONE_STATE.polygons.delete(id);
      stats.removed += 1;
    }
  }
  if (frag.childNodes.length) map3DElement.append(frag);
  stats.rendered = CURRENT_ZONE_STATE.polygons.size;
  CURRENT_ZONE_STATE.lastStats = stats;
  try { window.__gfsDebugEvent?.('current-zones/reconcile', stats); } catch (_) {}
  return stats;
}

function makeCurrentPolygon(poly) {
  const path = toPath(poly, 12);
  if (path.length < 3) return null;
  const band = poly.band || 'slow';
  const color = colorForBand(band);
  const value = Number(poly.value ?? poly.probability ?? 0);
  const el = createPolygon3D({
    path,
    altitude: 12,
    altitudeMode: 'relative',
    fillColor: color,
    fillOpacity: opacityForBand(band, value),
    strokeColor: color,
    strokeOpacity: Math.min(0.88, opacityForBand(band, value) + 0.22),
    strokeWidth: band === 'fast' ? 1.3 : 0.9,
    extrudedHeight: extrudeForBand(band, value),
    preferAttributePath: true,
    hover: {
      title: `Boater Current ${band} zone`,
      lines: [
        `Interpolated current: ${value.toFixed(2)} kt`,
        `Safety band: ${band}`,
        `Cells merged: ${poly.cells || 'n/a'}`,
        'Method: HYCOM current marching squares',
      ],
      metrics: { layer: 'boater-current', band, path_points: path.length, source: 'hycom_current_marching_squares' },
      payload: { speed_kt: value, band, cells: poly.cells, path_points: path.length, method: 'hycom_current_marching_squares' },
    },
  });
  try {
    el?.setAttribute?.('title', `Current ${band} contour ${value.toFixed(2)} kt`);
    el?.setAttribute?.('data-current-band', band);
  } catch (_) {}
  return tag(el);
}


function isDrawableViewportReason(reason) {
  const r = String(reason || 'steady').toLowerCase();
  return r === 'boot' || r === 'steady' || r === 'settled' || r === 'manual' || r === 'refresh' || r.includes('steady') || r.includes('settled') || r.includes('update') || r.includes('deferred');
}

export function renderCurrentZonesLayer({ payload, map3DElement, viewportReason = 'steady' }) {
  const created = [];
  const showCurrentGrid = window.GFS_ENABLE_BOATER_CURRENT_GRID === true || window.__GFS_DEBUG_CURRENT_ZONES === true;
  if (!map3DElement || !payload) return () => {};
  if (!showCurrentGrid) {
    try { clearCurrentZoneState(map3DElement); } catch (_) {}
    console.info('[gfs current-zones] visual grid disabled; boater layer uses HYCOM current data only', {
      source: payload?.source,
      boats: Array.isArray(payload?.boats) ? payload.boats.length : 0,
      points: Array.isArray(payload?.points) ? payload.points.length : 0,
      mode: 'data_only_no_grid_render',
    });
    const disposer = () => clearCurrentZoneState(map3DElement);
    disposer.__gfsKeepExisting = false;
    disposer.__gfsDidRender = false;
    return disposer;
  }
  if (!isDrawableViewportReason(viewportReason)) {
    console.info('[gfs current-zones] hold visible layer during non-draw reason', { reason: viewportReason });
    return () => {};
  }
  const directPolygons = Array.isArray(payload?.current_polygons) ? payload.current_polygons : [];
  if (directPolygons.length) {
    const queue = directPolygons.slice(0, MAX_POLYGONS_PER_FRAME);
    const stats = reconcileCurrentPolygons({ polygons: queue, map3DElement });
    console.info('[gfs current-zones] reconciled direct polygons', { ...stats, source: payload?.source, mode: payload?.mode });
    const disposer = () => clearCurrentZoneState(map3DElement);
    disposer.__gfsKeepExisting = true;
    disposer.__gfsDidRender = true;
    return disposer;
  }
  const allPoints = payloadPoints(payload);
  const points = allPoints.filter((p) => isStrictSstWaterPoint(p) && speedKt(p) >= 0.03);
  if (points.length < 4) {
    console.info('[gfs current-zones] not enough points', {
      points: points.length,
      rawPoints: allPoints.length,
      directPolygons: directPolygons.length,
      boats: Array.isArray(payload?.boats) ? payload.boats.length : 0,
      current_points: Array.isArray(payload?.current_points) ? payload.current_points.length : 0,
      payloadKeys: Object.keys(payload || {}).slice(0, 18),
    });
    return () => {};
  }
  const bbox = payload?.bbox || payload?.bbox_used || payload?.oceanPoints?.bbox || payload?.oceanPoints?.bbox_used || null;
  const contour = contourPolygonsFromPoints({
    points,
    bbox,
    valueAccessor: speedKt,
    thresholds: [
      { name: 'safe', value: 0.03, upper: 0.35 },
      { name: 'active', value: 0.35, upper: 0.90 },
      { name: 'strong', value: 0.90, upper: 1.60 },
      { name: 'extreme', value: 1.60 },
    ],
    maxGrid: 54,
    capPerBand: 24,
  });
  const queue = [
    ...(contour.bands?.safe || []),
    ...(contour.bands?.active || []),
    ...(contour.bands?.strong || []),
    ...(contour.bands?.extreme || []),
  ].slice(0, MAX_POLYGONS_PER_FRAME);
  if (!queue.length) {
    console.info('[gfs current-zones] no contours from field', { points: points.length, grid: contour.grid });
    return () => {};
  }
  const stats = reconcileCurrentPolygons({ polygons: queue, map3DElement });
  console.info('[gfs current-zones] reconciled interpolated marching-squares current zones', {
    source: payload?.oceanPoints?.source || payload?.source,
    points: points.length,
    polygons: stats.rendered,
    created: stats.created,
    updated: stats.updated,
    unchanged: stats.unchanged,
    removed: stats.removed,
    grid: contour.grid,
    safe: contour.bands?.safe?.length || 0,
    active: contour.bands?.active?.length || 0,
    strong: contour.bands?.strong?.length || 0,
    extreme: contour.bands?.extreme?.length || 0,
    sourcePointFamilies: {
      points: Array.isArray(payload?.points) ? payload.points.length : 0,
      current_points: Array.isArray(payload?.current_points) ? payload.current_points.length : 0,
      boats: Array.isArray(payload?.boats) ? payload.boats.length : 0,
    },
  });
  const disposer = () => clearCurrentZoneState(map3DElement);
  disposer.__gfsKeepExisting = true;
  disposer.__gfsDidRender = true;
  return disposer;
}
