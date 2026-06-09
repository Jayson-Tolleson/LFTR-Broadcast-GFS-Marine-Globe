import { attachPolygonHover } from './hover_tip.js';

const NEON_GLOW_ENABLED = true;

function maps3dApi() {
  return window.google?.maps?.maps3d || null;
}

function finitePoint(p, altitude) {
  let lat;
  let lng;
  let alt;
  if (Array.isArray(p)) {
    const a = Number(p[0]);
    const b = Number(p[1]);
    if (!Number.isFinite(a) || !Number.isFinite(b)) return null;
    // Accept both [lng, lat] backend rings and [lat, lng] frontend rings.
    if (Math.abs(a) <= 90 && Math.abs(b) <= 180 && Math.abs(b) > 90) {
      lat = a; lng = b;
    } else {
      lng = a; lat = b;
    }
    alt = Number(p[2]);
  } else {
    lat = Number(p?.lat ?? p?.latitude);
    lng = Number(p?.lng ?? p?.lon ?? p?.longitude);
    alt = Number(p?.altitude ?? p?.altitude_m ?? p?.alt ?? p?.z);
  }
  if (!Number.isFinite(lat) || !Number.isFinite(lng)) return null;
  while (lng < -180) lng += 360;
  while (lng >= 180) lng -= 360;
  if (lat < -90 || lat > 90 || lng < -180 || lng >= 180) return null;
  const fallbackAltitude = Number.isFinite(Number(altitude)) ? Number(altitude) : 0;
  return { lat, lng, altitude: Number.isFinite(alt) ? alt : fallbackAltitude };
}

function toLngLatAlt(path, altitude) {
  const out = [];
  for (const point of Array.isArray(path) ? path : []) {
    const p = finitePoint(point, altitude);
    if (!p) continue;
    const prev = out[out.length - 1];
    if (prev && Math.abs(prev.lat - p.lat) < 1e-8 && Math.abs(prev.lng - p.lng) < 1e-8) continue;
    out.push(p);
  }
  if (out.length >= 2) {
    const first = out[0];
    const last = out[out.length - 1];
    if (Math.abs(first.lat - last.lat) < 1e-8 && Math.abs(first.lng - last.lng) < 1e-8) out.pop();
  }
  return out;
}


function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function parseHexColor(color) {
  const value = String(color || '').trim();
  const match = value.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i);
  if (!match) return null;
  let hex = match[1];
  if (hex.length === 3) hex = hex.split('').map((c) => c + c).join('');
  const intVal = Number.parseInt(hex, 16);
  if (!Number.isFinite(intVal)) return null;
  return { r: (intVal >> 16) & 255, g: (intVal >> 8) & 255, b: intVal & 255 };
}

function rgbToHex(rgb) {
  const toHex = (v) => clamp(Math.round(Number(v) || 0), 0, 255).toString(16).padStart(2, '0');
  return `#${toHex(rgb.r)}${toHex(rgb.g)}${toHex(rgb.b)}`;
}

function cssColorWithAlpha(color, opacity = 1) {
  const alpha = clamp(Number(opacity), 0, 1);
  const value = String(color || '').trim();
  const rgba = value.match(/^rgba?\(([^)]+)\)$/i);
  if (rgba) {
    const parts = rgba[1].split(',').map((s) => s.trim());
    if (parts.length >= 3) return `rgba(${parts[0]},${parts[1]},${parts[2]},${alpha.toFixed(3)})`;
  }
  const rgb = parseHexColor(value);
  if (rgb) return `rgba(${rgb.r},${rgb.g},${rgb.b},${alpha.toFixed(3)})`;
  // CSS named colors are allowed by Maps; keep them rather than guessing.
  return value || '#ffffff';
}

function setBooleanAttrSafe(target, attr, enabled) {
  try {
    if (enabled) target?.setAttribute?.(attr, '');
    else target?.removeAttribute?.(attr);
    return true;
  } catch (_) { return false; }
}

function mixRgb(a, b, t) {
  return {
    r: a.r + (b.r - a.r) * t,
    g: a.g + (b.g - a.g) * t,
    b: a.b + (b.b - a.b) * t,
  };
}

