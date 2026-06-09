import { getJsonSafe, postJsonSafe, uploadSafe } from './api.js';
import { loadLocationVideos, renderVideoFrame } from './media.js';
import { isPolygonHoverActive } from './hover_tip.js';
import { sharkIntelForLocation } from './shark-intel.js';

function clamp(value, lo = 0, hi = 100) {
  const n = Number(value);
  if (!Number.isFinite(n)) return lo;
  return Math.max(lo, Math.min(hi, n));
}

function pct(value, digits = 0) {
  const n = Number(value);
  if (!Number.isFinite(n)) return 'n/a';
  return `${n.toFixed(digits)}%`;
}

function safeFixed(value, digits = 1, suffix = '') {
  const n = Number(value);
  return Number.isFinite(n) ? `${n.toFixed(digits)}${suffix}` : 'n/a';
}

function compassText(deg) {
  const n = Number(deg);
  if (!Number.isFinite(n)) return '';
  const dirs = ['N','NNE','NE','ENE','E','ESE','SE','SSE','S','SSW','SW','WSW','W','WNW','NW','NNW'];
  return dirs[Math.floor((((n % 360) + 360) % 360 + 11.25) / 22.5) % 16];
}

function formatSwellComponent(label, swell) {
  if (!swell) return `${label} n/a`;
  const dir = Number.isFinite(Number(swell.dirDeg)) ? `${Number(swell.dirDeg).toFixed(0)}° ${swell.dirText || compassText(swell.dirDeg)}` : (swell.dirText || '');
  return `${label} ${safeFixed(swell.heightFt, 1, ' ft')} @ ${safeFixed(swell.periodS, 0, 's')}${dir ? ` from ${dir}` : ''}`;
}

function cToF(value) {
  const n = Number(value);
  return Number.isFinite(n) ? ((n * 9) / 5) + 32 : NaN;
}

function normalizeMaybeFahrenheit(value, referenceF = NaN) {
  const raw = Number(value);
  if (!Number.isFinite(raw)) return NaN;
  const ref = Number(referenceF);
  const asF = raw;
  const asConverted = cToF(raw);
  if (!Number.isFinite(ref)) {
    return (raw >= -20 && raw <= 45) ? asConverted : asF;
  }
  if (!Number.isFinite(asConverted)) return asF;
  return Math.abs(asConverted - ref) + 2 < Math.abs(asF - ref) ? asConverted : asF;
}

function averageGrid(grid, fallback = NaN) {
  if (!Array.isArray(grid) || !Array.isArray(grid[0])) return fallback;
  const arr = Array.isArray(grid[0][0]) ? grid[0] : grid;
  let sum = 0;
  let count = 0;
  for (const row of arr) {
    if (!Array.isArray(row)) continue;
    for (const value of row) {
      const n = Number(value);
      if (Number.isFinite(n)) {
        sum += n;
        count += 1;
      }
    }
  }
  return count ? (sum / count) : fallback;
}

function geoDistanceNm(lat1, lon1, lat2, lon2) {
  const a1 = Number(lat1);
  const o1 = Number(lon1);
  const a2 = Number(lat2);
  const o2 = Number(lon2);
  if (![a1, o1, a2, o2].every(Number.isFinite)) return NaN;
  const meanLat = ((a1 + a2) / 2) * Math.PI / 180;
  const dLatNm = (a2 - a1) * 60;
  const dLonNm = (o2 - o1) * 60 * Math.cos(meanLat);
  return Math.hypot(dLatNm, dLonNm);
}

function nearestPoint(points, lat, lon, maxDeg = 5) {
  if (!Array.isArray(points) || !Number.isFinite(lat) || !Number.isFinite(lon)) return null;
  let best = null;
  let bestD = Infinity;
  for (const point of points) {
    const plat = Number(point?.lat);
    const plon = Number(point?.lon ?? point?.lng);
    if (!Number.isFinite(plat) || !Number.isFinite(plon)) continue;
    const d = Math.hypot(plat - lat, plon - lon);
    if (d < bestD) {
      best = point;
      bestD = d;
    }
  }
  if (!best) return null;
  const distanceNm = geoDistanceNm(lat, lon, best.lat, best.lon);
  return { ...best, _distance_deg: bestD, _distance_nm: distanceNm };
}

function probabilityToPct(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return NaN;
  return n <= 1.25 ? n * 100 : n;
}

function mphToKt(value) { const n = Number(value); return Number.isFinite(n) ? n * 0.868976 : NaN; }
function supportFromIdeal(value, ideal, span, fallback = 50) { const n = Number(value); if (!Number.isFinite(n)) return fallback; return clamp(100 - Math.min(100, Math.abs(n - ideal) / Math.max(0.001, span) * 100)); }
function firstFinite(...values) {
  for (const value of values) {
    const n = Number(value);
    if (Number.isFinite(n)) return n;
  }
  return NaN;
}
function depthIntelFromSample(sample) {
  const d = sample?.depth_intel || sample?.depth || {};
  const oceanVars = sample?.ocean_vars || {};
  const baitDepthFt = firstFinite(
    sample?.bait_depth_ft,
    sample?.preferred_bait_depth_ft,
    d?.bait_depth_ft,
    d?.preferred_bait_depth_ft,
    oceanVars?.preferred_bait_depth_ft,
  );
  const bottomDepthFt = firstFinite(
    sample?.bottom_depth_ft,
    d?.bottom_depth_ft,
    oceanVars?.bottom_depth_ft,
    sample?.estimated_mean_depth_ft,
    d?.estimated_mean_depth_ft,
  );
  const band = Array.isArray(sample?.bait_depth_band_ft) ? sample.bait_depth_band_ft
    : (Array.isArray(d?.bait_depth_band_ft) ? d.bait_depth_band_ft : null);
  return {
    baitDepthFt,
    bottomDepthFt,
    bandFt: band,
    source: sample?.depth_source || d?.source || oceanVars?.depth_source || '',
  };
}
function oceanVarsFromSample(sample) {
  const oceanVars = sample?.ocean_vars || {};
  const sstC = firstFinite(sample?.sst_c, sample?.sst, sample?.water_temp_c, oceanVars?.sst_c);
  const sstF = firstFinite(sample?.water_temp_f, sample?.sst_f, oceanVars?.sst_f, Number.isFinite(sstC) ? ((sstC * 9 / 5) + 32) : NaN);
  const currentKt = firstFinite(sample?.current_speed_kt, sample?.speedKt, sample?.current?.speedKt, oceanVars?.current_speed_kt);
  const currentMs = firstFinite(sample?.current_speed_m_s, sample?.speedMs, sample?.current?.speedMs, oceanVars?.current_speed_m_s);
  const currentDir = firstFinite(sample?.current_dir_deg, sample?.heading, sample?.current?.dirDeg, oceanVars?.current_dir_deg);
  const salinity = firstFinite(sample?.sss, sample?.salinity, oceanVars?.sss_psu);
  return { sstC, sstF, currentKt, currentMs, currentDir, salinity };
}
function candidateOceanRows(bait, boats) {
  const rows = [];
  const push = (arr) => { if (Array.isArray(arr)) rows.push(...arr); };
  push(bait?.oceanPoints?.points);
  push(bait?.ocean_points);
  push(bait?.points);
  push(boats?.oceanPoints?.points);
  push(boats?.ocean_points);
  push(boats?.points);
  push(boats?.current_points);
  return rows.filter((x) => x && Number.isFinite(Number(x.lat)) && Number.isFinite(Number(x.lon ?? x.lng)));
}
function likelyInlandContext(sample, bait, profile) {
  const vars = oceanVarsFromSample(sample || {});
  if (Number.isFinite(vars.sstF) || Number.isFinite(vars.sstC) || sample?.water === true || sample?.mask === 'hycom_sst_ocean_mask') return false;
  if (sample?.colorado_corridor === true) return true;
  const source = String(sample?.source || bait?.source || bait?.contract || '').toLowerCase();
  if (source.includes('inland')) return true;
  const wb = String(profile?.waterbody || '').toLowerCase();
  return /lake|reservoir|river|pond|inland/.test(wb);
}
function inlandFactorSummary(sample) {
  const vars = oceanVarsFromSample(sample || {});
  const depthIntel = depthIntelFromSample(sample || {});
  const tempF = firstFinite(sample?.water_temp_f, vars.sstF);
  const currentMph = firstFinite(sample?.current_speed_mph, Number.isFinite(vars.currentMs) ? vars.currentMs * 2.23694 : NaN, Number.isFinite(vars.currentKt) ? vars.currentKt * 1.15078 : NaN);
  const depthFt = firstFinite(sample?.bait_depth_ft, depthIntel.baitDepthFt);
  const windMph = Number(sample?.speed_mph ?? (Number.isFinite(Number(sample?.speed_m_s)) ? Number(sample.speed_m_s) * 2.23694 : NaN));
  const colorado = sample?.colorado_corridor === true;
  return {
    tempPct: Number.isFinite(Number(sample?.temp_factor_pct)) ? Number(sample.temp_factor_pct) : supportFromIdeal(tempF, 67, 18, 50),
    currentPct: Number.isFinite(Number(sample?.current_factor_pct)) ? Number(sample.current_factor_pct) : supportFromIdeal(currentMph, colorado ? 1.3 : 0.8, colorado ? 1.3 : 0.9, 48),
    depthPct: Number.isFinite(Number(sample?.depth_factor_pct)) ? Number(sample.depth_factor_pct) : supportFromIdeal(depthFt, colorado ? 18 : 14, colorado ? 16 : 12, 52),
    windPct: Number.isFinite(Number(sample?.wind_factor_pct)) ? Number(sample.wind_factor_pct) : supportFromIdeal(windMph, 8, 8, 50),
    tempF, currentMph, depthFt, windMph, colorado,
  };
}


