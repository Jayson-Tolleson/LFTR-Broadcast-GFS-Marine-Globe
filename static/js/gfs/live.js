import { postJsonSafe } from './api.js';

let mediaStream = null;
let recorder = null;
let chunks = [];

export async function startLive({ locationId, videoEl, overlayEl }) {
  mediaStream = await navigator.mediaDevices.getUserMedia({ video: true, audio: true });
  if (videoEl) videoEl.srcObject = mediaStream;
  if (overlayEl) overlayEl.classList.remove('hidden');
  recorder = new MediaRecorder(mediaStream, { mimeType: 'video/webm' });
  chunks = [];
  recorder.ondataavailable = (e) => { if (e.data?.size) chunks.push(e.data); };
  recorder.start(1000);
  await postJsonSafe(`/gfs/api/location/${encodeURIComponent(locationId)}/live`, { active: true, stream_url: 'webrtc-local-capture' }, null);
}

export async function stopLive({ locationId, onBlob }) {
  if (recorder && recorder.state !== 'inactive') {
    await new Promise((resolve) => {
      recorder.onstop = resolve;
      recorder.stop();
    });
  }
  if (mediaStream) mediaStream.getTracks().forEach((t) => t.stop());
  mediaStream = null;
  const blob = chunks.length ? new Blob(chunks, { type: 'video/webm' }) : null;
  chunks = [];
  await postJsonSafe(`/gfs/api/location/${encodeURIComponent(locationId)}/live`, { active: false, stream_url: '' }, null);
  if (blob && onBlob) onBlob(blob);
}