function neonStrokeColor(baseColor, fallback = '#7ee7ff') {
  const rgb = parseHexColor(baseColor) || parseHexColor(fallback);
  if (!rgb) return fallback;
  return rgbToHex(mixRgb(rgb, { r: 255, g: 255, b: 255 }, 0.38));
}

function neonizePolygonStyle(style = {}) {
  if (style.neonGlow === false || NEON_GLOW_ENABLED === false) return style;
  const fillColor = style.fillColor || '#ffffff';
  const baseStroke = style.strokeColor || fillColor;
  const fillOpacity = Number(style.fillOpacity);
  const strokeOpacity = Number(style.strokeOpacity);
  const strokeWidth = Number(style.strokeWidth);
  const neonColor = neonStrokeColor(baseStroke, fillColor);
  return {
    ...style,
    strokeColor: neonColor,
    strokeOpacity: clamp(Number.isFinite(strokeOpacity) ? Math.max(strokeOpacity, 0.96) : 0.98, 0, 1),
    strokeWidth: clamp((Number.isFinite(strokeWidth) ? strokeWidth : 1) * 2.9 + 0.9, 2.4, 8.5),
    fillOpacity: Number.isFinite(fillOpacity) ? fillOpacity : 0.3,
    glowColor: neonColor,
  };
}

function pathAttrString(coords) {
  // Current gmp-polygon-3d custom element docs use a coordinate string:
  //   path="lat,lng,alt lat,lng,alt ..."
  // Never write JSON here. The JSON string form is the exact source of:
  //   Could not interpret "[{\"lat\":...}]" as a LatLngAltitude
  return (Array.isArray(coords) ? coords : [])
    .map((p) => `${Number(p.lat).toFixed(8)},${Number(p.lng).toFixed(8)},${Number(p.altitude || 0).toFixed(2)}`)
    .join(' ');
}

function setSafe(target, prop, value) {
  try { target[prop] = value; return true; } catch (_) { return false; }
}

function setAttrSafe(target, attr, value) {
  if (value == null) return false;
  try { target?.setAttribute?.(attr, String(value)); return true; } catch (_) { return false; }
}

function stylePolygon(el, style) {
  const resolved = neonizePolygonStyle(style);
  const fillOpacity = Number.isFinite(Number(resolved.fillOpacity)) ? clamp(Number(resolved.fillOpacity), 0, 1) : 1;
  const strokeOpacity = Number.isFinite(Number(resolved.strokeOpacity)) ? clamp(Number(resolved.strokeOpacity), 0, 1) : 1;
  const fillColor = cssColorWithAlpha(resolved.fillColor, fillOpacity);
  const strokeColor = cssColorWithAlpha(resolved.strokeColor, strokeOpacity);
  const requestedHeight = Number(resolved.extrudedHeight);
  const shouldExtrude = resolved.extrudeToGround === true || (resolved.extrudeToGround !== false && Number.isFinite(requestedHeight) && requestedHeight > 0);
  // Maps 3D Polygon3DElement supports `extruded` as a boolean that connects the
  // polygon to the ground. It does not support finite `extrudedHeight`; keep the
  // requested height as debug metadata only.
  setSafe(el, 'fillColor', fillColor);
  setSafe(el, 'strokeColor', strokeColor);
  setSafe(el, 'strokeWidth', resolved.strokeWidth);
  setSafe(el, 'extruded', shouldExtrude);

  setAttrSafe(el, 'fill-color', fillColor);
  setAttrSafe(el, 'stroke-color', strokeColor);
  setAttrSafe(el, 'stroke-width', resolved.strokeWidth);
  setBooleanAttrSafe(el, 'extruded', shouldExtrude);
  if (Number.isFinite(requestedHeight) && requestedHeight > 0) setAttrSafe(el, 'data-gfs-requested-extruded-height-m', requestedHeight.toFixed(1));
  else { try { el.removeAttribute?.('data-gfs-requested-extruded-height-m'); } catch (_) {} }
  if (style.neonGlow === false || NEON_GLOW_ENABLED === false) {
    setAttrSafe(el, 'data-gfs-neon-edge', 'false');
  } else {
    setAttrSafe(el, 'data-gfs-neon-edge', 'true');
    setAttrSafe(el, 'data-gfs-neon-color', resolved.glowColor || resolved.strokeColor || resolved.fillColor || '#ffffff');
  }
}