function inlandSpeciesList(profile, inlandMode) {
  if (!inlandMode) {
    return Array.isArray(profile?.species) && profile.species.length
      ? profile.species.slice(0, 4)
      : [
          { key: 'mackerel', label: 'Mackerel', temp_center_f: 63, temp_half_span_f: 12, structure_bias: 0.1, current_bias: 0.12, bait_bias: 0.28, hint_boost: 0 },
          { key: 'bass', label: 'Bass', temp_center_f: 62, temp_half_span_f: 10, structure_bias: 0.26, current_bias: 0.08, bait_bias: 0.22, hint_boost: 0 },
          { key: 'halibut', label: 'Halibut', temp_center_f: 62, temp_half_span_f: 9, structure_bias: 0.28, current_bias: 0.06, bait_bias: 0.22, hint_boost: 0 },
          { key: 'shark', label: 'Shark', temp_center_f: 64, temp_half_span_f: 14, structure_bias: 0.1, current_bias: 0.10, bait_bias: 0.24, hint_boost: 0 },
        ];
  }
  // Inland lake species model. These centers are intentionally broad because
  // the first lake temp is surface temp, not a true thermal-profile reading.
  return [
    {
      key: 'bass',
      label: 'Bass',
      temp_center_f: 70,
      temp_half_span_f: 14,
      preferred_depth_ft: 12,
      depth_span_ft: 13,
      current_ideal_mph: 0.7,
      current_span_mph: 1.0,
      wind_ideal_mph: 7,
      wind_span_mph: 8,
      bait_bias: 0.28,
      temp_bias: 0.31,
      depth_bias: 0.19,
      current_bias: 0.10,
      wind_bias: 0.06,
      structure_bias: 0.06,
    },
    {
      key: 'trout',
      label: 'Trout',
      temp_center_f: 58,
      temp_half_span_f: 14,
      preferred_depth_ft: 22,
      depth_span_ft: 18,
      current_ideal_mph: 0.9,
      current_span_mph: 1.2,
      wind_ideal_mph: 6,
      wind_span_mph: 7,
      bait_bias: 0.18,
      temp_bias: 0.38,
      depth_bias: 0.22,
      current_bias: 0.10,
      wind_bias: 0.06,
      structure_bias: 0.06,
    },
    {
      key: 'catfish',
      label: 'Catfish',
      temp_center_f: 76,
      temp_half_span_f: 16,
      preferred_depth_ft: 18,
      depth_span_ft: 20,
      current_ideal_mph: 0.35,
      current_span_mph: 0.9,
      wind_ideal_mph: 4,
      wind_span_mph: 9,
      bait_bias: 0.24,
      temp_bias: 0.28,
      depth_bias: 0.20,
      current_bias: 0.08,
      wind_bias: 0.04,
      structure_bias: 0.16,
    },
    {
      key: 'striped-bass',
      label: 'Striped bass',
      temp_center_f: 66,
      temp_half_span_f: 13,
      preferred_depth_ft: 24,
      depth_span_ft: 20,
      current_ideal_mph: 1.25,
      current_span_mph: 1.25,
      wind_ideal_mph: 8,
      wind_span_mph: 9,
      bait_bias: 0.30,
      temp_bias: 0.26,
      depth_bias: 0.18,
      current_bias: 0.16,
      wind_bias: 0.05,
      structure_bias: 0.05,
      colorado_boost: 7,
    },
  ];
}

function scoreInlandSpecies(spec, context) {
  const tempScore = supportFromIdeal(context.waterTempF, spec.temp_center_f, spec.temp_half_span_f, 48);
  const depthScore = supportFromIdeal(context.depthFt, spec.preferred_depth_ft, spec.depth_span_ft, 52);
  const currentScore = supportFromIdeal(context.currentMph, spec.current_ideal_mph, spec.current_span_mph, 50);
  const windScore = supportFromIdeal(context.windMph, spec.wind_ideal_mph, spec.wind_span_mph, 50);
  const baitScore = clamp(context.baitProb);
  const structureScore = clamp(context.structureEdge);
  const score = clamp(
    (tempScore * (spec.temp_bias || 0.3))
      + (depthScore * (spec.depth_bias || 0.18))
      + (currentScore * (spec.current_bias || 0.1))
      + (windScore * (spec.wind_bias || 0.05))
      + (baitScore * (spec.bait_bias || 0.24))
      + (structureScore * (spec.structure_bias || 0.08))
      + (context.colorado && spec.key === 'striped-bass' ? Number(spec.colorado_boost || 0) : 0)
  );
  return {
    key: spec.key,
    label: spec.label,
    score,
    factors: {
      temp: tempScore,
      depth: depthScore,
      current: currentScore,
      wind: windScore,
      bait: baitScore,
      structure: structureScore,
    },
  };
}

function rowsFromInlandTargets(targets) {
  return (Array.isArray(targets) ? targets : []).map((row) => {
    const lat = Number(row?.lat ?? row?.centroid?.lat);
    const lon = Number(row?.lon ?? row?.lng ?? row?.centroid?.lng ?? row?.centroid?.lon);
    const score5 = Number(row?.bait_score ?? row?.score_5 ?? row?.score);
    return {
      ...row,
      lat,
      lon,
      lng: lon,
      probability: Number.isFinite(score5) ? Math.max(0, Math.min(1, score5 / 5)) : Number(row?.probability),
      bait_score: Number.isFinite(score5) ? score5 : row?.bait_score,
      method: row?.method || 'inland_lake_temperature_bait_score',
      driver: row?.temperature_source || row?.source || 'inland_lake_ncss_surface_temp',
      water_temp_f: Number(row?.water_temp_f ?? row?.surface_temp_f),
      current_speed_mph: Number(row?.current_speed_mph),
      current_speed_m_s: Number(row?.current_speed_m_s),
      current_heading_deg: Number(row?.current_heading_deg),
      speed_mph: Number(row?.speed_mph),
      speed_m_s: Number(row?.speed_m_s),
      heading_deg: Number(row?.heading_deg),
      bait_depth_ft: Number(row?.bait_depth_ft),
      estimated_mean_depth_ft: Number(row?.estimated_mean_depth_ft),
      colorado_corridor: row?.colorado_corridor === true,
      temp_factor_pct: Number(row?.temp_factor_pct),
      current_factor_pct: Number(row?.current_factor_pct),
      depth_factor_pct: Number(row?.depth_factor_pct),
      wind_factor_pct: Number(row?.wind_factor_pct),
    };
  }).filter((row) => Number.isFinite(row.lat) && Number.isFinite(row.lon));
}

function candidateBaitRows(bait) {
  const inlandRows = [
    ...rowsFromInlandTargets(bait?.targets),
    ...rowsFromInlandTargets(bait?.bait?.targets),
  ];
  if (inlandRows.length) return inlandRows;
  const sets = [
    bait?.bait_score,
    bait?.bait?.bait_score,
    bait?.temperature_points,
    bait?.bait?.temperature_points,
    bait?.oceanPoints,
    bait?.ocean_points,
    bait?.points,
    bait?.oceanPoints?.points,
  ];
  for (const rows of sets) {
    if (Array.isArray(rows) && rows.length) return rows.map((row) => ({ ...row, lon: row?.lon ?? row?.lng }));
  }
  return [];
}

