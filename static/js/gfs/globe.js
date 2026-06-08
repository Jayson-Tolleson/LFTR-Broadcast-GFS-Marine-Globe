import { getJson } from './api.js';

let bootstrapPromise = null;


function normalizeConfig(cfg) {
  if (!cfg || typeof cfg !== 'object') {
    return { google_maps_api_key: '', runtime: {}, debug: false };
  }
  return {
    google_maps_api_key: String(cfg.google_maps_api_key || ''),
    runtime: (cfg.runtime && typeof cfg.runtime === 'object') ? cfg.runtime : {},
    debug: Boolean(cfg.debug),
  };
}

async function loadConfigSafe() {
  const cfg = await getJson('/gfs/api/config', { timeoutMs: 12000 });
  if (!cfg) {
    console.error('[gfs globe] config load failed: /gfs/api/config unavailable');
  }
  return normalizeConfig(cfg);
}

function bootstrapMaps({ key }) {
  if (window.google?.maps?.importLibrary) {
    return Promise.resolve(true);
  }
  if (bootstrapPromise) {
    return bootstrapPromise;
  }

  bootstrapPromise = new Promise((resolve, reject) => {
    try {
      const g = window.google || (window.google = {});
      const maps = g.maps || (g.maps = {});

      if (maps.__gfsBootstrapReady) {
        resolve(true);
        return;
      }

      const p = 'The Google Maps JavaScript API';
      const c = 'google';
      const l = 'importLibrary';
      const q = '__ib__';
      const m = document;
      const b = window;
      const d = maps;
      const r = new Set();
      const e = new URLSearchParams();
      let h;
      let a;
      let k;

      const u = () =>
        h ||
        (h = new Promise(async (f, n) => {
          await (a = m.createElement('script'));
          e.set('libraries', [...r] + '');
          for (k in { key, v: 'beta', loading: 'async' }) {
            if ({ key, v: 'beta', loading: 'async' }[k]) {
              e.set(k.replace(/[A-Z]/g, (t) => '_' + t[0].toLowerCase()), { key, v: 'beta', loading: 'async' }[k]);
            }
          }
          e.set('callback', c + '.maps.' + q);
          a.src = `https://maps.${c}apis.com/maps/api/js?` + e;
          d[q] = f;
          a.onerror = () => n(new Error(p + ' could not load.'));
          a.nonce = m.querySelector('script[nonce]')?.nonce || '';
          m.head.append(a);
        }));

      if (d[l]) {
        // Another bootstrap already installed.
        maps.__gfsBootstrapReady = true;
        resolve(true);
        return;
      }

      d[l] = (f, ...n) => (r.add(f), u().then(() => d[l](f, ...n)));

      u()
        .then(() => {
          maps.__gfsBootstrapReady = true;
          resolve(true);
        })
        .catch(reject);
    } catch (err) {
      reject(err);
    }
  });

  return bootstrapPromise;
}

export async function ensureMaps3D(_globeEl, fallbackEl) {
  const cfg = await loadConfigSafe();
  const key = cfg.google_maps_api_key;

  if (!key) {
    const reason = 'missing_google_maps_api_key';
    console.error('[gfs globe] maps3d init failed:', reason, cfg.runtime || {});
    fallbackEl.textContent = '3D globe unavailable: GOOGLE_MAPS_API_KEY missing in app runtime.';
    fallbackEl.classList.remove('hidden');
    return { ok: false, reason, keyPresent: false, maps3dReady: false };
  }

  try {
    await bootstrapMaps({ key });
  } catch (err) {
    const reason = 'bootstrap_loader_failed';
    console.error('[gfs globe] maps bootstrap failed:', reason, err);
    fallbackEl.textContent = '3D globe unavailable: Maps bootstrap failed (check network/key restrictions).';
    fallbackEl.classList.remove('hidden');
    return { ok: false, reason, keyPresent: true, maps3dReady: false };
  }

  try {
    await window.google.maps.importLibrary('maps3d');
    try {
      await window.google.maps.importLibrary('marker');
    } catch (markerErr) {
      console.warn('[gfs globe] marker library unavailable; falling back to SVG marker content', markerErr);
    }
  } catch (err) {
    const reason = 'maps3d_library_rejected';
    console.error('[gfs globe] maps3d import failed:', reason, err);
    fallbackEl.textContent = '3D globe unavailable: maps3d library rejected (key restrictions or API enablement).';
    fallbackEl.classList.remove('hidden');
    return { ok: false, reason, keyPresent: true, maps3dReady: false };
  }

  fallbackEl.classList.add('hidden');
  return { ok: true, reason: 'ready', keyPresent: true, maps3dReady: true };
}

export async function libs() {
  const maps3d = await google.maps.importLibrary('maps3d');
  try {
    await google.maps.importLibrary('marker');
  } catch (err) {
    console.warn('[gfs globe] marker library import skipped', err);
  }
  return { maps3d };
}
