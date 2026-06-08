const inflight = new Map();
const abortControllers = new Map();

function warn(prefix, detail) {
  console.warn(`[gfs/api] ${prefix}`, detail || '');
}

function makeError(message, extra = {}) {
  const err = new Error(message);
  Object.assign(err, extra);
  return err;
}

function normalizeOptions(opts = {}) {
  if (opts instanceof AbortSignal) return { signal: opts };
  if (!opts || typeof opts !== 'object') return {};
  return opts;
}

function shouldAbortPreviousGet(url, options) {
  if (options?.abortPrevious === false) return false;
  return typeof url === 'string' && url.startsWith('/gfs/api/');
}

function gfsRequestKey(url) {
  // Keep the full path+query in the abort key so viewport-specific visual
  // requests do not cancel unrelated bbox pulls. This keeps fast cache hits
  // poppable while still deduping identical in-flight GETs.
  try {
    const u = new URL(url, window.location.origin);
    return `${u.pathname}${u.search}`;
  } catch (_) {
    return String(url || '');
  }
}

function requestKey(url, method = 'GET') {
  return `${String(method || 'GET').toUpperCase()} ${String(url)}`;
}

function shouldDedupe(method, dedupe) {
  return dedupe !== false && String(method || 'GET').toUpperCase() === 'GET';
}

function timeoutSignal(timeoutMs) {
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
    return { signal: undefined, cancel: () => {} };
  }
  const controller = new AbortController();
  const id = setTimeout(() => {
    try { controller.abort(); } catch (_) {}
  }, timeoutMs);
  return {
    signal: controller.signal,
    cancel: () => clearTimeout(id),
  };
}

function mergeSignals(signals) {
  const valid = signals.filter(Boolean);
  if (!valid.length) return { signal: undefined, cleanup: () => {} };
  if (valid.length === 1) return { signal: valid[0], cleanup: () => {} };

  const controller = new AbortController();
  const onAbort = () => {
    if (!controller.signal.aborted) {
      try { controller.abort(); } catch (_) {}
    }
  };

  for (const signal of valid) {
    if (signal.aborted) {
      try { controller.abort(); } catch (_) {}
      return { signal: controller.signal, cleanup: () => {} };
    }
    signal.addEventListener('abort', onAbort, { once: true });
  }

  return {
    signal: controller.signal,
    cleanup: () => {
      for (const signal of valid) {
        try { signal.removeEventListener('abort', onAbort); } catch (_) {}
      }
    },
  };
}

function buildGetSignal(url, options = {}) {
  const externalSignal = options?.signal || null;
  if (!shouldAbortPreviousGet(url, options)) {
    return { signal: externalSignal, controller: null, key: '' };
  }

  const key = gfsRequestKey(url);
  const prior = abortControllers.get(key);
  if (prior) {
    try { prior.abort(); } catch (_) {}
  }

  const controller = new AbortController();
  abortControllers.set(key, controller);

  const merged = mergeSignals([externalSignal, controller.signal]);
  return { signal: merged.signal, controller, key, cleanup: merged.cleanup };
}

async function parseJsonResponse(response, url) {
  const text = await response.text();
  if (!text || !text.trim()) {
    throw makeError('empty response body', { url, status: response.status, responseText: text });
  }
  try {
    return JSON.parse(text);
  } catch (err) {
    throw makeError(`json parse failed ${err?.message || err}`, {
      url,
      status: response.status,
      responseText: text.slice(0, 500),
    });
  }
}

