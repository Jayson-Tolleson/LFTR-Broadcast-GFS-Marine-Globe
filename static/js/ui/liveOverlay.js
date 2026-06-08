let onCloseHandler = null;

function ensureOverlayStyles() {
  if (document.getElementById('liveOverlayStyles')) return;
  const style = document.createElement('style');
  style.id = 'liveOverlayStyles';
  style.textContent = `
.live-overlay {
  position: fixed;
  bottom: 20px;
  right: 20px;
  width: 420px;
  max-width: min(420px, calc(100vw - 24px));
  z-index: 9999;
  border: 1px solid rgba(56, 189, 248, 0.35);
  background: rgba(8, 20, 41, 0.68);
  backdrop-filter: blur(14px);
  border-radius: 12px;
  padding: 8px;
}
.live-frame video {
  width: 100%;
  border-radius: 12px;
  background: #000;
}
.live-controls {
  display: flex;
  justify-content: space-between;
  margin-top: 6px;
}
`;
  document.head.appendChild(style);
}

export function createLiveOverlay({ stream = null, muted = true, onClose = null } = {}) {
  const existing = document.getElementById('liveOverlay');
  if (existing) {
    if (stream) setLiveOverlayStream(stream);
    setLiveOverlayMuted(muted);
    return {
      overlayEl: existing,
      videoEl: existing.querySelector('#livePreview') || null,
      closeBtn: existing.querySelector('#liveClose') || null,
      muteBtn: existing.querySelector('#liveMute') || null,
    };
  }

  ensureOverlayStyles();
  onCloseHandler = typeof onClose === 'function' ? onClose : null;

  const overlay = document.createElement('div');
  overlay.id = 'liveOverlay';
  overlay.className = 'live-overlay';
  overlay.innerHTML = `
    <div class="live-frame">
      <video id="livePreview" autoplay playsinline controls muted></video>
      <div class="live-controls">
        <button id="liveMute" type="button">Unmute</button>
        <button id="liveClose" type="button">Close</button>
      </div>
    </div>
  `;

  document.body.appendChild(overlay);

  const videoEl = overlay.querySelector('#livePreview');
  const closeBtn = overlay.querySelector('#liveClose');
  const muteBtn = overlay.querySelector('#liveMute');

  if (stream) setLiveOverlayStream(stream);
  setLiveOverlayMuted(muted);

  closeBtn?.addEventListener('click', async () => {
    try {
      await onCloseHandler?.();
    } finally {
      destroyLiveOverlay();
    }
  });

  muteBtn?.addEventListener('click', () => {
    if (!videoEl) return;
    setLiveOverlayMuted(!videoEl.muted);
  });

  return { overlayEl: overlay, videoEl, closeBtn, muteBtn };
}

export function setLiveOverlayStream(stream) {
  const videoEl = document.getElementById('livePreview');
  if (!videoEl) return;
  if (stream instanceof MediaStream) {
    videoEl.srcObject = stream;
    videoEl.src = '';
    return;
  }
  if (typeof stream === 'string' && stream) {
    videoEl.srcObject = null;
    videoEl.src = stream;
  }
}

export function setLiveOverlayMuted(muted) {
  const videoEl = document.getElementById('livePreview');
  const muteBtn = document.getElementById('liveMute');
  if (!videoEl) return;
  videoEl.muted = !!muted;
  if (muteBtn) muteBtn.textContent = videoEl.muted ? 'Unmute' : 'Mute';
}

export function destroyLiveOverlay() {
  const overlay = document.getElementById('liveOverlay');
  if (!overlay) return;
  const videoEl = overlay.querySelector('#livePreview');
  if (videoEl) {
    try { videoEl.pause(); } catch (_) {}
    videoEl.srcObject = null;
    videoEl.removeAttribute('src');
  }
  overlay.remove();
  onCloseHandler = null;
}