function sampleBaitGridAt(bait, lat, lon) {
  const rows = candidateBaitRows(bait);
  const aLat = Number(lat);
  const aLon = Number(lon);
  if (!rows.length || !Number.isFinite(aLat) || !Number.isFinite(aLon)) return null;

  const usable = rows.map((row) => ({
    ...row,
    lat: Number(row?.lat),
    lon: Number(row?.lon ?? row?.lng),
    probability: Number(row?.probability ?? row?.baitScore ?? row?.score_probability ?? row?.confidence ?? (Number.isFinite(Number(row?.bait_score)) ? Number(row.bait_score) / 5 : NaN)),
    preferred_depth_m: Number(row?.preferred_depth_m),
    depth_min_m: Number(row?.depth_min_m),
    depth_max_m: Number(row?.depth_max_m),
    water_temp_f: Number(row?.water_temp_f ?? row?.surface_temp_f),
    current_speed_mph: Number(row?.current_speed_mph),
    current_speed_m_s: Number(row?.current_speed_m_s),
    current_heading_deg: Number(row?.current_heading_deg),
    speed_mph: Number(row?.speed_mph),
    speed_m_s: Number(row?.speed_m_s),
    heading_deg: Number(row?.heading_deg),
    bait_depth_ft: Number(row?.bait_depth_ft),
    estimated_mean_depth_ft: Number(row?.estimated_mean_depth_ft),
    colorado_corridor: row?.colorado_corridor === true,
    temp_factor_pct: Number(row?.temp_factor_pct),
    current_factor_pct: Number(row?.current_factor_pct),
    depth_factor_pct: Number(row?.depth_factor_pct),
    wind_factor_pct: Number(row?.wind_factor_pct),
  })).filter((row) => Number.isFinite(row.lat) && Number.isFinite(row.lon) && Number.isFinite(row.probability));
  if (!usable.length) return null;

  const nearest = nearestPoint(usable, aLat, aLon, 8);
  const latKeys = Array.from(new Set(usable.map((r) => Number(r.lat.toFixed(6))))).sort((a, b) => a - b);
  const lonKeys = Array.from(new Set(usable.map((r) => Number(r.lon.toFixed(6))))).sort((a, b) => a - b);
  const cellMap = new Map();
  for (const row of usable) cellMap.set(`${Number(row.lat.toFixed(6))}|${Number(row.lon.toFixed(6))}`, row);

  function bounds(keys, value) {
    if (!keys.length) return null;
    let hi = keys.findIndex((k) => k >= value);
    if (hi < 0) hi = keys.length - 1;
    let lo = Math.max(0, hi - 1);
    if (keys[hi] === value) lo = hi;
    if (hi === lo && hi < keys.length - 1) hi += 1;
    return [keys[lo], keys[hi]];
  }

  const yb = bounds(latKeys, aLat);
  const xb = bounds(lonKeys, aLon);
  if (yb && xb) {
    const [y0, y1] = yb;
    const [x0, x1] = xb;
    const q11 = cellMap.get(`${y0}|${x0}`);
    const q21 = cellMap.get(`${y0}|${x1}`);
    const q12 = cellMap.get(`${y1}|${x0}`);
    const q22 = cellMap.get(`${y1}|${x1}`);
    if (q11 && q21 && q12 && q22 && x1 !== x0 && y1 !== y0) {
      const tx = Math.max(0, Math.min(1, (aLon - x0) / (x1 - x0)));
      const ty = Math.max(0, Math.min(1, (aLat - y0) / (y1 - y0)));
      const lerp = (a, b, t) => a * (1 - t) + b * t;
      const sample = (field) => {
        const vals = [q11, q21, q12, q22].map((r) => Number(r?.[field]));
        if (!vals.every(Number.isFinite)) return NaN;
        const top = lerp(vals[0], vals[1], tx);
        const bottom = lerp(vals[2], vals[3], tx);
        return lerp(top, bottom, ty);
      };
      return {
        lat: aLat,
        lon: aLon,
        probability: sample('probability'),
        preferred_depth_m: sample('preferred_depth_m'),
        depth_min_m: sample('depth_min_m'),
        depth_max_m: sample('depth_max_m'),
        water_temp_f: sample('water_temp_f'),
        current_speed_mph: sample('current_speed_mph'),
        current_speed_m_s: sample('current_speed_m_s'),
        current_heading_deg: sample('current_heading_deg'),
        speed_mph: sample('speed_mph'),
        speed_m_s: sample('speed_m_s'),
        heading_deg: sample('heading_deg'),
        bait_depth_ft: sample('bait_depth_ft'),
        estimated_mean_depth_ft: sample('estimated_mean_depth_ft'),
        temp_factor_pct: sample('temp_factor_pct'),
        current_factor_pct: sample('current_factor_pct'),
        depth_factor_pct: sample('depth_factor_pct'),
        wind_factor_pct: sample('wind_factor_pct'),
        colorado_corridor: nearest?.colorado_corridor === true,
        driver: nearest?.driver || 'bilinear_marching_grid',
        method: 'bilinear_marching_square_grid',
        source: bait?.source || bait?.bait?.source || 'bait_grid',
        nearest,
        _distance_nm: 0,
      };
    }
  }

  const weighted = usable.map((row) => ({ row, distanceNm: geoDistanceNm(aLat, aLon, row.lat, row.lon) }))
    .filter((x) => Number.isFinite(x.distanceNm))
    .sort((a, b) => a.distanceNm - b.distanceNm)
    .slice(0, 6);
  if (!weighted.length) return nearest || null;
  if (weighted[0].distanceNm <= 0.1) return { ...weighted[0].row, method: 'exact_marching_grid_cell', source: bait?.source || bait?.bait?.source || 'bait_grid', nearest: weighted[0].row, _distance_nm: weighted[0].distanceNm };
  let weightSum = 0;
  const acc = { probability: 0, preferred_depth_m: 0, depth_min_m: 0, depth_max_m: 0, water_temp_f: 0, current_speed_mph: 0, current_speed_m_s: 0, current_heading_deg: 0, speed_mph: 0, speed_m_s: 0, heading_deg: 0, bait_depth_ft: 0, estimated_mean_depth_ft: 0, temp_factor_pct: 0, current_factor_pct: 0, depth_factor_pct: 0, wind_factor_pct: 0 };
  const counts = { preferred_depth_m: 0, depth_min_m: 0, depth_max_m: 0, water_temp_f: 0, current_speed_mph: 0, current_speed_m_s: 0, current_heading_deg: 0, speed_mph: 0, speed_m_s: 0, heading_deg: 0, bait_depth_ft: 0, estimated_mean_depth_ft: 0, temp_factor_pct: 0, current_factor_pct: 0, depth_factor_pct: 0, wind_factor_pct: 0 };
  for (const item of weighted) {
    const w = 1 / Math.max(item.distanceNm, 0.15);
    weightSum += w;
    acc.probability += item.row.probability * w;
    for (const field of ['preferred_depth_m', 'depth_min_m', 'depth_max_m', 'water_temp_f', 'current_speed_mph', 'current_speed_m_s', 'current_heading_deg', 'speed_mph', 'speed_m_s', 'heading_deg', 'bait_depth_ft', 'estimated_mean_depth_ft', 'temp_factor_pct', 'current_factor_pct', 'depth_factor_pct', 'wind_factor_pct']) {
      if (Number.isFinite(Number(item.row[field]))) {
        acc[field] += Number(item.row[field]) * w;
        counts[field] += w;
      }
    }
  }
  return {
    lat: aLat,
    lon: aLon,
    probability: acc.probability / weightSum,
    preferred_depth_m: counts.preferred_depth_m ? acc.preferred_depth_m / counts.preferred_depth_m : NaN,
    depth_min_m: counts.depth_min_m ? acc.depth_min_m / counts.depth_min_m : NaN,
    depth_max_m: counts.depth_max_m ? acc.depth_max_m / counts.depth_max_m : NaN,
    water_temp_f: counts.water_temp_f ? acc.water_temp_f / counts.water_temp_f : NaN,
    current_speed_mph: counts.current_speed_mph ? acc.current_speed_mph / counts.current_speed_mph : NaN,
    current_speed_m_s: counts.current_speed_m_s ? acc.current_speed_m_s / counts.current_speed_m_s : NaN,
    current_heading_deg: counts.current_heading_deg ? acc.current_heading_deg / counts.current_heading_deg : NaN,
    speed_mph: counts.speed_mph ? acc.speed_mph / counts.speed_mph : NaN,
    speed_m_s: counts.speed_m_s ? acc.speed_m_s / counts.speed_m_s : NaN,
    heading_deg: counts.heading_deg ? acc.heading_deg / counts.heading_deg : NaN,
    bait_depth_ft: counts.bait_depth_ft ? acc.bait_depth_ft / counts.bait_depth_ft : NaN,
    estimated_mean_depth_ft: counts.estimated_mean_depth_ft ? acc.estimated_mean_depth_ft / counts.estimated_mean_depth_ft : NaN,
    temp_factor_pct: counts.temp_factor_pct ? acc.temp_factor_pct / counts.temp_factor_pct : NaN,
    current_factor_pct: counts.current_factor_pct ? acc.current_factor_pct / counts.current_factor_pct : NaN,
    depth_factor_pct: counts.depth_factor_pct ? acc.depth_factor_pct / counts.depth_factor_pct : NaN,
    wind_factor_pct: counts.wind_factor_pct ? acc.wind_factor_pct / counts.wind_factor_pct : NaN,
    colorado_corridor: weighted[0].row?.colorado_corridor === true,
    driver: weighted[0].row.driver || 'idw_marching_grid',
    method: 'idw_marching_square_grid',
    source: bait?.source || bait?.bait?.source || 'bait_grid',
    nearest: weighted[0].row,
    _distance_nm: weighted[0].distanceNm,
  };
}

function interpolateBoatScalar(boats, lat, lon, getter, maxDistanceNm = 90, maxPoints = 4) {
  if (!Array.isArray(boats) || !Number.isFinite(lat) || !Number.isFinite(lon)) return NaN;
  const samples = [];
  for (const boat of boats) {
    const plat = Number(boat?.lat);
    const plon = Number(boat?.lon);
    const value = Number(getter?.(boat));
    if (!Number.isFinite(plat) || !Number.isFinite(plon) || !Number.isFinite(value)) continue;
    const distanceNm = geoDistanceNm(lat, lon, plat, plon);
    if (!Number.isFinite(distanceNm) || distanceNm > maxDistanceNm) continue;
    samples.push({ value, distanceNm });
  }
  if (!samples.length) return NaN;
  samples.sort((a, b) => a.distanceNm - b.distanceNm);
  const top = samples.slice(0, maxPoints);
  if (top[0].distanceNm <= 0.25) return top[0].value;
  let weighted = 0;
  let weightSum = 0;
  for (const sample of top) {
    const weight = 1 / Math.max(sample.distanceNm, 0.25);
    weighted += sample.value * weight;
    weightSum += weight;
  }
  return weightSum ? (weighted / weightSum) : NaN;
}

function to2DGrid(value) {
  if (!Array.isArray(value) || !Array.isArray(value[0])) return null;
  return Array.isArray(value[0][0]) ? value[0] : value;
}

function gridValueAt(arr, yi, xi) {
  const row = arr?.[yi];
  return Number(row?.[xi]);
}

function bilinearSample(arr, bbox, lat, lon) {
  const grid = to2DGrid(arr);
  if (!grid || !Array.isArray(bbox) || bbox.length < 4) return NaN;
  const ny = grid.length;
  const nx = Array.isArray(grid[0]) ? grid[0].length : 0;
  if (!ny || !nx) return NaN;
  const west = Number(bbox[0]);
  const south = Number(bbox[1]);
  const east = Number(bbox[2]);
  const north = Number(bbox[3]);
  const spanX = (east - west) || 1;
  const spanY = (north - south) || 1;
  let x = ((Number(lon) - west) / spanX) * Math.max(0, nx - 1);
  let y = ((Number(lat) - south) / spanY) * Math.max(0, ny - 1);
  if (!Number.isFinite(x) || !Number.isFinite(y)) return NaN;
  x = Math.max(0, Math.min(nx - 1, x));
  y = Math.max(0, Math.min(ny - 1, y));
  const x0 = Math.floor(x);
  const y0 = Math.floor(y);
  const x1 = Math.min(nx - 1, x0 + 1);
  const y1 = Math.min(ny - 1, y0 + 1);
  const tx = x - x0;
  const ty = y - y0;
  const q11 = gridValueAt(grid, y0, x0);
  const q21 = gridValueAt(grid, y0, x1);
  const q12 = gridValueAt(grid, y1, x0);
  const q22 = gridValueAt(grid, y1, x1);
  if (![q11, q21, q12, q22].every(Number.isFinite)) return NaN;
  const a = q11 * (1 - tx) + q21 * tx;
  const b = q12 * (1 - tx) + q22 * tx;
  return a * (1 - ty) + b * ty;
}

function fieldSample(payload, fieldName, lat, lon) {
  return bilinearSample(payload?.fields?.[fieldName], payload?.bbox, lat, lon);
}

function scoreText(score) {
  if (score >= 80) return 'High';
  if (score >= 62) return 'Good';
  if (score >= 45) return 'Moderate';
  return 'Low';
}

