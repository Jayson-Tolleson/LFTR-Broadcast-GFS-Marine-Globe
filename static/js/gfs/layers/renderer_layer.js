
const DRAWABLE_REASONS = new Set(['boot', 'steady', 'settled', 'refresh', 'manual', 'cache_update', 'tile_update', 'bait-deferred', 'clouds-deferred', 'boats-deferred', 'oceanPoints-deferred']);
const HOLD_REASONS = new Set(['camera_move', 'camera_moving', 'orbit', 'orbit_move', 'pan', 'tilt', 'range', 'heading']);

function normalizeReason(reason) {
  return String(reason || 'steady').toLowerCase();
}

function isDrawableReason(reason) {
  const r = normalizeReason(reason);
  return DRAWABLE_REASONS.has(r) || r.endsWith('_settled') || r.includes('settled') || r.includes('steady') || r.includes('update');
}

function isHoldReason(reason) {
  const r = normalizeReason(reason);
  return HOLD_REASONS.has(r) || r.includes('camera_move') || r.includes('moving');
}

function defaultSignature(frame, payload) {
  const explicitVersion = payload?.__gfsRenderVersion || payload?.version || payload?.cache?.version || payload?.cache_quality?.version || payload?.tile_version || payload?.cache?.key || payload?.__gfsCacheMeta?.version || payload?.__gfsCacheMeta?.cache_key || '';
  if (explicitVersion) return String(explicitVersion);
  const bbox = Array.isArray(payload?.bbox) ? payload.bbox.join(',') : (Array.isArray(frame?.bbox) ? frame.bbox.join(',') : '');
  const valid = payload?.valid_time || frame?.valid_time || '';
  const source = payload?.source_time || frame?.source_time || '';
  const resolved = payload?.resolved_time || frame?.resolved_time || '';
  const hint = [
    Array.isArray(payload?.cloud_layers) ? payload.cloud_layers.length : 0,
    Array.isArray(payload?.items) ? payload.items.length : 0,
    Array.isArray(payload?.features) ? payload.features.length : 0,
    Array.isArray(payload?.polygon_field_v1?.features) ? payload.polygon_field_v1.features.length : 0,
    payload?.sea_feature_count || 0,
    payload?.sea_resolution?.subgrid_used || '',
    Array.isArray(payload?.precip_columns) ? payload.precip_columns.length : 0,
    Array.isArray(payload?.scene?.clouds) ? payload.scene.clouds.length : 0,
    Object.keys(payload?.fields || {}).join(','),
    Array.isArray(payload?.front_lines) ? payload.front_lines.length : 0,
    Array.isArray(payload?.bait?.polygons) ? payload.bait.polygons.length : 0,
    Array.isArray(payload?.bait?.outer_polygons) ? payload.bait.outer_polygons.length : 0,
    Array.isArray(payload?.bait?.inner_polygons) ? payload.bait.inner_polygons.length : 0,
    Array.isArray(payload?.bait?.core_polygons) ? payload.bait.core_polygons.length : 0,
    Array.isArray(payload?.bait_score) ? payload.bait_score.length : 0,
    Array.isArray(payload?.oceanPoints?.points) ? payload.oceanPoints.points.length : 0,
    payload?.oceanPoints?.source_time || payload?.oceanPoints?.valid_time || '',
    Array.isArray(payload?.boats) ? payload.boats.length : 0,
    Array.isArray(payload?.polygons?.boater) ? payload.polygons.boater.length : 0,
    Array.isArray(payload?.current_polygons) ? payload.current_polygons.length : 0,
  ].join(':');
  return `${bbox}|${valid}|${source}|${resolved}|${hint}`;
}

export class RendererLayer {
  constructor(map3DElement, { name, selector, renderer, signature, clearBeforeRender = true, preserveOnEmpty = false, minRenderIntervalMs = 0 }) {
    this.map = map3DElement;
    this.name = name;
    this.selector = selector;
    this.renderer = renderer;
    this.signature = signature || defaultSignature;
    this.visible = false;
    this.currentFrame = null;
    this.currentDisposer = null;
    this.lastSignature = '';
    this.clearBeforeRender = clearBeforeRender !== false;
    this.preserveOnEmpty = preserveOnEmpty === true;
    this.minRenderIntervalMs = Math.max(0, Number(minRenderIntervalMs || 0));
    this.lastRenderAt = 0;
    this.stats = { draws: 0, skips: 0, preserves: 0, clears: 0 };
  }

  show() {
    this.visible = true;
    if (this.currentFrame) this.onData(this.currentFrame);
  }

  hide() {
    this.visible = false;
    this.clear();
  }

