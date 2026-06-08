import { getJsonSafe } from './api.js';

export async function loadLocationVideos(locationId) {
  const payload = await getJsonSafe(`/gfs/api/location/${encodeURIComponent(locationId)}/videos`, { videos: [] });
  return payload?.videos || [];
}

export function renderVideoFrame(container, videos) {
  container.innerHTML = '';
  if (!videos.length) {
    container.textContent = 'No video yet for this location.';
    return;
  }
  const v = document.createElement('video');
  v.controls = true;
  v.loop = true;
  v.autoplay = true;
  v.muted = true;
  v.playsInline = true;
  v.src = videos[0].url;
  container.appendChild(v);
}