function trendText(score, baitState, frontCount, boilCount) {
  if (score >= 78 && (baitState === 'stacking' || boilCount > 0)) return 'Building';
  if (frontCount > 0 || baitState === 'moving') return 'Active';
  if (score >= 55) return 'Stable';
  return 'Watch';
}

function classifyBaitState(baitScore, fronts, boils) {
  if (baitScore >= 78 && (fronts >= 1 || boils >= 1)) return 'stacking';
  if (baitScore >= 62 && fronts >= 1) return 'holding';
  if (baitScore >= 48) return 'moving';
  return 'scattered';
}

function confidenceLabel(score) {
  if (score >= 80) return 'High';
  if (score >= 58) return 'Medium';
  return 'Low';
}

function safetyLabel(score) {
  if (score >= 78) return 'Good';
  if (score >= 55) return 'Moderate';
  return 'Rough';
}

function speciesTempWindow(tempF, center, halfSpan) {
  const t = Number(tempF);
  if (!Number.isFinite(t)) return 0.5;
  const diff = Math.abs(t - center);
  return clamp((1 - (diff / halfSpan)) * 100, 0, 100) / 100;
}

function summarizeReports(reports) {
  const joined = Array.isArray(reports) ? reports.join(' • ').toLowerCase() : '';
  return {
    joined,
    sardine: /sardine/.test(joined),
    anchovy: /anchov/.test(joined),
    jig: /(jig|iron)/.test(joined),
    flyline: /flyline/.test(joined),
    fresh: /(trout|catfish|largemouth|smallmouth|striper|stocked|powerbait)/.test(joined),
    salt: /(tuna|yellowtail|dorado|mackerel|mack|barracuda|halibut|corbina|shark|calico|kelp bass)/.test(joined),
  };
}

function recommendTackle(speciesRows, reportHints, baitState, profile) {
  if (Array.isArray(profile?.tackle_hints) && profile.tackle_hints.length) {
    const lead = profile.tackle_hints[0];
    if (baitState === 'stacking' && Array.isArray(profile.tackle_hints) && profile.tackle_hints[1]) return `${lead} • ${profile.tackle_hints[1]}`;
    return lead;
  }
  const ordered = Array.isArray(speciesRows) ? [...speciesRows].sort((a, b) => b.score - a.score) : [];
  const top = ordered[0]?.key || 'coastal_general';
  if (top === 'tuna') return reportHints.sardine ? 'Flylined sardine / sinker rig / knife jig' : 'Flylined bait / sinker rig / knife jig';
  if (top === 'yellowtail') return reportHints.jig ? 'Surface iron / yo-yo jig / flylined bait' : 'Flylined sardine / surface iron / yo-yo jig';
  if (top === 'dorado') return 'Small paddletail / troll feather / live bait near moving water';
  if (top === 'trout') return 'PowerBait / mini jig / slip-float at first light';
  if (top === 'catfish') return 'Cut bait / stink bait / night soak near channel edge';
  if (top === 'largemouth_bass' || top === 'bass') return 'Swimbait / finesse worm / live bait near structure';
  if (baitState === 'stacking') return 'Sabiki / small live bait / finesse presentation';
  return reportHints.anchovy ? 'Anchovy / small jig / light leader' : 'Small live bait / anchovy / sabiki at active periods';
}

function moonBiasText() {
  const day = Math.floor(Date.now() / 86400000) % 29;
  if (day < 7) return 'New-to-first-quarter push';
  if (day < 15) return 'Waxing moon energy';
  if (day < 22) return 'Full-moon recovery window';
  return 'Waning moon reset';
}

function updateMeter(node, value) {
  if (!node) return;
  node.style.width = `${clamp(value)}%`;
}

function listToHtml(items) {
  if (!Array.isArray(items) || !items.length) return '<li>Signal build pending</li>';
  return items.map((item) => `<li>${item}</li>`).join('');
}


function formatMarkerEnvironment(markerEnv, fallbackIntel) {
  const env = markerEnv || null;
  const display = env?.display || {};
  const wx = env?.weather || {};
  const water = env?.water || {};
  const astro = env?.astro || {};
  const ocean = env?.ocean || {};
  const swellComponents = Array.isArray(env?.swell_components) ? env.swell_components : (Array.isArray(ocean?.swells) ? ocean.swells : (Array.isArray(ocean?.waves?.components) ? ocean.waves.components : []));
  const sourceTier = env?.source_tier || env?.environment_meta?.source_tier || '';
  const sourceList = Array.isArray(env?.sources_used) ? env.sources_used.join(', ') : '';

  const nowParts = [];
  if (display.now) nowParts.push(display.now);
  if (!display.now && Number.isFinite(Number(wx.air_temp_f))) nowParts.push(`Air ${Number(wx.air_temp_f).toFixed(1)}°F`);
  if (!display.now && Number.isFinite(Number(water.sst_f))) nowParts.push(`Water ${Number(water.sst_f).toFixed(1)}°F`);
  if (!display.now && Number.isFinite(Number(wx.wind_speed_kt))) {
    nowParts.push(`Wind ${Number(wx.wind_speed_kt).toFixed(1)} kt${Number.isFinite(Number(wx.wind_direction_deg)) ? ` @ ${Number(wx.wind_direction_deg).toFixed(0)}°` : ''}`);
  }
  if (display.short_forecast) nowParts.push(display.short_forecast);

  const waterParts = [];
  if (display.water) waterParts.push(display.water);
  if (!display.water && Number.isFinite(Number(water.current_speed_kt))) {
    waterParts.push(`Current ${Number(water.current_speed_kt).toFixed(2)} kt${Number.isFinite(Number(water.current_direction_deg)) ? ` @ ${Number(water.current_direction_deg).toFixed(0)}°` : ''}`);
  }
  if (!display.water && Number.isFinite(Number(water.tide_height_ft))) waterParts.push(`Tide ${Number(water.tide_height_ft).toFixed(2)} ft`);
  if (Number.isFinite(Number(wx.cloud_cover_pct))) waterParts.push(`Cloud ${Number(wx.cloud_cover_pct).toFixed(0)}%`);
  if (Number.isFinite(Number(wx.precip_probability_pct))) waterParts.push(`Precip ${Number(wx.precip_probability_pct).toFixed(0)}%`);
  else if (Number.isFinite(Number(wx.precipitation_factor))) waterParts.push(`Precip factor ${Number(wx.precipitation_factor).toFixed(3)}`);
  if (Number.isFinite(Number(wx.pressure_mb))) waterParts.push(`Pressure ${Number(wx.pressure_mb).toFixed(1)} mb`);
  if (swellComponents[0]) waterParts.push(formatSwellComponent('Primary swell', swellComponents[0]));
  if (swellComponents[1]) waterParts.push(formatSwellComponent('Secondary', swellComponents[1]));
  if (swellComponents[2]) waterParts.push(formatSwellComponent('Third', swellComponents[2]));

  const sourceParts = [];
  if (display.source) sourceParts.push(display.source);
  else if (sourceTier) sourceParts.push(`Source tier ${sourceTier}`);
  if (sourceList) sourceParts.push(`Sources ${sourceList}`);
  if (env?.valid_time) sourceParts.push(`valid ${env.valid_time}`);
  if (display.bait_window) sourceParts.push(`Bait window ${display.bait_window}`);
  if (Number.isFinite(Number(astro.moon_illumination))) sourceParts.push(`Moon ${Math.round(Number(astro.moon_illumination) * 100)}% ${astro.moon_label || ''}`.trim());

  return {
    now: nowParts.filter(Boolean).join(' • ') || `Water ${safeFixed(Math.round(Number(fallbackIntel?.waterTempF) * 10) / 10, 1, '°F')} • Air ${safeFixed(Math.round(Number(fallbackIntel?.airTempF) * 10) / 10, 1, '°F')} • Current ${safeFixed(fallbackIntel?.currentKt, 1, ' kt')} @ ${safeFixed(fallbackIntel?.currentDir, 0, '°')}`,
    more: waterParts.filter(Boolean).join(' • ') || `Wind ${safeFixed(fallbackIntel?.windKt, 1, ' kt')} @ ${safeFixed(fallbackIntel?.windDir, 0, '°')} • Swell ${safeFixed(fallbackIntel?.swellFt, 1, ' ft')} • Cloud ${safeFixed(fallbackIntel?.cloudPct, 0, '%')} • Rain ${safeFixed(fallbackIntel?.rainRate, 3, '')}`,
    source: sourceParts.filter(Boolean).join(' • '),
  };
}

