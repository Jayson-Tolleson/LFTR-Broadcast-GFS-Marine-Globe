const MARKER_DEBUG = Boolean(window.__GFS_DEBUG);

function resolveProbability(loc) {
  const value = Number(loc?.probability ?? loc?.confidence ?? 1);
  return Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : 1;
}

function parseCoordinate(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function hashPhaseSeed(input) {
  const text = String(input || 'fish-orb');
  let h = 2166136261;
  for (let i = 0; i < text.length; i += 1) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return ((h >>> 0) % 1000) / 1000;
}

function greenForProbability(probability) {
  if (probability >= 0.66) return { r: 57, g: 255, b: 120 };
  if (probability >= 0.33) return { r: 88, g: 245, b: 146 };
  return { r: 67, g: 217, b: 122 };
}

function pulseEnvelope(probability, phase, seconds) {
  const omega = Math.PI * 1.2;
  const smooth = (Math.sin(seconds * omega + phase) + 1) * 0.5;

  if (probability >= 0.66) {
    const crest = Math.pow(smooth, 5) * 0.26;
    const snap = Math.exp(-Math.pow((smooth - 0.97) / 0.055, 2)) * 0.32;
    return 0.34 + (smooth * 0.46) + crest + snap;
  }
  if (probability >= 0.33) {
    const crest = Math.pow(smooth, 3) * 0.2;
    return 0.3 + (smooth * 0.5) + crest;
  }
  return 0.28 + (smooth * 0.44);
}


function pinStyle(probability, glow) {
  const c = greenForProbability(probability);
  const alpha = Math.min(0.95, 0.34 + glow * 0.55);
  return {
    scale: Math.min(2.6, 1.15 + probability * 0.75 + glow * 0.45),
    background: `rgba(${c.r},${c.g},${c.b},${alpha.toFixed(3)})`,
    borderColor: 'rgba(220,252,231,0.95)',
    glyphColor: 'rgba(236,255,241,0.95)',
  };
}

function createFallbackTemplateOrb(probability) {
  const c = greenForProbability(probability);
  const uid = `orb-${Math.random().toString(36).slice(2, 10)}`;
  const tpl = document.createElement('template');
  tpl.innerHTML = `
    <svg width="48" height="48" viewBox="0 0 44 44" xmlns="http://www.w3.org/2000/svg" aria-hidden="true" style="pointer-events:none">
      <defs>
        <radialGradient id="${uid}-core" cx="30%" cy="28%" r="70%">
          <stop offset="0%" stop-color="#dcffe9"/>
          <stop offset="42%" stop-color="rgb(${c.r},${c.g},${c.b})"/>
          <stop offset="100%" stop-color="#0a3c22"/>
        </radialGradient>
        <radialGradient id="${uid}-halo" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stop-color="rgb(${c.r},${c.g},${c.b})" stop-opacity="0.62"/>
          <stop offset="100%" stop-color="rgb(${c.r},${c.g},${c.b})" stop-opacity="0"/>
        </radialGradient>
      </defs>
      <circle data-role="halo" cx="22" cy="22" r="19" fill="url(#${uid}-halo)"/>
      <circle data-role="core" cx="22" cy="22" r="12.5" fill="url(#${uid}-core)"/>
      <ellipse data-role="highlight" cx="17.5" cy="15" rx="4.5" ry="2.6" fill="white" fill-opacity="0.82" transform="rotate(-24 17.5 15)"/>
    </svg>`;
  return {
    kind: 'template',
    content: tpl,
    core: tpl.content.querySelector('[data-role="core"]'),
    halo: tpl.content.querySelector('[data-role="halo"]'),
    highlight: tpl.content.querySelector('[data-role="highlight"]'),
  };
}

function createOrbContent(probability) {
  // Use the original LFTR fish-orb visual instead of Google PinElement.
  // PinElement renders like a flat 2D map pin; the globe HUD expects glowing
  // orb anchors that match the CSV fish intelligence locations.
  return createFallbackTemplateOrb(probability);
}

function createFishMarker({ maps3d, loc, altitudeMode }) {
  const probability = resolveProbability(loc);
  const position = { lat: loc.lat, lng: loc.lon, altitude: 18 };
  let marker = null;

  if (maps3d?.Marker3DInteractiveElement) {
    marker = new maps3d.Marker3DInteractiveElement({
      position,
      altitudeMode,
      title: loc.name || loc.title || 'Fishing location',
      drawsWhenOccluded: false,
      sizePreserved: true,
      extruded: false,
    });
  } else {
    marker = document.createElement('gmp-marker-3d-interactive');
    marker.position = position;
    marker.altitudeMode = altitudeMode;
    marker.title = loc.name || loc.title || 'Fishing location';
    marker.drawsWhenOccluded = false;
    marker.sizePreserved = true;
  }

  const orb = createOrbContent(probability);
  marker.append(orb.content);

  if (MARKER_DEBUG) {
    console.debug('[gfs markers] created marker orb', { id: loc?.id || loc?.location_key, probability, kind: orb.kind });
  }

  return { marker, probability, orb };
}

function startPulseLoop(animatedOrbs) {
  let frameId = null;
  const tick = (now) => {
    const seconds = now * 0.001;
    for (const orb of animatedOrbs) {
      const glow = pulseEnvelope(orb.probability, orb.phase, seconds);
      if (orb.kind === 'pin' && orb.pin) {
        const style = pinStyle(orb.probability, glow);
        try {
          orb.pin.scale = style.scale;
          orb.pin.background = style.background;
          orb.pin.borderColor = style.borderColor;
          orb.pin.glyphColor = style.glyphColor;
        } catch (_) {}
        continue;
      }
      if (!orb?.core || !orb?.halo || !orb?.highlight) continue;
      const coreOpacity = Math.min(1, 0.5 + glow * 0.55);
      const haloOpacity = Math.min(1, 0.22 + glow * 0.7);
      const highlightOpacity = Math.min(1, 0.34 + glow * 0.55);
      orb.core.setAttribute('fill-opacity', coreOpacity.toFixed(3));
      orb.halo.setAttribute('fill-opacity', haloOpacity.toFixed(3));
      orb.highlight.setAttribute('fill-opacity', highlightOpacity.toFixed(3));
    }
    frameId = requestAnimationFrame(tick);
  };
  frameId = requestAnimationFrame(tick);
  return () => {
    if (frameId) cancelAnimationFrame(frameId);
  };
}

export function renderMarkers({ locations, globeEl, maps3d, onSelect }) {
  const active = [];
  const animatedOrbs = [];
  const altitudeMode = maps3d?.AltitudeMode?.RELATIVE_TO_GROUND || 'RELATIVE_TO_GROUND';

  for (const loc of locations) {
    const lat = parseCoordinate(loc?.lat);
    const lon = parseCoordinate(loc?.lon);
    if (lat === null || lon === null) {
      console.warn('[gfs markers] skipped invalid location coordinates', { id: loc?.id, lat: loc?.lat, lon: loc?.lon });
      continue;
    }

    const normalizedLoc = { ...loc, lat, lon };
    let built;
    try {
      built = createFishMarker({ maps3d, loc: normalizedLoc, altitudeMode });
    } catch (err) {
      console.error('[gfs markers] failed creating marker', { id: loc?.id, error: String(err) });
      continue;
    }

    const click = () => {
      if (MARKER_DEBUG) {
        console.debug('[gfs markers] click -> HUD open', { id: normalizedLoc?.id });
      }
      onSelect(normalizedLoc);
    };

    try { built.marker?.setAttribute?.('data-gfs-layer', 'locations'); } catch (_) {}
    built.marker.addEventListener('gmp-click', click);
    built.marker.addEventListener('click', click);

    globeEl.append(built.marker);
    active.push(built.marker);

    const phaseSeed = `${normalizedLoc.id || normalizedLoc.name || ''}:${lat.toFixed(4)}:${lon.toFixed(4)}`;
    animatedOrbs.push({
      kind: built.orb.kind,
      pin: built.orb.pin,
      probability: built.probability,
      phase: hashPhaseSeed(phaseSeed) * Math.PI * 2,
      core: built.orb.core,
      halo: built.orb.halo,
      highlight: built.orb.highlight,
    });
  }

  console.info('[gfs markers] rendered fish beacons', { count: active.length, inputCount: Array.isArray(locations) ? locations.length : 0 });
  const stopPulse = startPulseLoop(animatedOrbs);

  return () => {
    stopPulse();
    active.forEach((marker) => {
      try { marker.remove(); } catch (_) {}
    });
  };
}
