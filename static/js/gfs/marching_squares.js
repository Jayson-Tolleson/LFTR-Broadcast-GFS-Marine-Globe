const DEFAULT_MAX_GRID = 46;
const EPS = 1e-9;

function num(v, fallback = NaN) {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

function uniqSorted(values, precision = 6) {
  const set = new Set();
  for (const v of values) {
    const n = num(v);
    if (Number.isFinite(n)) set.add(n.toFixed(precision));
  }
  return [...set].map(Number).sort((a, b) => a - b);
}

function downsampleAxis(axis, maxN) {
  if (axis.length <= maxN) return axis;
  const out = [];
  for (let i = 0; i < maxN; i += 1) {
    const idx = Math.round((i * (axis.length - 1)) / Math.max(1, maxN - 1));
    out.push(axis[idx]);
  }
  return uniqSorted(out, 7);
}

function edgesFromCenters(centers, loFallback, hiFallback) {
  if (!centers.length) return [];
  if (centers.length === 1) {
    const c = centers[0];
    const span = Math.max(0.02, Math.abs(hiFallback - loFallback) || 0.1);
    return [c - span * 0.5, c + span * 0.5];
  }
  const edges = [];
  const firstStep = centers[1] - centers[0];
  edges.push(centers[0] - firstStep * 0.5);
  for (let i = 0; i < centers.length - 1; i += 1) edges.push((centers[i] + centers[i + 1]) * 0.5);
  const lastStep = centers[centers.length - 1] - centers[centers.length - 2];
  edges.push(centers[centers.length - 1] + lastStep * 0.5);
  if (Number.isFinite(loFallback)) edges[0] = Math.max(Math.min(edges[0], hiFallback), loFallback);
  if (Number.isFinite(hiFallback)) edges[edges.length - 1] = Math.min(Math.max(edges[edges.length - 1], loFallback), hiFallback);
  return edges;
}

function buildUniformAxis(lo, hi, count) {
  const out = [];
  const n = Math.max(2, count);
  for (let i = 0; i < n; i += 1) out.push(lo + ((i + 0.5) * (hi - lo) / n));
  return out;
}

function cellSizeFromPoints(points, fallback = 0.25) {
  const lats = uniqSorted(points.map((p) => p.lat), 5);
  const lons = uniqSorted(points.map((p) => p.lon ?? p.lng), 5);
  const deltas = [];
  for (let i = 1; i < lats.length; i += 1) deltas.push(Math.abs(lats[i] - lats[i - 1]));
  for (let i = 1; i < lons.length; i += 1) deltas.push(Math.abs(lons[i] - lons[i - 1]));
  const valid = deltas.filter((d) => d > EPS).sort((a, b) => a - b);
  return valid.length ? clamp(valid[Math.floor(valid.length * 0.35)], 0.01, 0.75) : fallback;
}

function makeGrid(points, valueAccessor, opts = {}) {
  const clean = (Array.isArray(points) ? points : [])
    .map((p) => ({ raw: p, lat: num(p?.lat), lon: num(p?.lon ?? p?.lng), value: num(valueAccessor(p)) }))
    .filter((p) => Number.isFinite(p.lat) && Number.isFinite(p.lon) && Number.isFinite(p.value));
  if (!clean.length) return null;

  const bbox = Array.isArray(opts.bbox) ? opts.bbox.map(Number) : null;
  const west = Number.isFinite(bbox?.[0]) ? bbox[0] : Math.min(...clean.map((p) => p.lon));
  const south = Number.isFinite(bbox?.[1]) ? bbox[1] : Math.min(...clean.map((p) => p.lat));
  const east = Number.isFinite(bbox?.[2]) ? bbox[2] : Math.max(...clean.map((p) => p.lon));
  const north = Number.isFinite(bbox?.[3]) ? bbox[3] : Math.max(...clean.map((p) => p.lat));
  const maxGrid = opts.maxGrid || DEFAULT_MAX_GRID;
  let latAxis = downsampleAxis(uniqSorted(clean.map((p) => p.lat), 5), maxGrid);
  let lonAxis = downsampleAxis(uniqSorted(clean.map((p) => p.lon), 5), maxGrid);

  if (latAxis.length < 4 || lonAxis.length < 4) {
    const step = cellSizeFromPoints(clean, 0.25);
    const ny = clamp(Math.ceil(Math.max(0.1, north - south) / step), 8, maxGrid);
    const nx = clamp(Math.ceil(Math.max(0.1, east - west) / step), 8, maxGrid);
    latAxis = buildUniformAxis(south, north, ny);
    lonAxis = buildUniformAxis(west, east, nx);
  }

  const ny = latAxis.length;
  const nx = lonAxis.length;
  const grid = Array.from({ length: ny }, () => Array.from({ length: nx }, () => NaN));
  const hits = Array.from({ length: ny }, () => Array.from({ length: nx }, () => 0));
  const nearestIndex = (axis, v) => {
    let best = 0;
    let bestD = Infinity;
    for (let i = 0; i < axis.length; i += 1) {
      const d = Math.abs(axis[i] - v);
      if (d < bestD) { bestD = d; best = i; }
    }
    return best;
  };

  for (const p of clean) {
    const i = nearestIndex(latAxis, p.lat);
    const j = nearestIndex(lonAxis, p.lon);
    grid[i][j] = Number.isFinite(grid[i][j]) ? grid[i][j] + p.value : p.value;
    hits[i][j] += 1;
  }
  for (let i = 0; i < ny; i += 1) {
    for (let j = 0; j < nx; j += 1) if (hits[i][j]) grid[i][j] /= hits[i][j];
  }

  const latSpan = Math.max(0.01, north - south);
  const lonSpan = Math.max(0.01, east - west);
  const maxDist = opts.maxGapDeg || Math.max(latSpan / Math.max(6, ny), lonSpan / Math.max(6, nx)) * 3.2;
  for (let i = 0; i < ny; i += 1) {
    for (let j = 0; j < nx; j += 1) {
      if (Number.isFinite(grid[i][j])) continue;
      const lat = latAxis[i];
      const lon = lonAxis[j];
      let sumW = 0;
      let sumV = 0;
      for (const p of clean) {
        const dLat = lat - p.lat;
        const dLon = (lon - p.lon) * Math.max(0.25, Math.cos(lat * Math.PI / 180));
        const d = Math.hypot(dLat, dLon);
        if (d > maxDist) continue;
        const w = 1 / Math.max(0.0002, d * d);
        sumW += w;
        sumV += p.value * w;
      }
      if (sumW > 0) grid[i][j] = sumV / sumW;
    }
  }

  return { grid, latAxis, lonAxis, latEdges: edgesFromCenters(latAxis, south, north), lonEdges: edgesFromCenters(lonAxis, west, east), bbox: [west, south, east, north], points: clean };
}

function pointKey(lon, lat) { return `${lon.toFixed(7)},${lat.toFixed(7)}`; }
function parseKey(k) { const [lon, lat] = k.split(',').map(Number); return [lon, lat]; }

function smoothRing(coords, passes = 1) {
  let ring = coords;
  for (let p = 0; p < passes; p += 1) {
    if (ring.length < 4) return ring;
    const out = [];
    for (let i = 0; i < ring.length; i += 1) {
      const a = ring[i];
      const b = ring[(i + 1) % ring.length];
      out.push([a[0] * 0.75 + b[0] * 0.25, a[1] * 0.75 + b[1] * 0.25]);
      out.push([a[0] * 0.25 + b[0] * 0.75, a[1] * 0.25 + b[1] * 0.75]);
    }
    ring = out;
  }
  return ring;
}

function stitchEdges(edges) {
  const next = new Map();
  for (const e of edges) {
    const s = pointKey(e[0][0], e[0][1]);
    if (!next.has(s)) next.set(s, []);
    next.get(s).push(e);
  }
  const loops = [];
  const guardMax = edges.length + 8;
  for (const e of edges) {
    if (e.used) continue;
    const loop = [];
    let cur = e;
    let guard = 0;
    while (cur && !cur.used && guard < guardMax) {
      cur.used = true;
      loop.push(cur[0]);
      const endKey = pointKey(cur[1][0], cur[1][1]);
      const candidates = next.get(endKey) || [];
      cur = candidates.find((x) => !x.used) || null;
      guard += 1;
      if (!cur && loop.length >= 3) {
        const start = loop[0];
        const end = parseKey(endKey);
        if (Math.abs(start[0] - end[0]) > 1e-5 || Math.abs(start[1] - end[1]) > 1e-5) loop.push(end);
      }
    }
    if (loop.length >= 3) loops.push(loop);
  }
  return loops;
}

function polygonArea(coords) {
  let area = 0;
  for (let i = 0; i < coords.length; i += 1) {
    const a = coords[i];
    const b = coords[(i + 1) % coords.length];
    area += a[0] * b[1] - b[0] * a[1];
  }
  return area * 0.5;
}

function componentsForThreshold(field, threshold, upper = Infinity) {
  const { grid, latEdges, lonEdges } = field;
  const ny = grid.length;
  const nx = grid[0]?.length || 0;
  const active = (i, j) => {
    if (i < 0 || j < 0 || i >= ny || j >= nx) return false;
    const v = grid[i][j];
    return Number.isFinite(v) && v >= threshold && v < upper;
  };
  const visited = Array.from({ length: ny }, () => Array.from({ length: nx }, () => false));
  const polys = [];

  for (let i = 0; i < ny; i += 1) {
    for (let j = 0; j < nx; j += 1) {
      if (!active(i, j) || visited[i][j]) continue;
      const stack = [[i, j]];
      const cells = [];
      visited[i][j] = true;
      while (stack.length) {
        const [ci, cj] = stack.pop();
        cells.push([ci, cj]);
        for (const [di, dj] of [[1,0],[-1,0],[0,1],[0,-1]]) {
          const ni = ci + di;
          const nj = cj + dj;
          if (ni < 0 || nj < 0 || ni >= ny || nj >= nx || visited[ni][nj] || !active(ni, nj)) continue;
          visited[ni][nj] = true;
          stack.push([ni, nj]);
        }
      }
      const cellSet = new Set(cells.map(([ci, cj]) => `${ci},${cj}`));
      const isInComponent = (ci, cj) => cellSet.has(`${ci},${cj}`);
      const edges = [];
      let sum = 0;
      let minV = Infinity;
      let maxV = -Infinity;
      for (const [ci, cj] of cells) {
        const v = grid[ci][cj];
        sum += v; minV = Math.min(minV, v); maxV = Math.max(maxV, v);
        const south = latEdges[ci];
        const north = latEdges[ci + 1];
        const west = lonEdges[cj];
        const east = lonEdges[cj + 1];
        if (!isInComponent(ci - 1, cj)) edges.push([[west, south], [east, south]]);
        if (!isInComponent(ci, cj + 1)) edges.push([[east, south], [east, north]]);
        if (!isInComponent(ci + 1, cj)) edges.push([[east, north], [west, north]]);
        if (!isInComponent(ci, cj - 1)) edges.push([[west, north], [west, south]]);
      }
      const loops = stitchEdges(edges);
      for (const loop of loops) {
        const area = Math.abs(polygonArea(loop));
        if (area < 0.00004) continue;
        const coords = smoothRing(loop, 1);
        polys.push({ coordinates: coords, probability: clamp(sum / cells.length, 0, 1), value: sum / cells.length, min_value: minV, max_value: maxV, cells: cells.length, area_deg2: area });
      }
    }
  }
  return polys.sort((a, b) => (b.area_deg2 || 0) - (a.area_deg2 || 0));
}

export function contourPolygonsFromPoints({ points, valueAccessor, thresholds, bbox, maxGrid = DEFAULT_MAX_GRID, maxGapDeg, capPerBand = 32 }) {
  const field = makeGrid(points, valueAccessor, { bbox, maxGrid, maxGapDeg });
  if (!field) return { bands: {}, grid: null, source: 'no_points' };
  const bands = {};
  const sorted = [...thresholds].sort((a, b) => a.value - b.value);
  for (let idx = 0; idx < sorted.length; idx += 1) {
    const band = sorted[idx];
    const upper = band.upper == null ? Infinity : band.upper;
    bands[band.name] = componentsForThreshold(field, band.value, upper).slice(0, capPerBand).map((p) => ({ ...p, band: band.name, threshold: band.value, source: 'interpolated_marching_squares' }));
  }
  return { bands, grid: { nx: field.lonAxis.length, ny: field.latAxis.length, bbox: field.bbox }, source: 'interpolated_marching_squares' };
}