function deriveIntel({ loc, wx, bait, clouds, boats, localOverlay, reports, videos, profile, markerEnvironment }) {
  const lat = Number(loc?.lat);
  const lon = Number(loc?.lon);
  const baitGridSample = sampleBaitGridAt(bait, lat, lon);
  const nearestBait = baitGridSample?.nearest || nearestPoint(candidateBaitRows(bait), lat, lon, 8);
  const nearestOceanPoint = nearestPoint(candidateOceanRows(bait, boats), lat, lon, 8);
  const baitSample = baitGridSample || nearestBait || nearestOceanPoint || {};
  const baitDepthIntel = depthIntelFromSample(baitSample);
  const baitOceanVars = oceanVarsFromSample(baitSample);
  const baitProbRaw = probabilityToPct(baitGridSample?.probability ?? nearestBait?.probability ?? bait?.confidence?.overall ?? 0) || 0;
  const inlandSample = baitGridSample || nearestBait || null;
  const inlandMode = likelyInlandContext(inlandSample, bait, profile);
  const inlandFactors = inlandFactorSummary(inlandSample || {});
  const baitProbBase = clamp(Math.max(baitProbRaw, Number.isFinite(Number(bait?.confidence?.overall)) ? Number(bait.confidence.overall) * 100 : 20));
  const inlandBlended = inlandMode
    ? clamp((baitProbBase * 0.56) + (inlandFactors.tempPct * 0.18) + (inlandFactors.currentPct * 0.10) + (inlandFactors.depthPct * 0.10) + (inlandFactors.windPct * 0.06) + (inlandFactors.colorado ? 4 : 0))
    : baitProbBase;
  const baitProb = inlandMode ? Math.max(baitProbBase, inlandBlended) : baitProbBase;
  const frontCount = Array.isArray(bait?.front_lines) ? bait.front_lines.length : 0;
  const boilCount = Array.isArray(bait?.boil_probability_polygons) ? bait.boil_probability_polygons.length : 0;
  const convCount = Array.isArray(bait?.convergence_polygons) ? bait.convergence_polygons.length : 0;
  const baitState = classifyBaitState(baitProb, frontCount, boilCount);

  const markerOcean = markerEnvironment?.ocean || {};
  const markerBoat = markerOcean?.boat || markerEnvironment?.boating || null;
  const boat = nearestPoint(boats?.boats, lat, lon, 8) || markerBoat;
  const sampledCloudPct = fieldSample(wx, 'cloud_total', lat, lon);
  const sampledRainRate = fieldSample(wx, 'precip_rate', lat, lon);
  const sampledWindU = fieldSample(wx, 'wind_u', lat, lon);
  const sampledWindV = fieldSample(wx, 'wind_v', lat, lon);
  const sampledTempK = fieldSample(wx, 'air_temp', lat, lon);
  const cloudPct = Number.isFinite(Number(localOverlay?.cloudCover)) ? Number(localOverlay.cloudCover) : (Number.isFinite(sampledCloudPct) ? sampledCloudPct : averageGrid(wx?.fields?.cloud_total, averageGrid(clouds?.cloud_layers?.find((l) => l?.name === 'low')?.density, NaN)));
  const rainRate = Number.isFinite(Number(localOverlay?.rainRate)) ? Number(localOverlay.rainRate) : (Number.isFinite(sampledRainRate) ? sampledRainRate : averageGrid(wx?.fields?.precip_rate, NaN));
  const sampledAirTempF = Number.isFinite(sampledTempK) ? (((sampledTempK - 273.15) * 9) / 5) + 32 : NaN;
  const sampledWindKt = Number.isFinite(sampledWindU) && Number.isFinite(sampledWindV) ? Math.hypot(sampledWindU, sampledWindV) * 1.94384 : NaN;
  const sampledWindDir = Number.isFinite(sampledWindU) && Number.isFinite(sampledWindV) ? ((Math.atan2(sampledWindU, sampledWindV) * 180 / Math.PI) + 360) % 360 : NaN;
  const sampledOceanVars = oceanVarsFromSample(nearestOceanPoint || {});
  const sampledWaterTempF = firstFinite(
    sampledOceanVars.sstF,
    interpolateBoatScalar(boats?.boats, lat, lon, (entry) => entry?.water?.sst_f ?? entry?.water?.tempF ?? entry?.sst_f, 90, 4)
  );
  const sampledBoatAirTempRaw = interpolateBoatScalar(boats?.boats, lat, lon, (entry) => entry?.water?.airTempF, 90, 4);
  const sampledBoatAirTempF = normalizeMaybeFahrenheit(sampledBoatAirTempRaw, sampledAirTempF);
  const boatAirTempRaw = Number(boat?.water?.airTempF);
  const boatAirTempF = normalizeMaybeFahrenheit(boatAirTempRaw, sampledAirTempF);
  const markerWater = markerEnvironment?.water || {};
  const markerWeather = markerEnvironment?.weather || {};
  const markerSwells = Array.isArray(markerEnvironment?.swell_components) ? markerEnvironment.swell_components : (Array.isArray(markerOcean?.swells) ? markerOcean.swells : (Array.isArray(markerOcean?.waves?.components) ? markerOcean.waves.components : []));
  const waterTempF = inlandMode && Number.isFinite(inlandFactors.tempF)
    ? inlandFactors.tempF
    : (Number.isFinite(sampledWaterTempF) ? sampledWaterTempF : (Number.isFinite(Number(boat?.water?.sst_f ?? boat?.water?.tempF)) ? Number(boat?.water?.sst_f ?? boat.water.tempF) : Number(markerWater.sst_f || markerOcean.sst_f)));
  const airTempF = Number.isFinite(sampledBoatAirTempF) ? sampledBoatAirTempF : (Number.isFinite(boatAirTempF) ? boatAirTempF : (Number.isFinite(Number(markerWeather.air_temp_f)) ? Number(markerWeather.air_temp_f) : sampledAirTempF));
  const currentKt = inlandMode && Number.isFinite(inlandFactors.currentMph)
    ? mphToKt(inlandFactors.currentMph)
    : (Number.isFinite(Number(boat?.current?.speedKt)) ? Number(boat.current.speedKt) : Number(markerWater.current_speed_kt || markerOcean?.current?.speedKt));
  const currentDir = inlandMode && Number.isFinite(Number(inlandSample?.current_heading_deg))
    ? Number(inlandSample.current_heading_deg)
    : (Number.isFinite(Number(boat?.current?.dirDeg)) ? Number(boat.current.dirDeg) : Number(markerWater.current_direction_deg || markerOcean?.current?.dirDeg));
  const windKt = inlandMode && Number.isFinite(inlandFactors.windMph)
    ? mphToKt(inlandFactors.windMph)
    : (Number.isFinite(Number(boat?.wind?.speedKt)) ? Number(boat?.wind?.speedKt) : (Number.isFinite(Number(markerWeather.wind_speed_kt)) ? Number(markerWeather.wind_speed_kt) : sampledWindKt));
  const windDir = inlandMode && Number.isFinite(Number(inlandSample?.heading_deg))
    ? Number(inlandSample.heading_deg)
    : (Number.isFinite(Number(boat?.wind?.dirDeg)) ? Number(boat?.wind?.dirDeg) : (Number.isFinite(Number(markerWeather.wind_direction_deg)) ? Number(markerWeather.wind_direction_deg) : sampledWindDir));
  const swellFt = Number.isFinite(Number(boat?.waves?.sigHeightFt)) ? Number(boat.waves.sigHeightFt) : Number(markerOcean?.waves?.sigHeightFt || markerSwells?.[0]?.heightFt);
  const swell1 = boat?.waves?.primary || markerSwells?.[0] || null;
  const swell2 = boat?.waves?.secondary || markerSwells?.[1] || null;
  const swell3 = boat?.waves?.tertiary || markerSwells?.[2] || null;
  const reportHints = summarizeReports(reports);

  const structureEdge = clamp(38 + (frontCount * 11) + (convCount * 6));
  const weatherPenalty = clamp((Number.isFinite(windKt) ? windKt * 1.7 : 16) + (Number.isFinite(swellFt) ? swellFt * 8.5 : 18) + (Number.isFinite(rainRate) ? rainRate * 180 : 0), 0, 100);
  const safetyScore = clamp(100 - weatherPenalty + (Number.isFinite(cloudPct) && cloudPct < 80 ? 4 : 0));
  const confidenceScore = clamp(
    34
      + (Number.isFinite(baitProb) ? 28 : 0)
      + (boat ? 18 : 0)
      + (frontCount > 0 ? 8 : 0)
      + (videos?.length ? 6 : 0)
      + (reports?.length ? 6 : 0)
      + (profile?.species?.length ? 4 : 0)
  );

  const activeSpecies = inlandSpeciesList(profile, inlandMode);

  const speciesRows = inlandMode
    ? activeSpecies.map((spec) => scoreInlandSpecies(spec, {
        waterTempF,
        depthFt: inlandFactors.depthFt,
        currentMph: inlandFactors.currentMph,
        windMph: inlandFactors.windMph,
        baitProb,
        structureEdge,
        colorado: inlandFactors.colorado,
      }))
    : activeSpecies.map((spec) => {
        const tempWindow = speciesTempWindow(waterTempF, spec.temp_center_f || 64, spec.temp_half_span_f || 12) * 28;
        const currentSupport = Math.min(Number.isFinite(currentKt) ? currentKt : 0, 3) * (spec.current_bias || 0.1) * 18;
        const structureSupport = structureEdge * (spec.structure_bias || 0.15);
        const baitSupport = baitProb * (spec.bait_bias || 0.22);
        const score = clamp(tempWindow + currentSupport + structureSupport + baitSupport + Number(spec.hint_boost || 0));
        return { key: spec.key, label: spec.label, score };
      });

  const predatorScore = clamp((Math.max(...speciesRows.map((row) => row.score), 0) * 0.62) + (baitProb * 0.24) + (structureEdge * 0.14));
  const opportunityScore = clamp((baitProb * 0.33) + (predatorScore * 0.34) + (safetyScore * 0.18) + (confidenceScore * 0.15));
  const trend = trendText(opportunityScore, baitState, frontCount, boilCount);

  const reasons = [];
  if (profile?.waterbody) reasons.push(`${profile.waterbody} rules are active for this marker instead of a one-size-fits-all ocean list`);
  if (baitProb >= 68) reasons.push('Bait probability is elevated around this orb');
  if (inlandMode) reasons.push(`Inland bait score is now blended from temperature ${Math.round(inlandFactors.tempPct)}%, current ${Math.round(inlandFactors.currentPct)}%, depth ${Math.round(inlandFactors.depthPct)}%, and wind ${Math.round(inlandFactors.windPct)}%`);
  if (inlandMode) reasons.push(`Inland species model active: bass / trout / catfish / striped bass from lake temp, depth, current, wind, and bait`);
  if (frontCount > 0) reasons.push(`Thermal edge activity showing ${frontCount} front line${frontCount === 1 ? '' : 's'} in the local window`);
  if (convCount > 0) reasons.push(`Current convergence pockets detected (${convCount})`);
  if (Number.isFinite(currentKt) && currentKt >= 1.0) reasons.push(`Current is moving with intent at ${currentKt.toFixed(1)} kt`);
  if (inlandMode && Number.isFinite(inlandFactors.depthFt)) reasons.push(`Preferred inland bait depth is reading near ${inlandFactors.depthFt.toFixed(1)} ft`);
  if (inlandMode && inlandFactors.colorado) reasons.push('Lower Colorado corridor current weighting is active for this location');
  if (Number.isFinite(swellFt) && swellFt <= 3.0) reasons.push('Sea state is still fishable for a small-to-mid boat');
  if (reports?.length) reasons.push('Historic location reports reinforce this node');
  if (Number.isFinite(boat?._distance_nm)) reasons.push(`Boat / sea-state solve is wired ${boat._distance_nm.toFixed(1)} nm from the orb anchor`);
  if (baitGridSample?.method) reasons.push(`Bait is sampled at the orb from the marching-square grid (${String(baitGridSample.method).replace(/_/g, ' ')})`);
  else if (Number.isFinite(nearestBait?._distance_nm)) reasons.push(`Bait solve is wired ${nearestBait._distance_nm.toFixed(1)} nm from the orb anchor`);
  if (!reasons.length) reasons.push('Data induction is online but this node is still waiting on a stronger stack');

  const risks = [];
  if (Number.isFinite(windKt) && windKt >= 18) risks.push('Wind is starting to tax clean presentations');
  if (inlandMode && Number.isFinite(inlandFactors.depthFt) && inlandFactors.depthFt > 28) risks.push('Bait depth is slipping deeper than the preferred shallow window');
  if (Number.isFinite(swellFt) && swellFt >= 4) risks.push('Wave energy is pushing boating conditions into caution');
  if (Number.isFinite(rainRate) && rainRate > 0.04) risks.push('Active precip may muddy the read');
  if (cloudPct >= 85) risks.push('Heavy cloud deck may flatten the visual read on the water');
  if (!risks.length) risks.push('No major short-fuse risk flag from the local stack');

  return {
    opportunityScore,
    baitScore: baitProb,
    baitGridSample,
    baitDepthIntel,
    baitOceanVars,
    predatorScore,
    safetyScore,
    confidenceScore,
    trend,
    cloudPct,
    rainRate,
    frontCount,
    convCount,
    boilCount,
    baitState,
    inlandMode,
    inlandFactors,
    currentKt,
    currentDir,
    windKt,
    windDir,
    swellFt,
    swell1,
    swell2,
    swell3,
    waterTempF,
    airTempF,
    speciesRows,
    reasons,
    risks,
    tackle: recommendTackle(speciesRows, reportHints, baitState, profile),
    moonBias: moonBiasText(),
    boat,
    positioning: {
      markerLat: lat,
      markerLon: lon,
      boatLat: Number(boat?.lat),
      boatLon: Number(boat?.lon),
      boatDistanceNm: Number(boat?._distance_nm),
      baitLat: Number(baitGridSample?.lat ?? nearestBait?.lat),
      baitLon: Number(baitGridSample?.lon ?? nearestBait?.lon),
      baitDistanceNm: Number(baitGridSample?._distance_nm ?? nearestBait?._distance_nm),
      matchedZone: profile?.matched_zone || null,
      classificationMethod: profile?.classification_method || null,
      coastDistanceNm: Number(profile?.coast_distance_deg) * 60,
    },
  };
}

