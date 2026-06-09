import { getJsonSafe } from './api.js';

// Sky is a clock/viewport visual, not a live weather layer. Keep it cheap.
const SKY_REFRESH_MS = 300000; // sun angle changes slowly; 5 min is plenty
const SKY_MIN_FETCH_MS = 120000; // never refetch from camera jitter faster than 2 min
const SKY_MIN_APPLY_MS = 900; // avoid repeated DOM/style writes during camera settles
const SKY_STAR_RANGE_M = 250000;
const SKY_STAR_TILT_DEG = 55;
const SKY_MODES_WITH_STARS = new Set(['night', 'astronomical']);
const SKY_DISABLED_KEY = 'gfs_sky_disabled';

let lastAppliedSig = '';
let lastAppliedAt = 0;

function skyDisabled() {
  try { return window.localStorage?.getItem(SKY_DISABLED_KEY) === '1'; } catch (_) { return false; }
}

function clampPct(value, fallback = 1) {
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.max(0, Math.min(1, n));
}

function formatSkyLine(sky, starsVisible) {
  const mode = sky?.mode || 'day';
  const elev = Number(sky?.sun_elevation_deg);
  const elevText = Number.isFinite(elev) ? `${elev.toFixed(1)}°` : 'n/a';
  return `Sky ${mode} • sun ${elevText} • stars ${starsVisible ? 'on' : 'off'}`;
}

function ensureStarfield() {
  let el = document.getElementById('gfsStarfield');
  if (el) return el;
  const shell = document.querySelector('.globe-shell') || document.body;
  el = document.createElement('div');
  el.id = 'gfsStarfield';
  el.className = 'gfs-starfield';
  el.setAttribute('aria-hidden', 'true');

  const stars = document.createElement('div');
  stars.className = 'gfs-stars';
  const seed = 1487;
  let state = seed;
  const rnd = () => {
    state = (state * 1664525 + 1013904223) >>> 0;
    return state / 4294967296;
  };
  // Keep this small. A single box-shadow node is cheap, but 150+ shadows plus
  // animated filters on map overlays can be painful on small GCP/client CPUs.
  const count = 72;
  const parts = [];
  for (let i = 0; i < count; i += 1) {
    const x = Math.round(rnd() * 10000) / 100;
    const y = Math.round(rnd() * 10000) / 100;
    const size = 0.7 + Math.round(rnd() * 110) / 100;
    const opacity = 0.32 + rnd() * 0.55;
    parts.push(`${x}vw ${y}vh 0 ${size * 0.22}px rgba(255,255,255,${opacity.toFixed(2)})`);
  }
  stars.style.boxShadow = parts.join(',');
  el.appendChild(stars);

  const glow = document.createElement('div');
  glow.className = 'gfs-horizon-glow';
  el.appendChild(glow);
  shell.prepend(el);
  return el;
}

function applySkyState(sky, camera, { force = false } = {}) {
  if (skyDisabled()) {
    document.body.dataset.sky = 'day';
    document.body.dataset.gfsStars = 'off';
    const starfield = document.getElementById('gfsStarfield');
    if (starfield) starfield.classList.remove('visible');
    return false;
  }

  const mode = String(sky?.mode || 'day');
  const tilt = Number(camera?.tilt ?? 0);
  const range = Number(camera?.range ?? 9999999);
  const starEligible = Boolean(sky?.stars) && SKY_MODES_WITH_STARS.has(mode);
  const starsVisible = starEligible && Number.isFinite(range) && range < SKY_STAR_RANGE_M && Number.isFinite(tilt) && tilt > SKY_STAR_TILT_DEG;
  const sig = [
    mode,
    starsVisible ? 'stars' : 'nostars',
    Math.round(Number(camera?.range ?? 0) / 25000),
    Math.round(Number(camera?.tilt ?? 0) / 5),
    clampPct(sky?.atmosphere_opacity, 1).toFixed(2),
    clampPct(sky?.cloud_opacity, 1).toFixed(2),
    clampPct(sky?.horizon_glow, 0.12).toFixed(2),
  ].join('|');
  const now = Date.now();
  if (!force && sig === lastAppliedSig && (now - lastAppliedAt) < SKY_MIN_APPLY_MS) {
    return starsVisible;
  }
  lastAppliedSig = sig;
  lastAppliedAt = now;

  document.body.dataset.sky = mode;
  document.body.dataset.gfsStars = starsVisible ? 'on' : 'off';
  document.documentElement.style.setProperty('--gfs-atmosphere-opacity', String(clampPct(sky?.atmosphere_opacity, 1)));
  document.documentElement.style.setProperty('--gfs-cloud-opacity', String(clampPct(sky?.cloud_opacity, 1)));
  document.documentElement.style.setProperty('--gfs-horizon-glow-opacity', String(clampPct(sky?.horizon_glow, 0.12)));

  const starfield = starsVisible ? ensureStarfield() : document.getElementById('gfsStarfield');
  if (starfield) starfield.classList.toggle('visible', starsVisible);

  const line = formatSkyLine(sky, starsVisible);
  const status = document.getElementById('status');
  if (status && status.dataset.skyLine !== line) {
    status.dataset.skyLine = line;
    const current = status.textContent || '';
    const base = current.split(' • Sky ')[0] || 'Ready';
    status.textContent = `${base} • ${line}`;
  }
  window.__gfsSkyState = { ...(sky || {}), stars_visible: starsVisible, camera: { ...camera } };
  return starsVisible;
}

