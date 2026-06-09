export function degToRad(latDeg, lonDeg) {
  const φ = Number(latDeg) * Math.PI / 180;
  const λ = Number(lonDeg) * Math.PI / 180;
  return { φ, λ };
}


export function wrappedLongitude(lambdaRad) {
  const λ = Number(lambdaRad);
  return ((λ + Math.PI) % (2 * Math.PI) + 2 * Math.PI) % (2 * Math.PI) - Math.PI;
}

export function bearingFromUV(u, v) {
  const θ = Math.atan2(Number(u) || 0, Number(v) || 0);
  return θ;
}

export function cellOffsets(cellSizeDeg) {
  const Δφ = Number(cellSizeDeg) * Math.PI / 180;
  const Δλ = Number(cellSizeDeg) * Math.PI / 180;
  return { Δφ, Δλ };
}

export function clamp01(value) {
  const α = Number(value);
  if (!Number.isFinite(α)) return 0;
  if (α < 0) return 0;
  if (α > 1) return 1;
  return α;
}