export function createHud({ root, onStartLive, onStopLive, onSelectLocation, getOverlaySummary }) {
  const el = {
    panel: root,
    close: document.getElementById('hudClose'),
    title: document.getElementById('hudLocation'),
    coords: document.getElementById('hudCoords'),
    waterbody: document.getElementById('hudWaterbody'),
    positioning: document.getElementById('hudPositioning'),
    statusLine: document.getElementById('hudStatusLine'),
    opportunityScore: document.getElementById('hudOpportunityScore'),
    opportunityFill: document.getElementById('hudOpportunityFill'),
    confidence: document.getElementById('hudConfidence'),
    trend: document.getElementById('hudTrend'),
    safetyLine: document.getElementById('hudSafetyLine'),
    baitSummary: document.getElementById('hudBaitSummary'),
    baitFill: document.getElementById('hudBaitFill'),
    baitDrivers: document.getElementById('hudBaitDrivers'),
    baitMovement: document.getElementById('hudBaitMovement'),
    sharkSummary: document.getElementById('hudSharkSummary'),
    sharkFill: document.getElementById('hudSharkFill'),
    sharkDrivers: document.getElementById('hudSharkDrivers'),
    sharkDepth: document.getElementById('hudSharkDepth'),
    predatorLabel1: document.getElementById('hudPredatorLabel1'),
    predatorLabel2: document.getElementById('hudPredatorLabel2'),
    predatorLabel3: document.getElementById('hudPredatorLabel3'),
    predatorLabel4: document.getElementById('hudPredatorLabel4'),
    predatorScore1: document.getElementById('hudPredatorScore1'),
    predatorScore2: document.getElementById('hudPredatorScore2'),
    predatorScore3: document.getElementById('hudPredatorScore3'),
    predatorScore4: document.getElementById('hudPredatorScore4'),
    predatorFill1: document.getElementById('hudPredatorFill1'),
    predatorFill2: document.getElementById('hudPredatorFill2'),
    predatorFill3: document.getElementById('hudPredatorFill3'),
    predatorFill4: document.getElementById('hudPredatorFill4'),
    envNow: document.getElementById('hudEnvNow'),
    envMore: document.getElementById('hudEnvMore'),
    envPosition: document.getElementById('hudEnvPosition'),
    windowText: document.getElementById('hudWindowText'),
    moonBias: document.getElementById('hudMoonBias'),
    reasons: document.getElementById('hudReasons'),
    risks: document.getElementById('hudRisks'),
    videoFrame: document.getElementById('hudVideoFrame'),
    lastReport: document.getElementById('hudLastReport'),
    reports: document.getElementById('hudReports'),
    reportInput: document.getElementById('hudReportInput'),
    saveReport: document.getElementById('hudSaveReport'),
    uploadFile: document.getElementById('hudUploadFile'),
    uploadVideo: document.getElementById('hudUploadVideo'),
    goLive: document.getElementById('hudGoLive'),
    stopLive: document.getElementById('hudStopLive'),
    tackle: document.getElementById('hudTackle'),
    hoverWeather: document.getElementById('hudHoverWeather'),
  };

  let selected = null;
  let selectedRefreshTimer = null;
  const selectedRefreshMs = 45000;

  function stopSelectedAutoRefresh() {
    if (selectedRefreshTimer) {
      clearInterval(selectedRefreshTimer);
      selectedRefreshTimer = null;
    }
  }

  function startSelectedAutoRefresh() {
    stopSelectedAutoRefresh();
    selectedRefreshTimer = setInterval(() => {
      if (!selected || document.hidden) return;
      refresh().catch((err) => console.info('[gfs hud] selected intel auto-refresh skipped', err?.message || String(err)));
    }, selectedRefreshMs);
  }

  function selectedApiId() {
    return selected?.location_key || selected?.location_id || selected?.id || selected?.csv_id || '';
  }

  el.close.onclick = () => {
    el.panel.classList.add('closed');
    el.panel.setAttribute('aria-hidden', 'true');
  };

  async function refresh() {
    if (!selected) return;
    const selectedLat = Number(selected.lat);
    const selectedLon = Number(selected.lon);
    el.title.textContent = selected.name || 'Fish intelligence';
    el.coords.textContent = `${Number.isFinite(selectedLat) ? selectedLat.toFixed(4) : 'n/a'}, ${Number.isFinite(selectedLon) ? selectedLon.toFixed(4) : 'n/a'} • orb anchor`;
    el.statusLine.textContent = 'Opening intel pane • pulling all available signals now';

    const id = selectedApiId();
    const baseLat = Number.isFinite(selectedLat) ? selectedLat : 0;
    const baseLon = Number.isFinite(selectedLon) ? selectedLon : 0;
    const frameBox = `${(baseLon - 1.8).toFixed(4)},${(baseLat - 1.8).toFixed(4)},${(baseLon + 1.8).toFixed(4)},${(baseLat + 1.8).toFixed(4)}`;

    const settled = await Promise.allSettled([
      getJsonSafe(`/gfs/api/location/${encodeURIComponent(id)}`, null, { timeoutMs: 1800, abortPrevious: false }),
      getJsonSafe(`/gfs/api/location/${encodeURIComponent(id)}/live-intel`, null, { timeoutMs: 2200, abortPrevious: false }),
      getJsonSafe(`/gfs/api/intelligence/node/${encodeURIComponent(id)}`, null, { timeoutMs: 2200, abortPrevious: false }),
      getJsonSafe(`/gfs/api/location/${encodeURIComponent(id)}/environment`, null, { timeoutMs: 2200, abortPrevious: false }),
      getJsonSafe(`/gfs/api/scene-cache?bbox=${frameBox}&layers=clouds,rain,bait,boater,shark-intel&mode=fast&fast=1&refresh=0&reason=hud_scene_cache_read`, null, { timeoutMs: 2800, abortPrevious: false }),
      // Prefer already-loaded bait layer. Avoid waking advanced bait on every HUD
      // selection; direct fetch only happens below if scene/frame lacks bait.
      Promise.resolve(window.__gfsDataInduction?.latest?.baitAdvanced || window.__gfsDataInduction?.latest?.frame?.baitAdvanced || null),
      getJsonSafe(`/gfs/api/scene-cache?bbox=${frameBox}&layers=inland_water_temp&mode=fast&fast=1&refresh=0&reason=hud_inland_temp_cache_read`, null, { timeoutMs: 2800, abortPrevious: false }),
      loadLocationVideos(id),
    ]);
    const valueAt = (idx, fallback = null) => settled[idx]?.status === 'fulfilled' ? (settled[idx].value ?? fallback) : fallback;
    const loc = valueAt(0, null) || selected;
    if (!loc) {
      el.statusLine.textContent = 'Location intelligence unavailable';
      return;
    }

    el.title.textContent = loc.name || selected.name;
    el.coords.textContent = `${Number(loc.lat || selected.lat).toFixed(4)}, ${Number(loc.lon || selected.lon).toFixed(4)} • orb anchor`;

    let liveIntel = valueAt(1, null);
    let node = valueAt(2, null);
    let markerEnvPayload = valueAt(3, null);
    let frame = valueAt(4, null);
    if (frame?.layers) {
      frame = {
        ...frame,
        clouds: frame.layers.clouds || null,
        rain: frame.layers.rain || null,
        baitAdvanced: frame.layers.bait || null,
        boats: frame.layers.boater || frame.layers.boats || null,
        sharkIntel: frame.layers['shark-intel'] || frame.layers.shark_intel || frame.layers.sharkIntel || null,
        inlandWaterTemp: frame.layers.inland_water_temp || frame.layers.inlandTemp || null,
        weather: { fields: frame.layers.clouds?.fields || {}, precip_columns: frame.layers.rain?.precip_columns || frame.layers.clouds?.precip_columns || [] },
      };
    }
    let baitGridPayload = valueAt(5, null);
    const inlandTempScene = valueAt(6, null);
    let inlandBaitPayload = inlandTempScene?.layers?.inland_water_temp || inlandTempScene?.layers?.inlandTemp || inlandTempScene || frame?.inlandWaterTemp || null;
    let vids = valueAt(7, []);
    const initialBaitRows = Array.isArray(baitGridPayload?.bait_score) ? baitGridPayload.bait_score.length : 0;
    const initialFrameBaitRows = Array.isArray(frame?.baitAdvanced?.bait_score) ? frame.baitAdvanced.bait_score.length : 0;
    if (!initialBaitRows && !initialFrameBaitRows && !inlandBaitPayload?.bait_score?.length) {
      try {
        const baitSceneCache = await getJsonSafe(`/gfs/api/scene-cache?bbox=${frameBox}&layers=bait&mode=fast&fast=1&refresh=0&reason=hud_missing_bait_cache_read`, null, { timeoutMs: 2200, abortPrevious: false });
        baitGridPayload = baitSceneCache?.layers?.bait || null;
        getJsonSafe(`/gfs/api/cache/refresh?bbox=${frameBox}&layers=bait&reason=hud_missing_bait_background_refresh`, null, { timeoutMs: 2000, abortPrevious: false }).catch(() => {});
      } catch (_) {}
    }
    if (liveIntel?.environment && !markerEnvPayload) markerEnvPayload = liveIntel.environment;
    if (liveIntel?.node && !node) node = liveIntel.node;

    let wx = frame?.weather || null;
    let bait = null;
    const inlandRows = Array.isArray(inlandBaitPayload?.bait_score) ? inlandBaitPayload.bait_score.length : 0;
    const inlandTargets = Array.isArray(inlandBaitPayload?.targets) ? inlandBaitPayload.targets.length : 0;
    const baitGridRows = Array.isArray(baitGridPayload?.bait_score) ? baitGridPayload.bait_score.length : 0;
    const frameBaitRows = Array.isArray(frame?.baitAdvanced?.bait_score) ? frame.baitAdvanced.bait_score.length : 0;
    if (inlandRows > 0 || inlandTargets > 0) bait = inlandBaitPayload;
    else if (baitGridRows > 0 || (baitGridPayload?.bait?.status === 'ready')) bait = baitGridPayload;
    else bait = frame?.baitAdvanced || baitGridPayload || null;
    let clouds = frame?.clouds || null;
    let boats = frame?.boats || null;
    let sharkIntelPayload = frame?.sharkIntel || null;
    if (!sharkIntelPayload?.score_points?.length && !sharkIntelPayload?.contours?.length) {
      try {
        const sharkScene = await getJsonSafe(`/gfs/api/scene-cache?bbox=${frameBox}&visible_bbox=${frameBox}&layers=shark-intel&mode=fast&fast=1&refresh=0&reason=hud_shark_intel_cache_read`, null, { timeoutMs: 2600, abortPrevious: false });
        sharkIntelPayload = sharkScene?.layers?.['shark-intel'] || sharkScene?.layers?.shark_intel || sharkScene?.layers?.sharkIntel || sharkScene || sharkIntelPayload;
        getJsonSafe(`/gfs/api/cache/refresh?bbox=${frameBox}&visible_bbox=${frameBox}&layers=shark-intel&reason=hud_shark_intel_background_refresh`, null, { timeoutMs: 1800, abortPrevious: false }).catch(() => {});
      } catch (_) {}
    }
    const sharkIntel = sharkIntelForLocation(sharkIntelPayload, loc);

    const reports = [...(loc.reports || loc.all_reports || loc.csv_reports || selected.all_reports || selected.reports || [])];
    const localOverlay = typeof getOverlaySummary === 'function' ? getOverlaySummary(loc) : null;
    const profile = node?.profile || null;
    const markerEnvironment = markerEnvPayload?.marker_environment || markerEnvPayload?.weather_environment || node?.marker_environment || node?.weather_environment || node?.weather?.marker_environment || loc?.marker_environment || loc?.weather_environment || loc?.weather?.marker_environment || null;
    const intel = deriveIntel({ loc, wx, bait, clouds, boats, localOverlay, reports, videos: vids, profile, markerEnvironment });
    const envDisplay = formatMarkerEnvironment(markerEnvironment, intel);

    el.waterbody.textContent = profile?.waterbody ? `${profile.waterbody} • ${profile.headline_species}` : 'Habitat lens pending';
    el.positioning.textContent = [
      profile?.matched_zone ? `Zone ${profile.matched_zone}` : null,
      profile?.classification_method ? `Classifier ${profile.classification_method}` : null,
      Number.isFinite(Number(profile?.coast_distance_deg)) ? `Coast ${ (Number(profile.coast_distance_deg) * 60).toFixed(1) } nm` : null,
    ].filter(Boolean).join(' • ') || 'Marker wiring pending';
    const warmingBits = [frame?.payload_state, baitGridPayload?.payload_state, markerEnvironment?.payload_state]
      .map((x) => String(x || '').toLowerCase())
      .filter((x) => x === 'warming' || x === 'fast_partial' || x === 'stale_while_revalidate');
    const warmSuffix = warmingBits.length ? ' • live data warming' : '';
    el.statusLine.textContent = `${scoreText(intel.opportunityScore)} setup • ${trendText(intel.opportunityScore, intel.baitState, intel.frontCount, intel.boilCount)} trend • ${safetyLabel(intel.safetyScore)} boating${warmSuffix}`;
    el.opportunityScore.textContent = `${Math.round(intel.opportunityScore)}%`;
    updateMeter(el.opportunityFill, intel.opportunityScore);
    el.confidence.textContent = `${confidenceLabel(intel.confidenceScore)} confidence • ${Math.round(intel.confidenceScore)}%`;
    el.trend.textContent = `${intel.trend} window`;
    el.safetyLine.textContent = `Safety ${Math.round(intel.safetyScore)}% • ${safetyLabel(intel.safetyScore)} • valid ${localOverlay?.validTime || wx?.valid_time || clouds?.valid_time || bait?.valid_time || 'n/a'}`;

    el.baitSummary.textContent = `${Math.round(intel.baitScore)}% • ${intel.baitState}`;
    updateMeter(el.baitFill, intel.baitScore);
    el.baitDrivers.innerHTML = listToHtml([
      profile?.summary || null,
      profile?.classification_reason || null,
      intel.frontCount > 0 ? `Temp breaks live in the box (${intel.frontCount})` : null,
      intel.convCount > 0 ? `Convergence support pockets (${intel.convCount})` : null,
      Number.isFinite(intel.currentKt) ? `Current pulse ${intel.currentKt.toFixed(1)} kt` : null,
      Number.isFinite(intel.cloudPct) ? `Cloud cover ${intel.cloudPct.toFixed(0)}% at orb` : null,
      intel.baitGridSample?.method ? `Orb bait score sampled from ${String(intel.baitGridSample.method).replace(/_/g, ' ')}` : null,
      Number.isFinite(intel.baitDepthIntel?.baitDepthFt) ? `Preferred bait depth ${Number(intel.baitDepthIntel.baitDepthFt).toFixed(1)} ft` : (Number.isFinite(intel.baitGridSample?.preferred_depth_m) ? `Preferred bait depth ${Number(intel.baitGridSample.preferred_depth_m).toFixed(0)} m` : null),
      Number.isFinite(intel.baitDepthIntel?.bottomDepthFt) ? `Bottom / mean depth ${Number(intel.baitDepthIntel.bottomDepthFt).toFixed(1)} ft` : null,
      Array.isArray(intel.baitDepthIntel?.bandFt) ? `Bait band ${Number(intel.baitDepthIntel.bandFt[0]).toFixed(1)}–${Number(intel.baitDepthIntel.bandFt[1]).toFixed(1)} ft` : null,
      Number.isFinite(intel.baitOceanVars?.sstF) ? `HYCOM SST ${Number(intel.baitOceanVars.sstF).toFixed(1)}°F` : null,
      Number.isFinite(intel.baitOceanVars?.currentKt) ? `HYCOM current ${Number(intel.baitOceanVars.currentKt).toFixed(2)} kt` : null,
      Number.isFinite(intel.baitOceanVars?.salinity) ? `HYCOM salinity ${Number(intel.baitOceanVars.salinity).toFixed(2)} PSU` : null,
      intel.baitDepthIntel?.source ? `Depth source ${String(intel.baitDepthIntel.source).replace(/_/g, ' ')}` : null,
      intel.baitGridSample?.driver ? `Grid driver ${String(intel.baitGridSample.driver).replace(/_/g, ' ')}` : null,
      intel.inlandMode ? `Inland factor blend → temp ${Math.round(intel.inlandFactors?.tempPct || 0)}% • current ${Math.round(intel.inlandFactors?.currentPct || 0)}% • depth ${Math.round(intel.inlandFactors?.depthPct || 0)}% • wind ${Math.round(intel.inlandFactors?.windPct || 0)}%` : null,
      intel.inlandMode && Number.isFinite(intel.inlandFactors?.depthFt) ? `Preferred inland bait depth ${Number(intel.inlandFactors.depthFt).toFixed(1)} ft` : null,
      intel.inlandMode && intel.speciesRows?.[0] ? `Top inland species read: ${intel.speciesRows[0].label} ${Math.round(intel.speciesRows[0].score)}%` : null,
      Number.isFinite(intel.positioning?.baitDistanceNm) && intel.positioning.baitDistanceNm > 0.05 ? `Nearest bait grid cell is ${intel.positioning.baitDistanceNm.toFixed(1)} nm off the orb` : null,
      bait?.bait?.meta?.valid_cells ? `${bait.bait.meta.valid_cells} active ocean cells in solve` : null,
      sharkIntel?.summary ? `Shark Intel pane sample: ${sharkIntel.summary}` : null,
    ].filter(Boolean));
    el.baitMovement.textContent = Number.isFinite(intel.currentKt)
      ? `${intel.inlandMode ? 'Lake drift' : 'Drifting'} ${safeFixed(intel.currentDir, 0, '°')} at ${safeFixed(intel.currentKt, 1, ' kt')} • bait state ${intel.baitState} • ${String(intel.baitGridSample?.method || 'grid sample pending').replace(/_/g, ' ')}`
      : `Movement read pending • bait state ${intel.baitState} • ${String(intel.baitGridSample?.method || 'grid sample pending').replace(/_/g, ' ')}`;

    if (el.sharkSummary && el.sharkFill && el.sharkDrivers && el.sharkDepth) {
      if (sharkIntel) {
        el.sharkSummary.textContent = sharkIntel.summary;
        updateMeter(el.sharkFill, sharkIntel.bestPct || 0);
        el.sharkDrivers.innerHTML = listToHtml(sharkIntel.drivers);
        const m = sharkIntel.metrics || {};
        const d = sharkIntel.depth || {};
        const swim = Array.isArray(d.target_swim_depth_ft || m.target_swim_depth_ft) ? (d.target_swim_depth_ft || m.target_swim_depth_ft) : null;
        el.sharkDepth.textContent = [
          Number.isFinite(Number(m.bottom_depth_ft ?? d.bottom_depth_ft)) ? `Bottom ${(Number(m.bottom_depth_ft ?? d.bottom_depth_ft)).toFixed(1)} ft` : null,
          swim ? `swim ${Number(swim[0]).toFixed(1)}–${Number(swim[1]).toFixed(1)} ft` : null,
          sharkIntel.fishingMode ? `mode ${String(sharkIntel.fishingMode).replace(/-/g, ' ')}` : null,
          sharkIntel.maskMethod ? `mask ${String(sharkIntel.maskMethod).replace(/_/g, ' ')}` : null,
        ].filter(Boolean).join(' • ') || 'Shark depth / SST mask pending';
      } else {
        el.sharkSummary.textContent = 'No SST-valid shark contour near this beacon yet';
        updateMeter(el.sharkFill, 0);
        el.sharkDrivers.innerHTML = listToHtml(['Waiting on shark-intel scene-cache near this coast/ocean location', 'Layer uses SST/ocean validity to avoid drawing land cells']);
        el.sharkDepth.textContent = 'Shark depth / SST mask pending';
      }
    }

    const speciesSlots = [intel.speciesRows?.[0], intel.speciesRows?.[1], intel.speciesRows?.[2], intel.speciesRows?.[3]];
    [
      [el.predatorLabel1, el.predatorScore1, el.predatorFill1, speciesSlots[0]],
      [el.predatorLabel2, el.predatorScore2, el.predatorFill2, speciesSlots[1]],
      [el.predatorLabel3, el.predatorScore3, el.predatorFill3, speciesSlots[2]],
      [el.predatorLabel4, el.predatorScore4, el.predatorFill4, speciesSlots[3]],
    ].forEach(([labelEl, scoreEl, fillEl, row], idx) => {
      labelEl.textContent = row?.label || `Species ${idx + 1}`;
      try {
        if (row?.factors) {
          labelEl.title = `temp ${Math.round(row.factors.temp)}% • depth ${Math.round(row.factors.depth)}% • current ${Math.round(row.factors.current)}% • wind ${Math.round(row.factors.wind)}% • bait ${Math.round(row.factors.bait)}%`;
        } else {
          labelEl.title = '';
        }
      } catch (_) {}
      scoreEl.textContent = row ? `${Math.round(row.score)}%` : '—';
      updateMeter(fillEl, row?.score || 0);
    });

    el.envNow.textContent = envDisplay.now;
    el.envMore.textContent = envDisplay.more;
    const boatSolveText = markerEnvironment?.ocean?.ok
      ? `Ocean/boat solve ${markerEnvironment.ocean.source_tier || 'active'} • ${formatSwellComponent('primary', intel.swell1)}`
      : (Number.isFinite(intel.positioning?.boatDistanceNm)
        ? `Boat solve ${intel.positioning.boatDistanceNm.toFixed(1)} nm from orb`
        : 'Boat solve sparse — using regional ocean conditions');
    const baitSolveText = Number.isFinite(intel.positioning?.baitDistanceNm)
      ? `${intel.inlandMode ? 'Inland bait solve' : 'Bait solve'} ${intel.positioning.baitDistanceNm.toFixed(1)} nm from orb`
      : `${intel.inlandMode ? 'Inland bait solve sparse — expanding lake model' : 'Bait solve sparse — expanding regional bait model'}`;
    el.envPosition.textContent = [
      envDisplay.source || null,
      boatSolveText,
      baitSolveText,
      Number.isFinite(intel.positioning?.boatLat) && Number.isFinite(intel.positioning?.boatLon) ? `Boat cell ${intel.positioning.boatLat.toFixed(3)}, ${intel.positioning.boatLon.toFixed(3)}` : null,
      Number.isFinite(intel.positioning?.baitLat) && Number.isFinite(intel.positioning?.baitLon) ? `Bait cell ${intel.positioning.baitLat.toFixed(3)}, ${intel.positioning.baitLon.toFixed(3)}` : null,
      Number.isFinite(intel.baitDepthIntel?.baitDepthFt) ? `Bait depth ${Number(intel.baitDepthIntel.baitDepthFt).toFixed(1)} ft` : null,
      Number.isFinite(intel.baitDepthIntel?.bottomDepthFt) ? `Bottom ${Number(intel.baitDepthIntel.bottomDepthFt).toFixed(1)} ft` : null,
    ].filter(Boolean).join(' • ');

    const primary = intel.swell1 ? formatSwellComponent('Primary swell', intel.swell1) : 'Primary swell n/a';
    const secondary = intel.swell2 ? formatSwellComponent('Secondary', intel.swell2) : 'Secondary n/a';
    const tertiary = intel.swell3 ? formatSwellComponent('Third', intel.swell3) : 'Third n/a';
    el.windowText.textContent = `Bias: dawn push → midday check → sunset recycle • ${primary} • ${secondary} • ${tertiary}`;
    el.moonBias.textContent = intel.moonBias;

    el.reasons.innerHTML = listToHtml(intel.reasons);
    el.risks.innerHTML = listToHtml(intel.risks);

    el.lastReport.textContent = reports.slice(-1)[0] || 'No reports yet';
    el.reports.innerHTML = '';
    reports.slice().reverse().forEach((r) => {
      const li = document.createElement('li');
      li.textContent = r;
      el.reports.appendChild(li);
    });

    el.tackle.textContent = intel.tackle;
    renderVideoFrame(el.videoFrame, vids);
  }

  el.saveReport.onclick = async () => {
    if (!selected) return;
    const text = el.reportInput.value.trim();
    if (!text) return;
    await postJsonSafe(`/gfs/api/location/${encodeURIComponent(selectedApiId())}/reports`, { report_text: text, text }, null);
    el.reportInput.value = '';
    await refresh();
  };

  el.uploadVideo.onclick = async () => {
    if (!selected || !el.uploadFile.files?.[0]) return;
    await uploadSafe(`/gfs/api/location/${encodeURIComponent(selectedApiId())}/upload`, el.uploadFile.files[0], {}, null);
    await refresh();
  };

  el.goLive.onclick = () => selected && onStartLive(selected);
  el.stopLive.onclick = () => selected && onStopLive(selected);

  return {
    async open(location) {
      selected = location;
      if (onSelectLocation) onSelectLocation(location);
      el.panel.classList.remove('closed');
      el.panel.setAttribute('aria-hidden', 'false');
      refresh().catch((err) => { el.statusLine.textContent = 'Intel refresh failed'; console.warn('[gfs hud] open refresh failed', err); });
      startSelectedAutoRefresh();
    },
    refreshSelected: refresh,
    stopAutoRefresh: stopSelectedAutoRefresh,
    selected: () => selected,
    updateHover(point, sample, baitInfo) {
      if (!el.hoverWeather) return;
      if (isPolygonHoverActive()) return;
      const lat = Number(point?.lat);
      const lon = Number(point?.lon);
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
        el.hoverWeather.textContent = 'Move cursor over globe';
        return;
      }
      const tempF = Number.isFinite(Number(sample?.temperature_f))
        ? Number(sample.temperature_f)
        : cToF(sample?.temperature_c);
      const pressure = Number(sample?.pressure_hpa);
      const wind = Number(sample?.wind_speed_mps);
      const baitDepth = Number(baitInfo?.preferred_depth_m);
      const baitProb = Number(baitInfo?.probability);
      const baitBandMin = Number(baitInfo?.depth_min_m);
      const baitBandMax = Number(baitInfo?.depth_max_m);
      const baitDriver = typeof baitInfo?.driver === 'string' ? baitInfo.driver.replace(/_/g, ' ') : '';
      const baitText = Number.isFinite(baitDepth)
        ? ` • Bait ${Number.isFinite(baitProb) ? Math.round(baitProb * 100) : 'n/a'}% @ ${baitDepth.toFixed(0)} m${Number.isFinite(baitBandMin) && Number.isFinite(baitBandMax) ? ` (${baitBandMin.toFixed(0)}–${baitBandMax.toFixed(0)} m)` : ''}${baitDriver ? ` • ${baitDriver}` : ''}`
        : '';
      el.hoverWeather.textContent = `@ ${lat.toFixed(3)}, ${lon.toFixed(3)} • Temp ${Number.isFinite(tempF) ? tempF.toFixed(1) : 'n/a'}°F • Pressure ${Number.isFinite(pressure) ? pressure.toFixed(1) : 'n/a'} hPa • Wind ${Number.isFinite(wind) ? wind.toFixed(1) : 'n/a'} m/s${baitText}`;
    },
  };
}
