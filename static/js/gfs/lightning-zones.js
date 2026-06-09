import { attachPolygonHover } from './hover_tip.js';

const MAX_LIGHTNING_MARKERS = 240;
const DEFAULT_LIGHTNING_FLASH_TTL_SECONDS = Number(window.GFS_GLM_FLASH_TTL_SECONDS || 600);
const LIGHTNING_FADE_OUT_MS = Number(window.GFS_LIGHTNING_FADE_OUT_MS || 2200);

function toNumber(v, fallback = 0) {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

function normalizeFlashShape(item) {
  if (!item || typeof item !== 'object') return null;
  const center = item.center || item.properties?.center || {};
  const lat = toNumber(item.lat ?? item.latitude ?? center.lat, NaN);
  const lon = toNumber(item.lon ?? item.lng ?? item.longitude ?? center.lon ?? center.lng, NaN);
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
  const risk = toNumber(item.energy_j ?? item.energy ?? item.flash_energy ?? item.visual_priority ?? item.severity ?? item.source_fields?.inferred_lightning_risk, 0);
  const ttl = Math.max(5, toNumber(item.event_ttl_seconds ?? item.particle_ttl_seconds, DEFAULT_LIGHTNING_FLASH_TTL_SECONDS));
  const age = Math.max(0, toNumber(item.age_seconds, 0));
  const expiresIn = Math.min(ttl - age, toNumber(item.expires_in_seconds, ttl - age));
  if (!Number.isFinite(expiresIn) || expiresIn <= 0) return null;
  return {
    ...item,
    lat,
    lon,
    energy_j: Number.isFinite(toNumber(item.energy_j, NaN)) ? item.energy_j : risk,
    age_seconds: age,
    event_ttl_seconds: ttl,
    expires_in_seconds: expiresIn,
    source: item.source || item.properties?.source || 'goes_glm_l2_lcfa',
    product: item.product || 'GLM-L2-LCFA',
  };
}

function flashesFromPayload(payload) {
  if (!payload) return [];
  const direct = payload.flashes || payload.items || payload.events || payload.features || [];
  if (!Array.isArray(direct)) return [];
  return direct.map(normalizeFlashShape).filter(Boolean);
}

function clearLightning(map3DElement) {
  // Scoped clear: lightning event particles are independent from clouds/rain and
  // must never remove other weather layers.
  try { map3DElement?.querySelectorAll?.('[data-gfs-layer="lightning"]')?.forEach((el) => el.remove()); } catch (_) {}
}

function fadeRemoveLightningElement(el, durationMs = LIGHTNING_FADE_OUT_MS) {
  if (!el || el.__gfsLightningRemoving) return;
  el.__gfsLightningRemoving = true;
  try {
    el.style.transition = `opacity ${Math.max(120, durationMs)}ms ease-out, transform ${Math.max(120, durationMs)}ms ease-out`;
    el.style.opacity = '0';
    el.style.transform = 'scale(0.72)';
  } catch (_) {}
  window.setTimeout(() => { try { el.remove(); } catch (_) {} }, Math.max(140, durationMs));
}

function scheduleLightningExpiry(el, item) {
  const expiresIn = Math.max(0, toNumber(item?.expires_in_seconds, 0));
  const fadeMs = Math.max(120, LIGHTNING_FADE_OUT_MS);
  if (!expiresIn) { fadeRemoveLightningElement(el, fadeMs); return; }
  const delayMs = Math.max(0, expiresIn * 1000 - fadeMs);
  try { el.setAttribute('data-lightning-expires-in-seconds', expiresIn.toFixed(1)); } catch (_) {}
  window.setTimeout(() => fadeRemoveLightningElement(el, fadeMs), delayMs);
}

function makeLightningTemplate(flash) {
  const energy = Math.max(0, toNumber(flash.energy_j || flash.energy || flash.flash_energy || 0));
  const age = Math.max(0, toNumber(flash.age_seconds, 0));
  const fresh = clamp(1 - age / 1800, 0.25, 1);
  const scale = clamp(0.85 + Math.log10(energy + 10) * 0.12, 0.9, 1.65);
  const opacity = clamp(0.35 + fresh * 0.58, 0.35, 0.95);
  const uid = `glm-${Math.random().toString(16).slice(2)}`;
  const tpl = document.createElement('template');
  tpl.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 96 96" style="overflow:visible;pointer-events:none;transform:scale(${scale.toFixed(2)})">
    <defs>
      <filter id="${uid}-glow" x="-80%" y="-80%" width="260%" height="260%"><feGaussianBlur stdDeviation="4" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
      <radialGradient id="${uid}-halo" cx="50%" cy="50%" r="50%"><stop offset="0%" stop-color="#fff7b4" stop-opacity="${opacity.toFixed(2)}"/><stop offset="52%" stop-color="#60a5fa" stop-opacity="${(opacity*0.45).toFixed(2)}"/><stop offset="100%" stop-color="#60a5fa" stop-opacity="0"/></radialGradient>
    </defs>
    <circle cx="48" cy="48" r="34" fill="url(#${uid}-halo)"/>
    <path d="M49 5 L29 44 L45 44 L35 91 L68 38 L51 39 L62 5 Z" fill="#fff" stroke="#fde047" stroke-width="3.5" stroke-linejoin="round" filter="url(#${uid}-glow)" opacity="${opacity.toFixed(2)}"/>
    <path d="M53 12 L39 39 L51 39 L45 68 L61 35 L51 36 Z" fill="#bfdbfe" opacity="${(0.72*opacity).toFixed(2)}"/>
  </svg>`;
  return tpl;
}

function createFlashMarker(flash) {
  const lat = toNumber(flash.lat ?? flash.latitude, NaN);
  const lon = toNumber(flash.lon ?? flash.lng ?? flash.longitude, NaN);
  if (!Number.isFinite(lat) || !Number.isFinite(lon) || Math.abs(lat) > 90 || Math.abs(lon) > 180) return null;
  const age = Math.max(0, toNumber(flash.age_seconds, 0));
  const energy = toNumber(flash.energy_j || flash.energy || flash.flash_energy || 0);
  const altitude = clamp(9000 - Math.min(5500, age * 2.2), 2600, 9600);
  const marker = document.createElement('gmp-marker-3d');
  marker.position = { lat, lng: lon, altitude };
  marker.drawsWhenOccluded = true;
  marker.sizePreserved = true;
  marker.setAttribute('data-gfs-layer', 'lightning');
  marker.setAttribute('data-lightning-source', flash.source || 'goes_glm_l2_lcfa');
  marker.setAttribute('data-lightning-age-seconds', String(Math.round(age)));
  marker.setAttribute('data-lightning-ttl-seconds', String(Math.round(toNumber(flash.event_ttl_seconds, DEFAULT_LIGHTNING_FLASH_TTL_SECONDS))));
  marker.style.opacity = '1';
  marker.append(makeLightningTemplate(flash));
  attachPolygonHover(marker, {
    title: 'GOES GLM lightning',
    detail: `${lat.toFixed(3)}, ${lon.toFixed(3)}`,
    lines: [
      `age: ${Math.round(age)} s`,
      `energy: ${energy ? Number(energy).toExponential(2) : 'n/a'}`,
      `satellite: ${flash.satellite || flash.platform || 'GOES GLM'}`,
      `time: ${flash.time || flash.valid_time || 'recent'}`,
    ],
    metrics: {
      layer: 'lightning',
      source: flash.source || 'goes_glm_l2_lcfa',
      product: flash.product || 'GLM-L2-LCFA',
    },
    payload: { lat, lon, age_seconds: age, energy_j: energy, source: flash.source || 'goes_glm_l2_lcfa', satellite: flash.satellite || flash.platform || 'GOES GLM', time: flash.time || flash.valid_time || 'recent' },
  });
  return marker;
}

function createClusterHalo(region) {
  const lat = toNumber(region?.center?.lat ?? region?.lat ?? region?.properties?.center?.lat, NaN);
  const lon = toNumber(region?.center?.lon ?? region?.center?.lng ?? region?.lon ?? region?.lng ?? region?.properties?.center?.lon, NaN);
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
  const count = Math.max(1, toNumber(region.flash_count || region.count, 1));
  const radius = clamp(18 + Math.sqrt(count) * 8, 18, 82);
  const tpl = document.createElement('template');
  tpl.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="140" height="140" viewBox="0 0 140 140" style="overflow:visible;pointer-events:none">
    <circle cx="70" cy="70" r="${radius.toFixed(1)}" fill="#fef08a" fill-opacity="0.07" stroke="#facc15" stroke-opacity="0.42" stroke-width="3"/>
    <circle cx="70" cy="70" r="${Math.max(8, radius*0.34).toFixed(1)}" fill="#60a5fa" fill-opacity="0.10"/>
  </svg>`;
  const marker = document.createElement('gmp-marker-3d');
  marker.position = { lat, lng: lon, altitude: 7200 };
  marker.drawsWhenOccluded = true;
  marker.sizePreserved = true;
  marker.setAttribute('data-gfs-layer', 'lightning');
  marker.setAttribute('data-lightning-region', 'cluster');
  marker.setAttribute('data-lightning-age-seconds', String(Math.round(toNumber(region.age_seconds, 0))));
  marker.setAttribute('data-lightning-ttl-seconds', String(Math.round(toNumber(region.event_ttl_seconds, DEFAULT_LIGHTNING_FLASH_TTL_SECONDS))));
  marker.style.opacity = '1';
  marker.append(tpl);
  attachPolygonHover(marker, {
    title: 'Lightning cluster',
    detail: `${count} recent flashes`,
    lines: [`center: ${lat.toFixed(3)}, ${lon.toFixed(3)}`, `age: ${Math.round(toNumber(region.age_seconds, 0))} s`],
    metrics: { layer: 'lightning', source: region.source || 'goes_glm_l2_lcfa' },
    payload: { flash_count: count, lat, lon, age_seconds: toNumber(region.age_seconds, 0), source: region.source || 'goes_glm_l2_lcfa' },
  });
  return marker;
}

export function renderLightningLayer({ payload, map3DElement }) {
  if (!map3DElement) return () => {};
  clearLightning(map3DElement);
  const flashes = flashesFromPayload(payload)
    .filter((f) => Number.isFinite(toNumber(f.lat ?? f.latitude, NaN)) && Number.isFinite(toNumber(f.lon ?? f.lng ?? f.longitude, NaN)))
    .sort((a, b) => toNumber(a.age_seconds, 0) - toNumber(b.age_seconds, 0))
    .slice(0, MAX_LIGHTNING_MARKERS);
  const eventTtl = Math.max(5, toNumber(payload?.event_ttl_seconds ?? payload?.particle_ttl_seconds, DEFAULT_LIGHTNING_FLASH_TTL_SECONDS));
  const regions = (Array.isArray(payload?.regions) ? payload.regions : [])
    .map((r) => ({ ...r, event_ttl_seconds: eventTtl, expires_in_seconds: Math.max(0, eventTtl - toNumber(r.age_seconds, 0)) }))
    .filter((r) => toNumber(r.expires_in_seconds, 0) > 0)
    .slice(0, 40);
  const frag = document.createDocumentFragment();
  let markers = 0;
  for (const r of regions) {
    const halo = createClusterHalo(r);
    if (halo) { scheduleLightningExpiry(halo, r); frag.append(halo); markers += 1; }
  }
  for (const f of flashes) {
    const marker = createFlashMarker(f);
    if (marker) { scheduleLightningExpiry(marker, f); frag.append(marker); markers += 1; }
  }
  if (markers) map3DElement.append(frag);
  console.info('[gfs lightning] rendered', {
    source: payload?.source,
    state: payload?.payload_state || payload?.status,
    flashes: flashes.length,
    regions: regions.length,
    markers,
    eventTtlSeconds: eventTtl,
    fallback: Boolean(payload?.fallback_used),
  });
  return () => clearLightning(map3DElement);
}
