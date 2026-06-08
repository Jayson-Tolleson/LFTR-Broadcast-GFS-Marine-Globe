// Hover intel has been intentionally disabled globally.
// Kept as a compatibility module so existing imports do not break.
// This removes cursor hover panels, title-bar hover intel, and browser title tooltips
// from drawn GFS polygons, polylines, markers, cloud shells, bait zones, boats, rain,
// lightning, inland water, and current graphics.

let observer = null;
let scrubTimer = null;

const HOVER_SELECTOR = [
  'gmp-polygon-3d',
  'gmp-polyline-3d',
  'gmp-marker-3d',
  '[data-gfs-layer]',
  '[data-gfs-hover-attached]',
  '[data-gfs-hover-payload]',
  '[data-current-band]',
  '[data-cloud-shell]',
  '[data-lightning-region]',
  '[data-inland-water-kind]'
].join(',');

function safeRemoveAttr(el, name) {
  try { el?.removeAttribute?.(name); } catch (_) {}
}

function scrubElement(el) {
  if (!el) return el;
  try {
    el.__gfsHover = null;
    el.__gfsHoverAttached = false;
    if (el.style) el.style.cursor = '';
    safeRemoveAttr(el, 'title');
    safeRemoveAttr(el, 'aria-label');
    safeRemoveAttr(el, 'data-gfs-hover-title');
    safeRemoveAttr(el, 'data-gfs-hover-detail');
    safeRemoveAttr(el, 'data-gfs-hover-payload');
    safeRemoveAttr(el, 'data-gfs-hover-attached');
    if (el.dataset) {
      delete el.dataset.gfsHoverTitle;
      delete el.dataset.gfsHoverDetail;
      delete el.dataset.gfsHoverPayload;
      delete el.dataset.gfsHoverAttached;
    }
  } catch (_) {}
  return el;
}

function scrubTree(root = document) {
  try {
    root.querySelectorAll?.(HOVER_SELECTOR)?.forEach(scrubElement);
  } catch (_) {}
  try {
    const tip = document.getElementById('gfsPolygonHoverTip');
    tip?.remove?.();
  } catch (_) {}
}

function scheduleScrub(root = document) {
  if (scrubTimer) return;
  scrubTimer = setTimeout(() => {
    scrubTimer = null;
    scrubTree(root);
  }, 80);
}

export function isPolygonHoverActive() { return false; }
export function setTitleHoverHud() { return ''; }
export function hidePolygonHoverTip() {}
export function describeLayerPayload(el) { return scrubElement(el); }
export function attachPolygonHover(el) { return scrubElement(el); }

export function installUniversalPolygonHover(root = document) {
  scrubTree(root);
  if (observer) return;
  try {
    observer = new MutationObserver((records) => {
      for (const rec of records || []) {
        for (const node of Array.from(rec.addedNodes || [])) {
          if (!node || node.nodeType !== 1) continue;
          if (node.matches?.(HOVER_SELECTOR)) scrubElement(node);
          node.querySelectorAll?.(HOVER_SELECTOR)?.forEach(scrubElement);
        }
      }
      scheduleScrub(root);
    });
    observer.observe(root === document ? document.body : root, { childList: true, subtree: true, attributes: true, attributeFilter: ['title', 'aria-label', 'data-gfs-hover-payload', 'data-gfs-hover-title', 'data-gfs-hover-attached'] });
    window.__gfsUniversalPolygonHoverObserver = observer;
    window.__gfsHoverIntelDisabled = true;
  } catch (_) {}
}

try {
  window.__gfsHoverIntelDisabled = true;
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => installUniversalPolygonHover(document));
  else installUniversalPolygonHover(document);
} catch (_) {}