  clear() {
    this.lastSignature = '';
    this.stats.clears += 1;
    // Belt-and-suspenders cleanup: renderer disposers should remove their own
    // elements, but Google Maps 3D/custom element constructors can leave nodes
    // behind if an async batch is still injecting. Remove any node tagged for
    // this layer so a double-click pill OFF always clears the globe.
    try {
      if (this.map && this.name) {
        this.map.querySelectorAll?.(`[data-gfs-layer="${this.name}"]`)?.forEach((el) => {
          try { el.remove(); } catch (_) {}
        });
      }
    } catch (_) {}
    if (typeof this.currentDisposer === 'function') {
      try { this.currentDisposer(); } catch (err) { console.warn('[gfs renderer layer] disposer failed', this.name, err); }
    }
    this.currentDisposer = null;
  }

  onData(frame) {
    this.currentFrame = frame || null;
    if (!this.visible || !this.map || !frame) return;
    const viewportReason = frame?.render_reason || frame?.meta?.render_reason || 'steady';
    const payload = this.selector?.(frame) || null;
    if (!payload) {
      // During camera movement keep the last known visible layer. A moving camera
      // should prioritize/fetch data, not clear bait/rain/current/clouds before
      // the settled replacement payload exists.  Cloud frames may also arrive as
      // cache/status-only shells while live GFS is in flight; preserving the last
      // drawable cloud scene prevents the half-blank flashing behavior.
      if ((isHoldReason(viewportReason) || this.preserveOnEmpty) && this.currentDisposer) {
        this.stats.preserves += 1;
        try { window.__gfsDebugEvent?.('render/preserve-empty-payload', { layer: this.name, reason: viewportReason, contract: 'preserve_last_good_clear_only_on_pill_off' }); } catch (_) {}
        return;
      }
      this.clear();
      return;
    }
    if (!isDrawableReason(viewportReason)) {
      if (this.currentDisposer) return;
    }
    const nextSignature = String(this.signature(frame, payload));
    if (nextSignature && nextSignature === this.lastSignature) {
      this.stats.skips += 1;
      try { window.__gfsDebugEvent?.('render/noop-same-version', { layer: this.name, signature: nextSignature, layerVersion: payload?.__gfsRenderVersion || null }); } catch (_) {}
      return;
    }
    if (this.minRenderIntervalMs > 0 && this.currentDisposer && this.lastRenderAt > 0) {
      const elapsed = Date.now() - this.lastRenderAt;
      const reasonText = String(viewportReason || '').toLowerCase();
      const force = reasonText.includes('pill_') || reasonText.includes('manual') || reasonText.includes('boot');
      if (!force && elapsed < this.minRenderIntervalMs) {
        try { window.__gfsDebugEvent?.('render/hold-min-interval', { layer: this.name, elapsed_ms: elapsed, min_ms: this.minRenderIntervalMs, next_signature: nextSignature }); } catch (_) {}
        return;
      }
    }
    if (this.clearBeforeRender) {
      this.clear();
      this.lastSignature = nextSignature;
      this.lastRenderAt = Date.now();
      this.stats.draws += 1;
      try {
        this.currentDisposer = this.renderer?.({
          frame,
          payload,
          map3DElement: this.map,
          viewportReason: isDrawableReason(viewportReason) ? viewportReason : 'steady',
          layerName: this.name,
        }) || null;
      } catch (err) {
        console.error('[gfs renderer layer] render failed', this.name, err);
        this.currentDisposer = null;
      }
      return;
    }

    // Some layers, especially clouds, receive warming/queued frames while the
    // real drawable payload is still being prepared. For those layers, render
    // first and only replace the disposer when the renderer says it actually
    // drew a new scene. This prevents blank/flash cycles during warm loads.
    const previousDisposer = this.currentDisposer;
    let nextDisposer = null;
    try {
      nextDisposer = this.renderer?.({
        frame,
        payload,
        map3DElement: this.map,
        viewportReason: isDrawableReason(viewportReason) ? viewportReason : 'steady',
        layerName: this.name,
      }) || null;
    } catch (err) {
      console.error('[gfs renderer layer] render failed', this.name, err);
      return;
    }
    if (nextDisposer && nextDisposer.__gfsKeepExisting === true) {
      // Persistent renderers reconcile their own objects in-place. Replace the
      // disposer reference without firing the previous one; otherwise a refresh
      // can remove the just-reconciled scene. Pill-off still calls the latest
      // disposer and clears the whole layer.
      this.currentDisposer = nextDisposer;
      this.lastSignature = nextSignature;
      this.lastRenderAt = Date.now();
      this.stats.draws += 1;
      return;
    }
    if (nextDisposer && nextDisposer.__gfsDidRender === true) {
      if (typeof previousDisposer === 'function') {
        try { previousDisposer(); } catch (err) { console.warn('[gfs renderer layer] previous disposer failed', this.name, err); }
      }
      this.currentDisposer = nextDisposer;
      this.lastSignature = nextSignature;
      this.lastRenderAt = Date.now();
      this.stats.draws += 1;
    }
  }

  update() {}
}