export function startSkySystem({ getViewport, getCameraAngles, getRangeMeters, deferInitialMs = 0 } = {}) {
  let lastFetchAt = 0;
  let lastKey = '';
  let timer = null;
  let inFlight = null;
  let lastSky = window.__gfsSkyState || { mode: 'day', stars: false, atmosphere_opacity: 1, cloud_opacity: 1, horizon_glow: 0.18 };

  const cameraSnapshot = () => {
    const viewport = (typeof getViewport === 'function') ? getViewport() : null;
    const center = viewport?.camera?.center || null;
    const angles = (typeof getCameraAngles === 'function') ? getCameraAngles() : {};
    const range = Number(viewport?.camera?.range ?? (typeof getRangeMeters === 'function' ? getRangeMeters() : NaN));
    return {
      lat: Number(center?.lat),
      lon: Number(center?.lon),
      range: Number.isFinite(range) ? range : 9999999,
      tilt: Number(angles?.tilt ?? 0),
      heading: Number(angles?.heading ?? 0),
      roll: Number(angles?.roll ?? 0),
    };
  };

  const refresh = async (reason = 'timer') => {
    const camera = cameraSnapshot();
    if (!Number.isFinite(camera.lat) || !Number.isFinite(camera.lon)) {
      applySkyState(lastSky, camera, { force: reason === 'boot' });
      return lastSky;
    }
    const key = `${camera.lat.toFixed(1)},${camera.lon.toFixed(1)}`;
    const now = Date.now();
    const canReuseSky = key === lastKey && (now - lastFetchAt) < SKY_MIN_FETCH_MS;
    if (canReuseSky) {
      applySkyState(lastSky, camera);
      return lastSky;
    }
    if (inFlight) return inFlight;
    lastKey = key;
    lastFetchAt = now;
    const url = `/gfs/api/sky?lat=${encodeURIComponent(camera.lat.toFixed(4))}&lon=${encodeURIComponent(camera.lon.toFixed(4))}`;
    inFlight = getJsonSafe(url, null, { abortPrevious: false, timeoutMs: 5000 })
      .then((payload) => {
        if (payload?.ok !== false && payload?.mode) lastSky = payload;
        const starsVisible = applySkyState(lastSky, camera, { force: reason === 'boot' });
        console.info('[gfs/sky] updated', {
          reason,
          mode: lastSky?.mode,
          sun_elevation_deg: lastSky?.sun_elevation_deg,
          stars: starsVisible,
          range: Math.round(camera.range),
          tilt: Math.round(camera.tilt),
        });
        return lastSky;
      })
      .catch((err) => {
        applySkyState(lastSky, camera);
        console.info('[gfs/sky] using last/default sky after fetch failure', { reason, message: err?.message || String(err) });
        return lastSky;
      })
      .finally(() => { inFlight = null; });
    return inFlight;
  };

  const startTimer = () => {
    if (timer) return;
    timer = window.setInterval(() => refresh('timer'), SKY_REFRESH_MS);
  };

  applySkyState(lastSky, cameraSnapshot(), { force: true });
  startTimer();
  const deferMs = Number(deferInitialMs || 0);
  if (deferMs > 0) window.setTimeout(() => refresh('boot'), deferMs);
  else refresh('boot');

  return {
    refresh,
    teardown() {
      if (timer) window.clearInterval(timer);
      timer = null;
    },
  };
}