function altitudeModeValue(altitudeMode) {
  return altitudeMode === 'absolute' ? 'absolute' : 'relative-to-ground';
}

function createAttributePolygon({ coords, altitudeMode, fillColor, fillOpacity, strokeColor, strokeOpacity, strokeWidth, extrudedHeight, extrudeToGround = null, neonGlow = true }) {
  const attrPath = pathAttrString(coords);
  if (!attrPath || attrPath.includes('[object Object]') || attrPath.includes('[{')) return null;
  const el = document.createElement('gmp-polygon-3d');
  // Important: use the documented coordinate-string attribute, not JSON and not
  // a post-append object-array property. This avoids Maps 3D beta reflecting an
  // object array into a JSON path attribute.
  setAttrSafe(el, 'path', attrPath);
  setAttrSafe(el, 'altitude-mode', altitudeModeValue(altitudeMode));
  stylePolygon(el, { fillColor, fillOpacity, strokeColor, strokeOpacity, strokeWidth, extrudedHeight, extrudeToGround, neonGlow });
  try {
    el.__gfsPathMode = 'coordinate-string-attribute';
    el.__gfsPathPointCount = coords.length;
    el.setAttribute('data-gfs-path-mode', 'coordinate-string-attribute');
    el.setAttribute('data-gfs-path-points', String(coords.length));
  } catch (_) {}
  return el;
}

function createClassPolygon({ coords, altitudeMode, fillColor, fillOpacity, strokeColor, strokeOpacity, strokeWidth, extrudedHeight, extrudeToGround = null, neonGlow = true }) {
  const maps3d = maps3dApi();
  if (!maps3d?.Polygon3DElement || !maps3d?.AltitudeMode) return null;
  const mode = altitudeMode === 'absolute' ? maps3d.AltitudeMode.ABSOLUTE : maps3d.AltitudeMode.RELATIVE_TO_GROUND;
  try {
    const polygon = new maps3d.Polygon3DElement({ altitudeMode: mode });
    try { polygon.removeAttribute?.('path'); } catch (_) {}
    // Use property only as a class fallback. Most layers should use the
    // coordinate-string custom-element path above because it is stable in the
    // beta custom-element parser.
    polygon.path = coords;
    stylePolygon(polygon, { fillColor, fillOpacity, strokeColor, strokeOpacity, strokeWidth, extrudedHeight, extrudeToGround, neonGlow });
    try {
      polygon.__gfsPathMode = 'class-array-property';
      polygon.__gfsPathPointCount = coords.length;
      polygon.setAttribute?.('data-gfs-path-mode', 'class-array-property');
      polygon.setAttribute?.('data-gfs-path-points', String(coords.length));
    } catch (_) {}
    return polygon;
  } catch (err) {
    console.warn('[gfs polygon3d] class polygon create failed', { message: err?.message || String(err), points: coords.length });
    return null;
  }
}

export function createPolygon3D({ path, altitude = 0, altitudeMode = 'relative', fillColor = '#ffffff', fillOpacity = 0.3, strokeColor = '#ffffff', strokeOpacity = 0.7, strokeWidth = 1, extrudedHeight = 0, extrudeToGround = null, hover = null, preferAttributePath = true, neonGlow = true }) {
  const coords = toLngLatAlt(path, altitude);
  if (coords.length < 3) return null;

  let polygon = null;
  // Stable path first: gmp-polygon-3d coordinate-string attributes have been
  // the most reliable Maps 3D beta contract for our generated rings. The class
  // property path is kept as a fallback because some beta builds reflect object
  // arrays into a JSON path attribute and then draw nothing.
  if (preferAttributePath !== false) {
    polygon = createAttributePolygon({ coords, altitudeMode, fillColor, fillOpacity, strokeColor, strokeOpacity, strokeWidth, extrudedHeight, extrudeToGround, neonGlow });
  }
  if (!polygon) {
    polygon = createClassPolygon({ coords, altitudeMode, fillColor, fillOpacity, strokeColor, strokeOpacity, strokeWidth, extrudedHeight, extrudeToGround, neonGlow });
  }
  if (!polygon) return null;
  return attachPolygonHover(polygon, hover || true);
}