export async function getJson(url, opts = {}) {
  const {
    signal: callerSignal,
    timeoutMs = 15000,
    dedupe = true,
    abortPrevious = true,
    fetchOptions = {},
    method = 'GET',
    fallback,
  } = normalizeOptions(opts);

  const upperMethod = String(fetchOptions.method || method || 'GET').toUpperCase();
  const dedupeKey = requestKey(url, upperMethod);
  if (shouldDedupe(upperMethod, dedupe) && inflight.has(dedupeKey)) {
    return inflight.get(dedupeKey);
  }

  const run = (async () => {
    const timeout = timeoutSignal(timeoutMs);
    const autoAbort = upperMethod === 'GET'
      ? buildGetSignal(url, { signal: callerSignal, abortPrevious })
      : { signal: callerSignal, controller: null, key: '', cleanup: () => {} };
    const merged = autoAbort.signal === callerSignal || !timeout.signal
      ? mergeSignals([autoAbort.signal, timeout.signal].filter(Boolean))
      : { signal: autoAbort.signal, cleanup: () => {} };

    try {
      const response = await fetch(url, {
        credentials: 'same-origin',
        ...fetchOptions,
        method: upperMethod,
        signal: merged.signal,
        headers: {
          Accept: 'application/json',
          ...(fetchOptions.headers || {}),
        },
      });

      if (!response.ok) {
        const text = await response.text().catch(() => '');
        throw makeError('GET non-ok', { url, status: response.status, responseText: text.slice(0, 500) });
      }

      return await parseJsonResponse(response, url);
    } catch (err) {
      const aborted = err?.name === 'AbortError' || merged.signal?.aborted || timeout.signal?.aborted || callerSignal?.aborted;
      if (aborted) {
        const abortErr = makeError('The operation was aborted.', { url, aborted: true });
        if (fallback !== undefined) return fallback;
        throw abortErr;
      }
      if (fallback !== undefined) {
        warn(err?.message || 'GET failure', { url, err: err?.message || err });
        return fallback;
      }
      throw err instanceof Error ? err : makeError(String(err), { url });
    } finally {
      timeout.cancel();
      try { merged.cleanup(); } catch (_) {}
      try { autoAbort.cleanup?.(); } catch (_) {}
      if (autoAbort.controller && autoAbort.key && abortControllers.get(autoAbort.key) === autoAbort.controller) {
        abortControllers.delete(autoAbort.key);
      }
    }
  })();

  if (shouldDedupe(upperMethod, dedupe)) {
    inflight.set(dedupeKey, run);
    run.finally(() => {
      if (inflight.get(dedupeKey) === run) inflight.delete(dedupeKey);
    });
  }

  return run;
}

export async function getSceneFrame({ bbox, visibleBbox, layers = [], mode = 'read', refresh = false, providerJobs = true, jobLimit = null, reason = 'scene_frame' } = {}, fallback = null, options = {}) {
  const params = new URLSearchParams();
  if (bbox) params.set('bbox', bbox);
  if (visibleBbox) params.set('visible_bbox', visibleBbox);
  if (layers?.length) params.set('layers', layers.join(','));
  params.set('mode', mode);
  params.set('refresh', refresh ? '1' : '0');
  params.set('provider_jobs', providerJobs ? '1' : '0');
  if (jobLimit !== null && jobLimit !== undefined) params.set('job_limit', String(jobLimit));
  if (reason) params.set('reason', reason);
  return getJsonSafe(`/gfs/api/scene-frame?${params.toString()}`, fallback, { abortPrevious: false, ...normalizeOptions(options) });
}

export async function getJsonSafe(url, fallback = null, options = {}) {
  return getJson(url, { ...normalizeOptions(options), fallback });
}

export async function postJsonSafe(url, payload = {}, fallback = null, options = {}) {
  try {
    return await getJson(url, {
      ...normalizeOptions(options),
      method: 'POST',
      dedupe: false,
      abortPrevious: false,
      fallback,
      fetchOptions: {
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(payload || {}),
      },
    });
  } catch (err) {
    warn('POST network failure', { url, err: err?.message || err });
    return fallback;
  }
}

export async function uploadSafe(url, file, fields = {}, fallback = null, options = {}) {
  try {
    const fd = new FormData();
    Object.entries(fields || {}).forEach(([k, v]) => fd.append(k, v));
    fd.append('file', file);
    return await getJson(url, {
      ...normalizeOptions(options),
      method: 'POST',
      dedupe: false,
      abortPrevious: false,
      fallback,
      fetchOptions: {
        body: fd,
      },
    });
  } catch (err) {
    warn('UPLOAD network failure', { url, err: err?.message || err });
    return fallback;
  }
}

export async function jget(url, opts = {}) {
  const normalized = normalizeOptions(opts);
  if (normalized && Object.keys(normalized).length && !('fallback' in normalized)) {
    return getJson(url, normalized);
  }
  return getJsonSafe(url, null, normalized);
}

export const jpost = postJsonSafe;
export const upload = uploadSafe;
