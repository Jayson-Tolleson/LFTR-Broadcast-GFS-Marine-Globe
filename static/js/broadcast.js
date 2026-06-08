(() => {
  const cfg = window.BROADCAST_CONFIG || {};
  const wsProto = location.protocol === 'https:' ? 'wss' : 'ws';
  const wsBase = `${wsProto}://${location.host}`;
  const VIDEO_PROFILE = {
    desktop: { width: 2560, height: 1440, fpsIdeal: 30, fpsMin: 15, fpsMax: 60, bitrate: 9_500_000 },
    // Mobile must be stable first. Android Chrome freezes when we canvas-composite
    // a hot 1440p camera plus STT plus two websocket startup paths. Default mobile
    // camera-only broadcast is now a direct 720p stream, with 540p fallback.
    mobile: { width: 1280, height: 720, fpsIdeal: 20, fpsMin: 8, fpsMax: 20, bitrate: 1_400_000 },
    mobile1080: { width: 1920, height: 1080, fpsIdeal: 20, fpsMin: 8, fpsMax: 24, bitrate: 2_500_000 },
    mobile720: { width: 1280, height: 720, fpsIdeal: 20, fpsMin: 8, fpsMax: 20, bitrate: 1_400_000 },
    fallback: { width: 960, height: 540, fpsIdeal: 18, fpsMin: 8, fpsMax: 18, bitrate: 900_000 },
  };


  function isProbablyMobile() {
    return /Android|iPhone|iPad|iPod/i.test(navigator.userAgent || '') || (navigator.maxTouchPoints || 0) > 1;
  }

  function broadcastAudioConstraints() {
    // Keep this conservative for Android/iOS. We resample for STT in AudioContext later.
    return {
      echoCancellation: true,
      noiseSuppression: !!state.media.noise_cancel_enabled,
      autoGainControl: true,
      channelCount: 1,
    };
  }

  function activeVideoProfile() {
    return isProbablyMobile() ? VIDEO_PROFILE.mobile : VIDEO_PROFILE.desktop;
  }

  function videoUpTo1440Constraints(extra = {}, profile = activeVideoProfile()) {
    // Desktop may request up to 1440p. Mobile is capped to the selected stable
    // profile so Android/iOS do not silently choose a hot 1440p camera mode and freeze.
    const mobile = isProbablyMobile();
    const maxProfile = mobile ? profile : VIDEO_PROFILE.desktop;
    return {
      width: { ideal: profile.width, max: maxProfile.width },
      height: { ideal: profile.height, max: maxProfile.height },
      frameRate: { ideal: profile.fpsIdeal, min: profile.fpsMin, max: profile.fpsMax },
      ...extra,
    };
  }

  function screenUpTo1440Constraints(extra = {}) {
    const profile = activeVideoProfile();
    // getDisplayMedia is fragile. Ask for up to 1440p, but avoid strict min constraints.
    return {
      width: { ideal: profile.width, max: VIDEO_PROFILE.desktop.width },
      height: { ideal: profile.height, max: VIDEO_PROFILE.desktop.height },
      frameRate: { ideal: profile.fpsIdeal, max: profile.fpsMax },
      ...extra,
    };
  }

  function applyProgramVideoHints(track) {
    if (!track) return;
    try { track.contentHint = 'motion'; } catch (_) {}
    try {
      const settings = track.getSettings?.() || {};
      console.info('[broadcast] program video track', {
        width: settings.width,
        height: settings.height,
        frameRate: settings.frameRate,
        contentHint: track.contentHint || '',
      });
    } catch (_) {}
  }


  function describeMediaStream(stream) {
    return {
      videoTracks: stream?.getVideoTracks?.().map((t) => ({
        label: t.label || '',
        readyState: t.readyState,
        muted: !!t.muted,
        settings: t.getSettings?.() || {},
      })) || [],
      audioTracks: stream?.getAudioTracks?.().map((t) => ({
        label: t.label || '',
        readyState: t.readyState,
        muted: !!t.muted,
        settings: t.getSettings?.() || {},
      })) || [],
    };
  }

  function waitForEventOnce(target, eventName, timeoutMs = 1000) {
    return new Promise((resolve) => {
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        try { target?.removeEventListener?.(eventName, finish); } catch (_) {}
        resolve();
      };
      try { target?.addEventListener?.(eventName, finish, { once: true }); } catch (_) {}
      setTimeout(finish, timeoutMs);
    });
  }

  async function waitForUsableVideoTrack(stream, reason = 'unknown', timeoutMs = 2200) {
    const startedAt = performance.now();
    let track = stream?.getVideoTracks?.()[0] || null;
    while (track && track.readyState === 'live' && (performance.now() - startedAt) < timeoutMs) {
      const settings = track.getSettings?.() || {};
      const w = Number(settings.width || 0);
      const h = Number(settings.height || 0);
      if (w > 0 && h > 0) break;
      await waitForEventOnce(track, 'unmute', 350);
      track = stream?.getVideoTracks?.()[0] || track;
      await new Promise((r) => setTimeout(r, 120));
    }
    console.info('[broadcast] media readiness check', { reason, elapsedMs: Math.round(performance.now() - startedAt), stream: describeMediaStream(stream) });
    return !!track && track.readyState === 'live';
  }

  function enforceMobileCameraOnly(reason = 'mobile') {
    if (!isProbablyMobile()) return false;
    if (state.media.screen_enabled || state.screenStream) {
      console.info('[broadcast] mobile camera-only path enforced', { reason });
    }
    state.media.screen_enabled = false;
    if (state.screenStream) {
      try { state.screenStream.getTracks().forEach((t) => t.stop()); } catch (_) {}
      state.screenStream = null;
    }
    if (programScreenVideo) programScreenVideo.srcObject = null;
    return true;
  }

  async function applySender1440pParameters(sender) {
    if (!sender || !sender.getParameters || !sender.setParameters) return;
    try {
      const profile = activeVideoProfile();
      const params = sender.getParameters() || {};
      params.encodings = params.encodings && params.encodings.length ? params.encodings : [{}];
      params.encodings[0].maxBitrate = profile.bitrate;
      params.encodings[0].maxFramerate = profile.fpsMax;
      params.encodings[0].scaleResolutionDownBy = 1;
      params.degradationPreference = isProbablyMobile() ? 'maintain-framerate' : 'balanced';
      await sender.setParameters(params);
    } catch (err) {
      console.debug('[broadcast] unable to apply sender params', err?.message || String(err));
    }
  }



  let mobileRestartPending = false;

  function installCameraTrackRestartHandlers(track) {
    if (!track) return;
    const restart = async (why) => {
      if (!isProbablyMobile() || mobileRestartPending || !state.media.camera_enabled) return;
      mobileRestartPending = true;
      console.warn('[broadcast] mobile camera track recovery', { why });
      try {
        if (state.camStream) {
          try { state.camStream.getTracks().forEach((t) => t.stop()); } catch (_) {}
          state.camStream = null;
        }
        await startCameraStream();
        await syncTracks();
        announceState();
      } catch (err) {
        console.warn('[broadcast] mobile camera recovery failed', { why, message: err?.message || String(err) });
      } finally {
        setTimeout(() => { mobileRestartPending = false; }, 1200);
      }
    };
    try { track.addEventListener('ended', () => restart('track_ended'), { once: true }); } catch (_) {}
    try { track.addEventListener('mute', () => setTimeout(() => {
      const t = state.camStream?.getVideoTracks?.()[0];
      if (t && t.muted && t.readyState === 'live') restart('track_muted_timeout');
    }, 2500), { once: true }); } catch (_) {}
  }

  let mobileCameraHealthTimer = 0;
  function installMobileCameraHealthWatch(track) {
    if (!isProbablyMobile() || !track?.getSettings) return;
    if (mobileCameraHealthTimer) clearTimeout(mobileCameraHealthTimer);
    mobileCameraHealthTimer = window.setTimeout(async () => {
      if (!state.camStream || !state.media.camera_enabled) return;
      const t = state.camStream.getVideoTracks()[0];
      const st = t?.getSettings?.() || {};
      const megapixels = (Number(st.width) || 0) * (Number(st.height) || 0);
      // Android Chrome can report success at a high camera mode and then stall.
      // Force the live track back to stable direct 720p/20fps constraints before it freezes.
      if (megapixels > 1300000 || Number(st.frameRate || 0) > 22) {
        try {
          await t.applyConstraints(videoUpTo1440Constraints({ facingMode: { ideal: selectedFacingMode || 'user' } }, VIDEO_PROFILE.mobile720));
          console.info('[broadcast] mobile camera stabilized to direct 720p/20fps constraints', t.getSettings?.() || {});
          await syncTracks();
        } catch (err) {
          console.info('[broadcast] mobile camera stabilize skipped', err?.message || String(err));
        }
      }
      if (state.media.camera_enabled && state.camStream?.getVideoTracks?.()[0]?.readyState === 'live') installMobileCameraHealthWatch(state.camStream.getVideoTracks()[0]);
    }, 4500);
  }

  const dom = {
    preview: document.getElementById('preview'),
    chat: document.getElementById('chat'),
    chatInput: document.getElementById('chatInput'),
    sendBtn: document.getElementById('sendBtn'),
    roomStatus: document.getElementById('roomStatus'),
    stRoom: document.getElementById('stRoom'),
    stServer: document.getElementById('stServer'),
    stRoomConn: document.getElementById('stRoomConn'),
    stLive: document.getElementById('stLive'),
    stAi: document.getElementById('stAi'),
    stWatchers: document.getElementById('stWatchers'),
    stPc: document.getElementById('stPc'),
    stIce: document.getElementById('stIce'),
    ledServer: document.getElementById('ledServer'),
    ledRoom: document.getElementById('ledRoom'),
    ledLive: document.getElementById('ledLive'),
    ledAi: document.getElementById('ledAi'),
    camBtn: document.getElementById('camBtn'),
    screenBtn: document.getElementById('screenBtn'),
    micBtn: document.getElementById('micBtn'),
    sttBtn: document.getElementById('sttBtn'),
    ncBtn: document.getElementById('ncBtn'),
    aiEnableBtn: document.getElementById('aiEnableBtn'),
    aiStatusBtn: document.getElementById('aiStatusBtn'),
    ttsMonBtn: document.getElementById('ttsMonBtn'),
    recordBtn: document.getElementById('recordBtn'),
    rtmpBtn: document.getElementById('rtmpBtn'),
    rtmpKeyInput: document.getElementById('rtmpKeyInput'),
    recordingStatus: document.getElementById('recordingStatus'),
    attachBtn: document.getElementById('attachBtn'),
    webBtn: document.getElementById('webBtn'),
    searchCloseBtn: document.getElementById('searchCloseBtn'),
    searchPane: document.getElementById('searchPane'),
    searchResults: document.getElementById('searchResults'),
    fileInput: document.getElementById('file'),
    chatCollapseBtn: document.getElementById('chatCollapseBtn'),
    chatPanel: document.querySelector('.chat'),
  };

  let chatRetryMs = 1200;
  let signalRetryMs = 1200;
  const DEBUG_CHAT = true;
  let lastChatSendAt = 0;
  let lastChatText = '';
  let selectedVideoDeviceId = '';
  let selectedFacingMode = 'user';
  let cachedVideoInputs = [];
  let cameraCycleIndex = -1;
  let recordingStream = null;
  let recordingChunks = [];
  let recordingMedia = null;
  let wakeLockSentinel = null;
  let programCanvas = null;
  let programCtx = null;
  let programStream = null;
  let programLoopHandle = 0;
  let programPumpTimer = 0;
  let programCamVideo = null;
  let programScreenVideo = null;
  let programAudioCtx = null;
  let programAudioSource = null;
  let programAudioDestination = null;
  let cameraStartPromise = null;
  let lastSingleTapAt = new WeakMap();
  let broadcasterMediaReady = false;
  const pendingViewerOffers = new Set();


  const state = {
    room: cfg.room || new URLSearchParams(location.search).get('room') || 'default',
    clientId: `b-${Math.random().toString(36).slice(2, 10)}`,
    pc: null,
    peerConnections: {},
    chatWs: null,
    signalWs: null,
    camStream: null,
    screenStream: null,
    speechCtx: null,
    speechSource: null,
    speechProcessor: null,
    media: {
      ai_enabled: false,
      ai_status: 'idle',
      stt_enabled: true,
      tts_enabled: false,
      hear_ai_voice: false,
      mic_enabled: true,
      camera_enabled: true,
      screen_enabled: false,
      noise_cancel_enabled: true,
      record_enabled: false,
      rtmp_enabled: false,
      rtmp_url: '',
    },
  };
  dom.stRoom && (dom.stRoom.textContent = state.room);

  function setLed(el, on) {
    if (!el) return;
    el.classList.remove('r', 'g');
    el.classList.add(on ? 'g' : 'r');
  }

  function sendJson(ws, type, extra = {}) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ type, room: state.room, clientId: state.clientId, role: 'broadcaster', ...extra }));
  }

  function applyPresence(presence) {
    const viewers = Number(presence.viewer_count ?? 0);
    dom.stWatchers && (dom.stWatchers.textContent = `${viewers}`);
    dom.roomStatus && (dom.roomStatus.textContent = `viewers: ${viewers}`);
    dom.stLive && (dom.stLive.textContent = presence.broadcaster_present ? 'live' : 'offline');
    setLed(dom.ledLive, !!presence.broadcaster_present);
  }

  function updateAiStatus(status) {
    state.media.ai_status = status || 'idle';
    dom.stAi && (dom.stAi.textContent = state.media.ai_status);
    if (dom.aiStatusBtn) dom.aiStatusBtn.textContent = `AI ${state.media.ai_status}`;
    setLed(dom.ledAi, state.media.ai_status === 'active' || state.media.ai_enabled);
  }



  async function acquireWakeLock(reason = 'init') {
    if (!('wakeLock' in navigator)) return;
    if (document.visibilityState !== 'visible') return;
    try {
      wakeLockSentinel = await navigator.wakeLock.request('screen');
      wakeLockSentinel.addEventListener('release', () => {
        if (document.visibilityState === 'visible') {
          acquireWakeLock('released').catch(() => {});
        }
      }, { once: true });
      console.info('[broadcast] wake lock active', { reason });
    } catch (err) {
      console.info('[broadcast] wake lock unavailable', { reason, message: err?.message || String(err) });
    }
  }


  function installMobileVisibilityRecovery() {
    if (!isProbablyMobile()) return;
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState !== 'visible') return;
      if (!state.media.camera_enabled) return;
      setTimeout(async () => {
        try {
          const track = state.camStream?.getVideoTracks?.()[0];
          if (!track || track.readyState !== 'live' || track.muted) {
            console.info('[broadcast] mobile visibility recovery camera restart');
            if (state.camStream) {
              try { state.camStream.getTracks().forEach((t) => t.stop()); } catch (_) {}
              state.camStream = null;
            }
          }
          await syncTracks();
          announceState();
        } catch (err) {
          console.warn('[broadcast] mobile visibility recovery failed', err?.message || String(err));
        }
      }, 500);
    });
  }

  function installWakeLock() {
    const retry = () => acquireWakeLock('visibility').catch(() => {});
    document.addEventListener('visibilitychange', retry);
    const activate = () => acquireWakeLock('gesture').catch(() => {});
    window.addEventListener('pointerdown', activate, { passive: true });
    window.addEventListener('touchstart', activate, { passive: true });
    window.addEventListener('keydown', activate, { passive: true });
    acquireWakeLock('boot').catch(() => {});
  }

  function applyRoomState(next = {}) {
    const settings = next.settings || {};
    state.media = { ...state.media, ...settings };
    state.media.ai_status = state.media.ai_enabled ? (state.media.ai_status || 'idle') : 'idle';
    if (next.runtime) applyPresence(next.runtime);
    updateAiStatus('disabled');

    const setTxt = (id, txt) => { const n = document.getElementById(id); if (n) n.textContent = txt; };
    setLed(document.getElementById('camLed'), !!state.media.camera_enabled);
    if (!state.media.camera_enabled) {
      setTxt('camTxt', 'CAM: off');
    }
    setLed(document.getElementById('screenLed'), !!state.media.screen_enabled);
    setTxt('screenTxt', `SCREEN: ${state.media.screen_enabled ? 'on' : 'off'}`);
    setLed(document.getElementById('micLed'), !!state.media.mic_enabled);
    setTxt('micTxt', `MIC: ${state.media.mic_enabled ? 'on' : 'off'}`);
    setLed(document.getElementById('sttLed'), !!state.media.stt_enabled);
    setTxt('sttTxt', `STT: ${state.media.stt_enabled ? 'on' : 'off'}`);
    setLed(document.getElementById('ncLed'), !!state.media.noise_cancel_enabled);
    setTxt('ncTxt', `NoiseCancel: ${state.media.noise_cancel_enabled ? 'on' : 'off'}`);
    setLed(document.getElementById('ttsMonLed'), !!state.media.hear_ai_voice);
    setTxt('ttsMonTxt', `AI voice: ${state.media.hear_ai_voice ? 'on' : 'off'}`);
    setLed(document.getElementById('aiEnableLed'), !!state.media.ai_enabled);
    setTxt('aiEnableTxt', `AI: ${state.media.ai_enabled ? 'on' : 'off'}`);
    setLed(document.getElementById('recordLed'), !!state.media.record_enabled);
    setTxt('recordTxt', `Record: ${state.media.record_enabled ? 'on' : 'off'}`);
    setLed(document.getElementById('rtmpLed'), !!state.media.rtmp_enabled);
    setTxt('rtmpTxt', `RTMP: ${state.media.rtmp_enabled ? 'on' : 'off'}`);
  }

  function compactCameraName(label) {
    const text = String(label || '').trim();
    if (!text) return 'camera';
    if (/front|user/i.test(text)) return 'front cam';
    if (/back|rear|environment/i.test(text)) return 'back cam';
    return text.length > 18 ? `${text.slice(0, 18)}…` : text;
  }

  function updateCameraLabel(label) {
    const camTxt = document.getElementById('camTxt');
    if (!camTxt) return;
    camTxt.textContent = state.media.camera_enabled ? `CAM: ${compactCameraName(label)}` : 'CAM: off';
  }


  function htmlEscape(value) {
    return String(value || '').replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
  }

  function googleAiUrl(query) {
    const q = String(query || '').trim();
    const base = 'https://www.google.com/ai';
    return q ? `${base}?q=${encodeURIComponent(q)}` : base;
  }

  function isImageAttachment(att = {}) {
    const mime = String(att.mime || att.mimetype || att.type || '').toLowerCase();
    const url = String(att.url || att.path || '').toLowerCase();
    return mime.startsWith('image/') || /\.(png|jpe?g|gif|webp|bmp|svg)(\?|#|$)/i.test(url);
  }

  function appendIframeAttachment(parent, attachment = {}) {
    const url = attachment.url || attachment.path || '';
    if (!url) return;
    const label = attachment.name || attachment.title || 'attachment';
    const kind = String(attachment.kind || '').toLowerCase();
    const meta = document.createElement('div');
    meta.className = 'attachmentMeta';
    meta.textContent = label;
    parent.appendChild(meta);

    const iframe = document.createElement('iframe');
    iframe.className = isImageAttachment(attachment) ? 'chatFrame imageFrame' : 'chatFrame';
    iframe.loading = 'lazy';
    iframe.referrerPolicy = 'no-referrer-when-downgrade';
    iframe.setAttribute('sandbox', 'allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox allow-same-origin');

    if (isImageAttachment(attachment)) {
      iframe.srcdoc = `<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><style>html,body{margin:0;height:100%;background:#020617;display:grid;place-items:center}img{max-width:100%;max-height:100%;object-fit:contain}</style></head><body><img src="${htmlEscape(url)}" alt="${htmlEscape(label)}"></body></html>`;
    } else if (kind === 'google_ai_search' || attachment.iframe) {
      iframe.src = url;
    } else {
      const linkOnly = document.createElement('a');
      linkOnly.href = url;
      linkOnly.target = '_blank';
      linkOnly.rel = 'noopener';
      linkOnly.className = 'chatFrameLink';
      linkOnly.textContent = `Open ${label}`;
      parent.appendChild(linkOnly);
      return;
    }
    parent.appendChild(iframe);
    const open = document.createElement('a');
    open.href = url;
    open.target = '_blank';
    open.rel = 'noopener';
    open.className = 'chatFrameLink';
    open.textContent = kind === 'google_ai_search' ? 'Open Google AI in new tab if the iframe is blocked' : 'Open attachment in new tab';
    parent.appendChild(open);
  }

  function appendGoogleAiSearchFrame(query, broadcastToRoom = true) {
    const clean = String(query || '').trim();
    if (!clean) return;
    const payload = {
      url: googleAiUrl(clean),
      name: `Google AI: ${clean}`,
      title: `Google AI: ${clean}`,
      kind: 'google_ai_search',
      mime: 'text/html',
      iframe: true,
      query: clean,
    };
    appendChat({ user: 'web', text: `Google AI search: ${clean}`, attachment: payload });
    if (broadcastToRoom && typeof sendJson === 'function') {
      if (typeof state !== 'undefined' && state?.chatWs) sendJson(state.chatWs, 'attachment_uploaded', { attachment: payload });
      else sendJson('attachment_uploaded', { attachment: payload });
    }
  }

  function appendChat(msg) {
    if (!dom.chat) return;
    const startedAt = performance.now();
    const d = document.createElement('article');
    d.className = 'entry';
    const who = msg.user || msg.sender || 'system';
    const text = msg.text || msg.payload?.text || '';
    const whoEl = document.createElement('b');
    const label = msg.source === 'stt' ? 'STT' : who;
    if (msg.source === 'stt') d.classList.add('stt-entry');
    whoEl.textContent = `[${label}]`;
    const textEl = document.createElement('div');
    textEl.textContent = String(text);
    d.append(whoEl, textEl);
    if (msg.attachment?.url || msg.attachment?.path) {
      appendIframeAttachment(d, msg.attachment);
    }
    dom.chat.appendChild(d);
    dom.chat.scrollTop = dom.chat.scrollHeight;
    if (DEBUG_CHAT) {
      console.debug('[broadcast.chat] render_ms', Math.round((performance.now() - startedAt) * 1000) / 1000);
    }
  }

  function renderSearchResults(query, result) {
    if (!dom.searchPane || !dom.searchResults) return;
    const results = (((result || {}).data || {}).results || []);
    dom.searchPane.classList.add('open');
    dom.searchResults.textContent = '';
    const frag = document.createDocumentFragment();
    if (!results.length) {
      const empty = document.createElement('div');
      empty.className = 'searchMeta';
      empty.textContent = `No results for "${query}".`;
      frag.appendChild(empty);
    } else {
      for (const item of results.slice(0, 8)) {
        const row = document.createElement('div');
        row.className = 'searchItem';
        const title = document.createElement('a');
        title.href = item.url || '#';
        title.target = '_blank';
        title.rel = 'noopener';
        title.textContent = item.title || item.url || 'Result';
        const snip = document.createElement('div');
        snip.textContent = item.snippet || '';
        const meta = document.createElement('div');
        meta.className = 'searchMeta';
        meta.textContent = item.source || 'web';
        row.append(title, snip, meta);
        frag.appendChild(row);
      }
    }
    dom.searchResults.appendChild(frag);
  }

  function updateConnectivity(online) {
    dom.stServer && (dom.stServer.textContent = online ? 'connected' : 'disconnected');
    dom.stRoomConn && (dom.stRoomConn.textContent = online ? 'connected' : 'disconnected');
    setLed(dom.ledServer, online);
    setLed(dom.ledRoom, online);
  }


  function scoreBackCamera(device, idx) {
    const label = String(device?.label || '').toLowerCase();
    let score = 0;
    if (/back|rear|environment|world|facing back/.test(label)) score += 100;
    if (/front|user|face/i.test(label)) score -= 100;
    // Android often lists the rear camera after the selfie camera once permission is granted.
    score += idx;
    return score;
  }

  function preferredCameraConstraint() {
    if (selectedVideoDeviceId) return videoUpTo1440Constraints({ deviceId: { exact: selectedVideoDeviceId } });
    // Default to front/user camera on mobile. It is lighter and matches the broadcast UX.
    return videoUpTo1440Constraints({
      facingMode: isProbablyMobile()
        ? { ideal: selectedFacingMode || 'user' }
        : { ideal: selectedFacingMode || 'user' },
    });
  }

  async function startCameraStreamInner() {
    if (state.camStream) {
      const activeTrack = state.camStream.getVideoTracks()[0];
      const activeDeviceId = activeTrack?.getSettings?.().deviceId || '';
      if (!selectedVideoDeviceId || selectedVideoDeviceId === activeDeviceId) {
        await attachProgramSource('camera', state.camStream);
        return state.camStream;
      }
      state.camStream.getTracks().forEach((t) => t.stop());
      state.camStream = null;
    }

    const facing = selectedFacingMode || 'user';
    const mobile = isProbablyMobile();
    const attempts = mobile
      ? [
          { name: 'mobile direct front 720p stable', profile: VIDEO_PROFILE.mobile720, extra: selectedVideoDeviceId ? { deviceId: { exact: selectedVideoDeviceId } } : { facingMode: { ideal: facing } } },
          { name: 'mobile direct front 540p fallback', profile: VIDEO_PROFILE.fallback, extra: { facingMode: { ideal: facing } } },
          { name: 'mobile any-camera 540p last resort', profile: VIDEO_PROFILE.fallback, extra: {} },
        ]
      : [
          { name: 'desktop 1440p preferred', profile: VIDEO_PROFILE.desktop, extra: selectedVideoDeviceId ? { deviceId: { exact: selectedVideoDeviceId } } : { facingMode: { ideal: facing } } },
          { name: 'desktop 1440p any-camera', profile: VIDEO_PROFILE.desktop, extra: {} },
          { name: 'desktop 720p fallback', profile: VIDEO_PROFILE.mobile720, extra: {} },
        ];

    let lastErr = null;
    for (const attempt of attempts) {
      try {
        state.camStream = await navigator.mediaDevices.getUserMedia({
          video: videoUpTo1440Constraints(attempt.extra, attempt.profile),
          audio: broadcastAudioConstraints(),
        });
        console.info('[broadcast] camera acquired', attempt.name, describeMediaStream(state.camStream));
        break;
      } catch (err) {
        lastErr = err;
        console.warn('[broadcast] camera attempt failed', attempt.name, err?.message || String(err));
        // If exact deviceId failed, stop pinning it so front/back facingMode can recover.
        if (selectedVideoDeviceId) selectedVideoDeviceId = '';
      }
    }
    if (!state.camStream) throw lastErr || new Error('camera unavailable');
    const track = state.camStream.getVideoTracks()[0];
    applyProgramVideoHints(track);
    installCameraTrackRestartHandlers(track);
    selectedVideoDeviceId = track?.getSettings?.().deviceId || selectedVideoDeviceId;
    const settings = track?.getSettings?.() || {};
    selectedFacingMode = settings.facingMode || selectedFacingMode || 'user';
    updateCameraLabel(track?.label || (selectedFacingMode === 'user' ? 'front cam' : (isProbablyMobile() ? 'front cam' : 'camera')));
    installMobileCameraHealthWatch(track);
    await attachProgramSource('camera', state.camStream);
    await configureProgramAudioTrack();
    updatePreviewToProgram();
    return state.camStream;
  }

  async function startCameraStream() {
    if (cameraStartPromise) return cameraStartPromise;
    cameraStartPromise = startCameraStreamInner().finally(() => { cameraStartPromise = null; });
    return cameraStartPromise;
  }

  async function refreshVideoInputs() {
    try {
      // A granted camera permission makes Android labels/devices reliable.
      const devices = await navigator.mediaDevices.enumerateDevices();
      cachedVideoInputs = devices.filter((d) => d.kind === 'videoinput');
    } catch {
      cachedVideoInputs = [];
    }
    return cachedVideoInputs;
  }

  async function selectBestBackCamera() {
    const inputs = await refreshVideoInputs();
    if (!inputs.length) return false;
    const ranked = inputs.map((d, i) => ({ d, i, score: scoreBackCamera(d, i) })).sort((a, b) => b.score - a.score);
    const best = ranked[0]?.d;
    if (!best?.deviceId) return false;
    selectedVideoDeviceId = best.deviceId;
    selectedFacingMode = 'environment';
    cameraCycleIndex = inputs.findIndex((d) => d.deviceId === selectedVideoDeviceId);
    if (state.camStream) {
      state.camStream.getTracks().forEach((t) => t.stop());
      state.camStream = null;
    }
    await syncTracks();
    updateCameraLabel(best.label || 'back cam');
    return true;
  }

  async function cycleCameraDevice() {
    state.media.camera_enabled = true;
    const previousDeviceId = selectedVideoDeviceId;
    const previousFacingMode = selectedFacingMode || 'user';

    if (isProbablyMobile()) {
      // On Android/iOS, device labels are often empty until permission and deviceId cycling can land
      // on a virtual/off camera. Toggle facingMode first; this is the reliable front/back control.
      selectedVideoDeviceId = '';
      selectedFacingMode = previousFacingMode === 'user' ? 'environment' : 'user';
      if (state.camStream) {
        state.camStream.getTracks().forEach((t) => t.stop());
        state.camStream = null;
      }
      try {
        await syncTracks();
        updateCameraLabel(selectedFacingMode === 'user' ? 'front cam' : 'back cam');
        announceState();
        return;
      } catch (err) {
        console.warn('[broadcast] facing-mode toggle failed, trying enumerated device cycle', err?.message || String(err));
        selectedVideoDeviceId = previousDeviceId;
        selectedFacingMode = previousFacingMode;
      }
    }

    await refreshVideoInputs();
    if (!cachedVideoInputs.length) {
      // Do not turn the camera off when enumerateDevices is empty; retry the preferred facing camera.
      selectedVideoDeviceId = '';
      if (state.camStream) {
        state.camStream.getTracks().forEach((t) => t.stop());
        state.camStream = null;
      }
      await syncTracks();
      announceState();
      return;
    }

    const inputs = cachedVideoInputs;
    const idx = inputs.findIndex((d) => d.deviceId === selectedVideoDeviceId);
    cameraCycleIndex = idx >= 0 ? idx : cameraCycleIndex;
    cameraCycleIndex = (cameraCycleIndex + 1) % inputs.length;
    selectedVideoDeviceId = inputs[cameraCycleIndex].deviceId;
    selectedFacingMode = '';
    if (state.camStream) {
      state.camStream.getTracks().forEach((t) => t.stop());
      state.camStream = null;
    }
    await syncTracks();
    updateCameraLabel(inputs[cameraCycleIndex].label || `camera ${cameraCycleIndex + 1}`);
    announceState();
  }

  function ensureVideoElement(kind) {
    let video = kind === 'screen' ? programScreenVideo : programCamVideo;
    if (video) return video;
    video = document.createElement('video');
    video.muted = true;
    video.playsInline = true;
    video.autoplay = true;
    video.style.display = 'none';
    document.body.appendChild(video);
    if (kind === 'screen') programScreenVideo = video; else programCamVideo = video;
    return video;
  }

  async function attachProgramSource(kind, stream) {
    if (!stream) return null;
    const video = ensureVideoElement(kind);
    if (video.srcObject !== stream) video.srcObject = stream;
    try { video.muted = true; video.playsInline = true; video.autoplay = true; } catch (_) {}
    try { await video.play(); } catch (_) {}
    if (video.readyState < 1) {
      await new Promise((resolve) => {
        const done = () => resolve();
        video.addEventListener('loadedmetadata', done, { once: true });
        setTimeout(done, isProbablyMobile() ? 1600 : 900);
      });
      try { await video.play(); } catch (_) {}
    }
    console.info('[broadcast] program source attached', {
      kind,
      tracks: stream.getTracks().map((t) => `${t.kind}:${t.readyState}`).join(','),
      width: video.videoWidth || 0,
      height: video.videoHeight || 0,
      readyState: video.readyState,
    });
    return video;
  }

  function ensureProgramStream() {
    if (programStream) return programStream;
    programCanvas = document.createElement('canvas');
    const profile = activeVideoProfile();
    programCanvas.width = profile.width;
    programCanvas.height = profile.height;
    programCtx = programCanvas.getContext('2d', { alpha: false });
    programStream = programCanvas.captureStream(activeVideoProfile().fpsIdeal);
    applyProgramVideoHints(programStream.getVideoTracks()[0]);
    startProgramLoop();
    return programStream;
  }

  function startProgramLoop() {
    if (programLoopHandle || programPumpTimer) return;
    const drawOnce = () => {
      if (!programCtx || !programCanvas) return;
      const w = programCanvas.width;
      const h = programCanvas.height;
      programCtx.fillStyle = '#05070b';
      programCtx.fillRect(0, 0, w, h);
      const screenReady = !!(state.media.screen_enabled && programScreenVideo?.srcObject && programScreenVideo.readyState >= 2 && programScreenVideo.videoWidth);
      const camReady = !!(programCamVideo?.srcObject && programCamVideo.readyState >= 2 && programCamVideo.videoWidth);
      const mainVideo = screenReady ? programScreenVideo : (camReady ? programCamVideo : null);
      if (mainVideo) {
        drawCover(programCtx, mainVideo, 0, 0, w, h);
      } else {
        programCtx.fillStyle = 'rgba(255,255,255,.72)';
        programCtx.font = '28px system-ui, -apple-system, Segoe UI, sans-serif';
        programCtx.fillText('Broadcast program output waiting for camera/screen…', 44, 64);
      }
      if (screenReady && state.media.camera_enabled && camReady) {
        const pw = Math.round(w * 0.26);
        const ph = Math.round(pw * 9 / 16);
        const pad = Math.round(w * 0.025);
        const x = w - pw - pad;
        const y = h - ph - pad;
        // Screen+Cam PiP glow: large cobalt blue outside, hunter green inner glow.
        const cobaltBlue = 'rgba(0, 71, 171, 0.95)';
        const hunterGreen = 'rgba(53, 94, 59, 0.95)';
        programCtx.save();
        programCtx.shadowColor = cobaltBlue;
        programCtx.shadowBlur = Math.round(w * 0.018);
        programCtx.lineWidth = Math.max(10, Math.round(w * 0.0075));
        programCtx.strokeStyle = cobaltBlue;
        roundRect(programCtx, x - 12, y - 12, pw + 24, ph + 24, 24, false, true);
        programCtx.shadowColor = hunterGreen;
        programCtx.shadowBlur = Math.round(w * 0.012);
        programCtx.lineWidth = Math.max(6, Math.round(w * 0.0045));
        programCtx.strokeStyle = hunterGreen;
        roundRect(programCtx, x - 5, y - 5, pw + 10, ph + 10, 18, false, true);
        programCtx.shadowColor = 'rgba(0,0,0,.65)';
        programCtx.shadowBlur = 14;
        programCtx.fillStyle = 'rgba(0,0,0,.42)';
        roundRect(programCtx, x - 10, y - 10, pw + 20, ph + 20, 20, true, false);
        programCtx.restore();

        programCtx.save();
        programCtx.beginPath();
        roundRect(programCtx, x, y, pw, ph, 14, false, false);
        programCtx.clip();
        drawCover(programCtx, programCamVideo, x, y, pw, ph);
        programCtx.restore();

        programCtx.save();
        programCtx.lineWidth = Math.max(3, Math.round(w * 0.0022));
        programCtx.strokeStyle = hunterGreen;
        roundRect(programCtx, x, y, pw, ph, 14, false, true);
        programCtx.lineWidth = Math.max(2, Math.round(w * 0.0014));
        programCtx.strokeStyle = cobaltBlue;
        roundRect(programCtx, x + 4, y + 4, pw - 8, ph - 8, 11, false, true);
        programCtx.restore();
      }
      const track = programStream?.getVideoTracks?.()[0];
      try { track?.requestFrame?.(); } catch (_) {}
    };
    const rafDraw = () => {
      programLoopHandle = requestAnimationFrame(rafDraw);
      drawOnce();
    };
    rafDraw();
    // requestAnimationFrame can pause or become inconsistent during screen-picker/source changes.
    // Keep a 30fps-ish pump so the canvas capture track always emits frames to /watch.
    programPumpTimer = window.setInterval(drawOnce, Math.round(1000 / activeVideoProfile().fpsIdeal));
  }

  function roundRect(ctx, x, y, w, h, r, fill, stroke) {
    const rr = Math.min(r, w / 2, h / 2);
    ctx.beginPath();
    ctx.moveTo(x + rr, y);
    ctx.arcTo(x + w, y, x + w, y + h, rr);
    ctx.arcTo(x + w, y + h, x, y + h, rr);
    ctx.arcTo(x, y + h, x, y, rr);
    ctx.arcTo(x, y, x + w, y, rr);
    ctx.closePath();
    if (fill) ctx.fill();
    if (stroke) ctx.stroke();
  }

  function drawCover(ctx, video, x, y, w, h) {
    const vw = video.videoWidth || w;
    const vh = video.videoHeight || h;
    const scale = Math.max(w / vw, h / vh);
    const sw = w / scale;
    const sh = h / scale;
    const sx = Math.max(0, (vw - sw) / 2);
    const sy = Math.max(0, (vh - sh) / 2);
    try { ctx.drawImage(video, sx, sy, sw, sh, x, y, w, h); } catch (_) {}
  }

  async function configureProgramAudioTrack() {
    ensureProgramStream();
    const micTrack = state.media.mic_enabled ? state.camStream?.getAudioTracks?.()[0] : null;
    if (!micTrack) return programStream;
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) {
      if (!programStream.getAudioTracks().length) programStream.addTrack(micTrack.clone());
      return programStream;
    }
    if (!programAudioCtx) {
      programAudioCtx = new Ctx();
      programAudioDestination = programAudioCtx.createMediaStreamDestination();
      const stableAudioTrack = programAudioDestination.stream.getAudioTracks()[0];
      if (stableAudioTrack && !programStream.getAudioTracks().length) programStream.addTrack(stableAudioTrack);
    }
    try { await programAudioCtx.resume(); } catch (_) {}
    if (programAudioSource) {
      try { programAudioSource.disconnect(); } catch (_) {}
      programAudioSource = null;
    }
    try {
      programAudioSource = programAudioCtx.createMediaStreamSource(new MediaStream([micTrack]));
      programAudioSource.connect(programAudioDestination);
    } catch (_) {}
    return programStream;
  }

  function updatePreviewToProgram(stream = null) {
    const target = stream || (state.media.screen_enabled ? ensureProgramStream() : state.camStream);
    if (dom.preview && target && dom.preview.srcObject !== target) dom.preview.srcObject = target;
  }

  async function startScreenStream() {
    if (state.screenStream) {
      await attachProgramSource('screen', state.screenStream);
      updatePreviewToProgram();
      return state.screenStream;
    }
    try {
      state.screenStream = await navigator.mediaDevices.getDisplayMedia({ video: screenUpTo1440Constraints({ displaySurface: 'monitor' }), audio: false });
    } catch (err) {
      console.warn('[broadcast] screen capture request failed, retrying browser default screen capture', err?.message || String(err));
      state.screenStream = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: false });
    }
    state.screenStream.getVideoTracks().forEach(applyProgramVideoHints);
    state.screenStream.getVideoTracks().forEach((t) => t.addEventListener('ended', () => {
      if (!state.media.screen_enabled) return;
      state.media.screen_enabled = false;
      switchToCamera().catch(() => announceState());
    }));
    await attachProgramSource('screen', state.screenStream);
    // Make sure the PiP camera exists before we switch the program canvas to screen.
    if (state.media.camera_enabled) startCameraStream().catch(() => {});
    updatePreviewToProgram();
    return state.screenStream;
  }

  async function ensurePeerConnection(viewerId = null) {
    if (!viewerId && state.pc) return state.pc;
    if (viewerId && state.peerConnections[viewerId]) return state.peerConnections[viewerId];
    const iceCfg = await fetch(cfg.iceConfigUrl || '/webrtc/ice-config').then((r) => r.json()).catch(() => ({ iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] }));
    const pc = new RTCPeerConnection({ iceServers: iceCfg.iceServers || [{ urls: 'stun:stun.l.google.com:19302' }] });
    if (viewerId) state.peerConnections[viewerId] = pc;
    else state.pc = pc;
    pc.onicecandidate = (e) => {
      if (!e.candidate) return;
      if (viewerId) sendJson(state.signalWs, 'ice-candidate', { viewerId, candidate: e.candidate });
      else sendJson(state.signalWs, 'webrtc_ice', { candidate: e.candidate });
    };
    pc.onconnectionstatechange = () => {
      dom.stPc && (dom.stPc.textContent = pc.connectionState);
      if (viewerId && ['failed', 'closed', 'disconnected'].includes(pc.connectionState)) {
        console.warn('[broadcast] viewer pc state needs recovery', { viewerId, state: pc.connectionState });
        if (pc.connectionState === 'failed' || pc.connectionState === 'closed') removeViewerPeer(viewerId);
      }
    };
    pc.oniceconnectionstatechange = () => dom.stIce && (dom.stIce.textContent = pc.iceConnectionState);
    if (viewerId) {
      const stream = await getProgramOutputStream();
      await waitForUsableVideoTrack(stream, `viewer_${viewerId}_before_addTrack`, isProbablyMobile() ? 3000 : 1400);
      const tracks = stream?.getTracks?.() || [];
      console.info('[broadcast] adding tracks for viewer', { viewerId, tracks: tracks.map((t) => `${t.kind}:${t.readyState}:${t.label || ''}`), stream: describeMediaStream(stream) });
      for (const track of tracks) {
        const sender = pc.addTrack(track, stream);
        if (track.kind === 'video') {
          applyProgramVideoHints(track);
          await applySender1440pParameters(sender);
        }
      }
    }
    return pc;
  }

  async function createOfferForViewer(viewerId) {
    if (!viewerId) return;
    if (!broadcasterMediaReady) {
      pendingViewerOffers.add(viewerId);
      console.info('[broadcast] viewer offer queued until media ready', { viewerId, queued: pendingViewerOffers.size });
      try { await syncTracks(); } catch (err) { console.warn('[broadcast] queued media sync failed', err?.message || String(err)); }
    }
    const pc = await ensurePeerConnection(viewerId);
    const senders = pc.getSenders?.() || [];
    const hasVideoSender = senders.some((sdr) => sdr.track?.kind === 'video' && sdr.track.readyState === 'live');
    console.info('[broadcast] creating offer for viewer', { viewerId, hasVideoSender, senders: senders.map((sdr) => `${sdr.track?.kind || 'none'}:${sdr.track?.readyState || 'none'}`) });
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    sendJson(state.signalWs, 'offer', { viewerId, sdp: offer.sdp, type: offer.type });
  }

  async function flushPendingViewerOffers(reason = 'media_ready') {
    if (!pendingViewerOffers.size) return;
    const viewers = Array.from(pendingViewerOffers);
    pendingViewerOffers.clear();
    console.info('[broadcast] flushing queued viewer offers', { reason, viewers });
    for (const viewerId of viewers) {
      try { await createOfferForViewer(viewerId); } catch (err) { console.warn('[broadcast] queued viewer offer failed', { viewerId, message: err?.message || String(err) }); }
    }
  }

  function removeViewerPeer(viewerId) {
    const pc = state.peerConnections[viewerId];
    if (!pc) return;
    try { pc.close(); } catch (_) {}
    delete state.peerConnections[viewerId];
  }

  async function replaceOutgoingVideoTrack(newTrack) {
    applyProgramVideoHints(newTrack);
    for (const pc of Object.values(state.peerConnections)) {
      const sender = pc.getSenders().find((s) => s.track && s.track.kind === 'video');
      if (sender) {
        await sender.replaceTrack(newTrack || null);
        await applySender1440pParameters(sender);
      }
    }
  }

  async function replaceOutgoingAudioTrack(newTrack) {
    for (const pc of Object.values(state.peerConnections)) {
      const sender = pc.getSenders().find((s) => s.track && s.track.kind === 'audio');
      if (sender) await sender.replaceTrack(newTrack || null);
    }
  }

  async function getProgramOutputStream() {
    // Mobile /broadcast is intentionally one clean camera-only WebRTC publisher.
    // Desktop may use screen+camera canvas composition; mobile should not be routed
    // through the canvas/screen path unless we explicitly redesign that later.
    if (isProbablyMobile()) enforceMobileCameraOnly('getProgramOutputStream');
    if (!state.media.screen_enabled) {
      if (state.media.camera_enabled || state.media.mic_enabled) await startCameraStream();
      updatePreviewToProgram(state.camStream);
      await waitForUsableVideoTrack(state.camStream, 'camera_program_output', isProbablyMobile() ? 2600 : 1200);
      return state.camStream;
    }
    ensureProgramStream();
    if (state.media.camera_enabled || state.media.mic_enabled) await startCameraStream();
    await startScreenStream();
    await configureProgramAudioTrack();
    updatePreviewToProgram(programStream);
    await waitForUsableVideoTrack(programStream, 'screen_program_output', 1200);
    return programStream;
  }


  async function syncTracks() {
    if (isProbablyMobile()) enforceMobileCameraOnly('syncTracks');
    const stream = await getProgramOutputStream();
    const vTrack = stream?.getVideoTracks?.()[0] || null;
    const aTrack = state.media.mic_enabled ? (stream?.getAudioTracks?.()[0] || null) : null;
    const videoReady = await waitForUsableVideoTrack(stream, 'syncTracks', isProbablyMobile() ? 2600 : 1200);
    broadcasterMediaReady = !!videoReady || !!aTrack;
    console.info('[broadcast] sync program tracks', { video: !!vTrack, videoReady, audio: !!aTrack, screen: state.media.screen_enabled, camera: state.media.camera_enabled, mediaReady: broadcasterMediaReady, stream: describeMediaStream(stream) });
    await replaceOutgoingVideoTrack(vTrack);
    await replaceOutgoingAudioTrack(aTrack);
    updatePreviewToProgram();
    if (broadcasterMediaReady) {
      sendJson(state.signalWs, 'media_ready');
      sendJson(state.signalWs, 'set_media_mode', { camera: state.media.camera_enabled, screen: state.media.screen_enabled, mic: state.media.mic_enabled });
      await flushPendingViewerOffers('syncTracks');
    }
  }

  async function switchToScreen() {
    if (isProbablyMobile()) {
      console.info('[broadcast] screen share ignored on mobile; keeping camera-only publisher');
      state.media.screen_enabled = false;
      state.media.camera_enabled = true;
      await syncTracks();
      announceState();
      return;
    }
    state.media.screen_enabled = true;
    state.media.camera_enabled = true; // camera stays alive as PiP on top of screen share
    await syncTracks();
    console.info('[broadcast] switched to screen+camera program', { videoTracks: programStream?.getVideoTracks?.().length || 0, audioTracks: programStream?.getAudioTracks?.().length || 0 });
    announceState();
  }

  async function switchToCamera() {
    state.media.screen_enabled = false;
    if (state.screenStream) {
      state.screenStream.getTracks().forEach((t) => t.stop());
      state.screenStream = null;
      if (programScreenVideo) programScreenVideo.srcObject = null;
    }
    await syncTracks();
    announceState();
  }

  const STT_TARGET_SAMPLE_RATE = 16000;
  const STT_TARGET_CHANNELS = 1;
  const STT_CHUNK_MS = 1800;
  const STT_MAX_CHUNK_MS = 2800;
  const STT_MIN_CHUNK_MS = 700;
  let sttPcmParts = [];
  let sttPcmSamples = 0;
  let sttChunkStartedAt = 0;
  let sttChunkSeq = 0;

  function stopSpeechCapture() {
    flushSpeechBuffer('stop');
    if (state.speechProcessor) {
      try { state.speechProcessor.disconnect(); } catch (_) {}
      state.speechProcessor.onaudioprocess = null;
      state.speechProcessor = null;
    }
    if (state.speechSource) {
      try { state.speechSource.disconnect(); } catch (_) {}
      state.speechSource = null;
    }
    if (state.speechCtx) {
      try { state.speechCtx.close(); } catch (_) {}
      state.speechCtx = null;
    }
  }

  async function playAiVoice(url) {
    if (!url || !state.media.hear_ai_voice) return;
    try {
      const audio = new Audio(url);
      audio.volume = 0.9;
      await audio.play();
    } catch (_) {}
  }

  function currentProgramStream() {
    const stream = state.media.screen_enabled ? ensureProgramStream() : state.camStream;
    const tracks = [];
    const videoTrack = stream?.getVideoTracks?.()[0];
    const audioTrack = state.media.mic_enabled ? stream?.getAudioTracks?.()[0] : null;
    if (videoTrack) tracks.push(videoTrack);
    if (audioTrack) tracks.push(audioTrack);
    return tracks.length ? new MediaStream(tracks) : null;
  }

  function downloadRecordingBlob(blob) {
    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `broadcast-${stamp}.webm`;
    a.style.display = 'none';
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 5000);
  }

  async function toggleRecording() {
    if (recordingMedia && recordingMedia.state === 'recording') {
      recordingMedia.stop();
      state.media.record_enabled = false;
      announceState();
      return;
    }
    const stream = currentProgramStream();
    if (!stream) {
      if (dom.recordingStatus) dom.recordingStatus.textContent = 'Record unavailable: no active camera/screen';
      return;
    }
    recordingChunks = [];
    recordingStream = stream;
    const mime = MediaRecorder.isTypeSupported('video/webm;codecs=vp9,opus') ? 'video/webm;codecs=vp9,opus' : (MediaRecorder.isTypeSupported('video/webm;codecs=vp8,opus') ? 'video/webm;codecs=vp8,opus' : 'video/webm');
    recordingMedia = new MediaRecorder(stream, { mimeType: mime, videoBitsPerSecond: activeVideoProfile().bitrate, audioBitsPerSecond: 128000 });
    recordingMedia.ondataavailable = (ev) => { if (ev.data?.size) recordingChunks.push(ev.data); };
    recordingMedia.onstop = async () => {
      const blob = new Blob(recordingChunks, { type: 'video/webm' });
      recordingChunks = [];
      recordingStream = null;
      if (blob.size > 0) {
        downloadRecordingBlob(blob);
        if (dom.recordingStatus) dom.recordingStatus.textContent = `Downloaded WebM (${Math.round(blob.size / 1024)} KB)`;
      } else if (dom.recordingStatus) {
        dom.recordingStatus.textContent = 'Recording stopped: no data captured';
      }
    };
    recordingMedia.start(1000);
    state.media.record_enabled = true;
    if (dom.recordingStatus) dom.recordingStatus.textContent = 'Recording live…';
    announceState();
  }

  async function toggleRtmp() {
    const next = !state.media.rtmp_enabled;
    const streamKey = (dom.rtmpKeyInput?.value || '').trim();
    const res = await fetch('/api/broadcast/rtmp', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ room: state.room, enabled: next, stream_key: streamKey }),
    }).then((r) => r.json()).catch(() => ({ ok: false }));
    if (!res.ok) return;
    state.media.rtmp_enabled = !!res.enabled;
    state.media.rtmp_url = res.rtmp_url || '';
    announceState();
  }

  function sendAudioChunk(b64, sampleRate, durationMs = 0, reason = 'chunk') {
    if (DEBUG_CHAT) console.debug('[broadcast.stt] send audio_chunk', { durationMs, reason, bytes_b64: b64.length });
    sendJson(state.chatWs, 'audio_chunk', {
      encoding: 'linear16',
      channels: STT_TARGET_CHANNELS,
      sampleRate,
      durationMs,
      chunkSeq: ++sttChunkSeq,
      reason,
      data: b64,
    });
  }

  function resetSpeechBuffer() {
    sttPcmParts = [];
    sttPcmSamples = 0;
    sttChunkStartedAt = performance.now();
  }

  function flushSpeechBuffer(reason = 'timer') {
    if (!sttPcmSamples || !sttPcmParts.length) return;
    if (!state.chatWs || state.chatWs.readyState !== WebSocket.OPEN) {
      resetSpeechBuffer();
      return;
    }
    const durationMs = Math.round((sttPcmSamples / STT_TARGET_SAMPLE_RATE) * 1000);
    if (durationMs < STT_MIN_CHUNK_MS && reason !== 'stop') return;
    const merged = new Int16Array(sttPcmSamples);
    let offset = 0;
    for (const part of sttPcmParts) {
      merged.set(part, offset);
      offset += part.length;
    }
    sendAudioChunk(int16ToBase64(merged), STT_TARGET_SAMPLE_RATE, durationMs, reason);
    resetSpeechBuffer();
  }

  function floatToInt16(input) {
    const out = new Int16Array(input.length);
    for (let i = 0; i < input.length; i += 1) {
      const s = Math.max(-1, Math.min(1, input[i]));
      out[i] = s < 0 ? Math.round(s * 0x8000) : Math.round(s * 0x7fff);
    }
    return out;
  }

  function downsampleBuffer(input, inputRate, outputRate) {
    if (outputRate === inputRate) return input;
    const ratio = inputRate / outputRate;
    const outLength = Math.max(1, Math.round(input.length / ratio));
    const out = new Float32Array(outLength);
    let outOffset = 0;
    let inOffset = 0;
    while (outOffset < outLength) {
      const nextOffset = Math.min(input.length, Math.round((outOffset + 1) * ratio));
      let acc = 0;
      let count = 0;
      for (let i = inOffset; i < nextOffset; i += 1) {
        acc += input[i];
        count += 1;
      }
      out[outOffset] = count > 0 ? acc / count : 0;
      outOffset += 1;
      inOffset = nextOffset;
    }
    return out;
  }

  function int16ToBase64(samples) {
    const bytes = new Uint8Array(samples.buffer);
    let binary = '';
    const chunkSize = 0x8000;
    for (let i = 0; i < bytes.length; i += chunkSize) {
      binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
    }
    return btoa(binary);
  }

  async function startSpeechCaptureFromMic() {
    stopSpeechCapture();
    if (!state.media.stt_enabled || !state.media.mic_enabled) return;
    const cam = await startCameraStream();
    const track = cam?.getAudioTracks?.()[0];
    if (!track) {
      console.warn('[broadcast.stt] no microphone track available for STT');
      return;
    }

    const sttStream = new MediaStream([track.clone()]);
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) {
      console.warn('[broadcast.stt] AudioContext unavailable');
      return;
    }

    const ctx = new Ctx({ sampleRate: STT_TARGET_SAMPLE_RATE });
    try { await ctx.resume(); } catch (_) {}
    resetSpeechBuffer();
    const source = ctx.createMediaStreamSource(sttStream);
    const processor = ctx.createScriptProcessor(2048, 1, 1);

    processor.onaudioprocess = (event) => {
      if (!state.chatWs || state.chatWs.readyState !== WebSocket.OPEN) return;
      const input = event.inputBuffer.getChannelData(0);
      const reduced = downsampleBuffer(input, ctx.sampleRate, STT_TARGET_SAMPLE_RATE);
      const pcm = floatToInt16(reduced);
      if (!pcm.length) return;
      sttPcmParts.push(pcm);
      sttPcmSamples += pcm.length;
      const elapsedMs = performance.now() - sttChunkStartedAt;
      const bufferedMs = (sttPcmSamples / STT_TARGET_SAMPLE_RATE) * 1000;
      if (elapsedMs >= STT_CHUNK_MS || bufferedMs >= STT_MAX_CHUNK_MS) flushSpeechBuffer('timed_chunk');
    };

    const silent = ctx.createGain();
    silent.gain.value = 0;
    source.connect(processor);
    processor.connect(silent);
    silent.connect(ctx.destination);

    state.speechCtx = ctx;
    state.speechSource = source;
    state.speechProcessor = processor;
    console.info('[broadcast.stt] capture started', { sampleRate: ctx.sampleRate, targetRate: STT_TARGET_SAMPLE_RATE, chunkMs: STT_CHUNK_MS });
  }

  function connectChat() {
    const ws = new WebSocket(`${wsBase}/ws/chat`);
    state.chatWs = ws;
    ws.onopen = () => {
      chatRetryMs = 1200;
      updateConnectivity(true);
      sendJson(ws, 'join', { role: 'broadcaster' });
      startSpeechCaptureFromMic().catch((err) => console.warn('[broadcast.stt] start skipped', err?.message || String(err)));
    };
    ws.onmessage = (ev) => {
      let msg; try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === 'state_sync' || msg.type === 'state_update') applyRoomState(msg.state || {});
      if (msg.type === 'presence') applyPresence(msg);
      if (msg.type === 'ai_status') updateAiStatus(msg.status || 'idle');
      if (['chat', 'attachment'].includes(msg.type)) { appendChat(msg); if (msg.source === 'ai') playAiVoice(msg.voiceUrl || msg.voice); }
      if (msg.type === 'stt_status' && DEBUG_CHAT) console.debug('[broadcast.stt] status', msg);
      if (msg.type === 'web_search_result') {
        renderSearchResults(msg.query || '', msg.result || {});
      }
    };
    ws.onclose = () => {
      updateConnectivity(false);
      stopSpeechCapture();
      setTimeout(connectChat, chatRetryMs);
      chatRetryMs = Math.min(15000, Math.round(chatRetryMs * 1.7));
    };
  }

  function connectSignal() {
    const ws = new WebSocket(`${wsBase}/ws/broadcast`);
    state.signalWs = ws;
    ws.onopen = async () => {
      signalRetryMs = 1200;
      sendJson(ws, 'join', { role: 'broadcaster' });
      try {
        await syncTracks();
        console.info('[broadcast] signal joined with media state', { mediaReady: broadcasterMediaReady, mobile: isProbablyMobile() });
      } catch (err) {
        broadcasterMediaReady = false;
        console.warn('[broadcast] signal join media sync failed', err?.message || String(err));
      }
    };
    ws.onmessage = async (ev) => {
      let msg; try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === 'viewer_joined' && msg.viewerId) {
        console.info('[broadcast] viewer joined', { viewerId: msg.viewerId, mediaReady: broadcasterMediaReady });
        try { await createOfferForViewer(msg.viewerId); } catch (err) { console.warn('[broadcast] create offer failed', { viewerId: msg.viewerId, message: err?.message || String(err) }); }
      }
      if (msg.type === 'request_stream' && msg.viewerId) {
        console.info('[broadcast] viewer requested stream', { viewerId: msg.viewerId, mediaReady: broadcasterMediaReady });
        try { await createOfferForViewer(msg.viewerId); } catch (err) { console.warn('[broadcast] request_stream offer failed', { viewerId: msg.viewerId, message: err?.message || String(err) }); }
      }
      if (msg.type === 'viewer_left' && msg.viewerId) {
        removeViewerPeer(msg.viewerId);
      }
      if (msg.type === 'answer' && msg.viewerId && msg.payload?.sdp) {
        const pc = state.peerConnections[msg.viewerId];
        if (pc) await pc.setRemoteDescription({ type: msg.payload.type || 'answer', sdp: msg.payload.sdp });
      }
      if (msg.type === 'ice-candidate' && msg.viewerId && msg.candidate) {
        const pc = state.peerConnections[msg.viewerId];
        if (pc) {
          try { await pc.addIceCandidate(msg.candidate); } catch (_) {}
        }
      }
      if (msg.type === 'webrtc_answer' && msg.sdp && state.pc) {
        await state.pc.setRemoteDescription({ type: msg.answerType || 'answer', sdp: msg.sdp });
      }
      if (msg.type === 'presence') applyPresence(msg);
      if (msg.type === 'state_sync' || msg.type === 'state_update') applyRoomState(msg.state || {});
    };
    ws.onclose = () => {
      Object.keys(state.peerConnections).forEach(removeViewerPeer);
      setTimeout(connectSignal, signalRetryMs);
      signalRetryMs = Math.min(15000, Math.round(signalRetryMs * 1.7));
    };
  }

  function announceState() {
    // Preserve AI/voice pills; server decides whether to answer.
    state.media.ai_enabled = !!state.media.ai_enabled;
    state.media.tts_enabled = !!state.media.tts_enabled;
    state.media.hear_ai_voice = !!state.media.hear_ai_voice;
    sendJson(state.chatWs, 'toggle_state', { state: state.media });
    sendJson(state.signalWs, 'toggle_state', { state: state.media });
    sendJson(state.signalWs, 'set_media_mode', { camera: state.media.camera_enabled, screen: state.media.screen_enabled, mic: state.media.mic_enabled });
    applyRoomState({ settings: state.media, runtime: { broadcaster_present: true, viewer_count: Number(dom.stWatchers?.textContent || 0) } });
  }

  function sendChatMessage() {
    const text = dom.chatInput?.value?.trim();
    if (!text) return;
    const now = Date.now();
    if (text === lastChatText && (now - lastChatSendAt) < 400) return;
    lastChatText = text;
    lastChatSendAt = now;
    if (DEBUG_CHAT) console.debug('[broadcast.chat] send', { chars: text.length });
    sendJson(state.chatWs, 'chat', { text });
    dom.chatInput.value = '';
  }

  dom.sendBtn?.addEventListener('click', sendChatMessage);
  dom.chatInput?.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); sendChatMessage(); }
  });

  dom.webBtn?.addEventListener('click', () => {
    const query = (dom.chatInput?.value || '').trim();
    if (!query) return;
    if (DEBUG_CHAT) console.debug('[broadcast.chat] google_ai_iframe', { query_len: query.length });
    const googleUrl = googleAiUrl(query);
    try { window.open(googleUrl, '_blank', 'noopener'); } catch (_) {}
    appendGoogleAiSearchFrame(query, true);
  });
  dom.searchCloseBtn?.addEventListener('click', () => dom.searchPane?.classList.remove('open'));

  dom.attachBtn?.addEventListener('click', () => dom.fileInput?.click());
  dom.fileInput?.addEventListener('change', async () => {
    const f = dom.fileInput.files?.[0];
    if (!f) return;
    const fd = new FormData(); fd.append('file', f);
    const res = await fetch('/api/upload', { method: 'POST', body: fd }).then((r) => r.json()).catch(() => ({}));
    const attachment = { url: res.url || '', name: f.name, mime: f.type, size: f.size, kind: /^image\//i.test(f.type || '') ? 'image' : 'file' };
    sendJson(state.chatWs, 'attachment_uploaded', { attachment });
    appendChat({ user: 'you', text: `Uploaded: ${f.name}`, attachment });
    dom.fileInput.value = '';
  });

  function installDoubleTapPill(btn, handler) {
    if (!btn) return;
    btn.addEventListener('click', (ev) => {
      ev.preventDefault();
      const now = Date.now();
      const last = lastSingleTapAt.get(btn) || 0;
      lastSingleTapAt.set(btn, now);
      if (now - last > 420 && btn.classList) {
        btn.classList.add('tap-armed');
        setTimeout(() => btn.classList.remove('tap-armed'), 430);
      }
    });
    btn.addEventListener('dblclick', (ev) => {
      ev.preventDefault();
      lastSingleTapAt.set(btn, 0);
      btn.classList?.remove('tap-armed');
      handler(ev);
    });
  }

  installDoubleTapPill(dom.camBtn, async () => {
    await cycleCameraDevice();
  });
  installDoubleTapPill(dom.screenBtn, async () => {
    if (isProbablyMobile()) {
      console.info('[broadcast] mobile screen pill tap: camera-only path retained');
      await switchToCamera();
      return;
    }
    if (state.media.screen_enabled) await switchToCamera(); else await switchToScreen();
  });
  installDoubleTapPill(dom.micBtn, async () => {
    state.media.mic_enabled = !state.media.mic_enabled;
    await syncTracks();
    if (state.media.mic_enabled && state.media.stt_enabled) await startSpeechCaptureFromMic();
    else stopSpeechCapture();
    announceState();
  });
  installDoubleTapPill(dom.sttBtn, async () => {
    state.media.stt_enabled = !state.media.stt_enabled;
    if (state.media.stt_enabled && state.media.mic_enabled) await startSpeechCaptureFromMic();
    else stopSpeechCapture();
    announceState();
  });
  installDoubleTapPill(dom.ncBtn, async () => {
    state.media.noise_cancel_enabled = !state.media.noise_cancel_enabled;
    if (state.camStream) { state.camStream.getTracks().forEach((t) => t.stop()); state.camStream = null; }
    await syncTracks();
    if (state.media.stt_enabled && state.media.mic_enabled) await startSpeechCaptureFromMic();
    announceState();
  });
  installDoubleTapPill(dom.aiEnableBtn, () => { state.media.ai_enabled = !state.media.ai_enabled; announceState(); });
  installDoubleTapPill(dom.ttsMonBtn, () => { state.media.hear_ai_voice = !state.media.hear_ai_voice; state.media.tts_enabled = state.media.hear_ai_voice; announceState(); });
  installDoubleTapPill(dom.recordBtn, () => { toggleRecording().catch(() => {}); });
  installDoubleTapPill(dom.rtmpBtn, () => { toggleRtmp().catch(() => {}); });
  dom.chatCollapseBtn?.addEventListener('click', () => {
    if (!dom.chatPanel) return;
    dom.chatPanel.classList.toggle('collapsed');
    dom.chatCollapseBtn.textContent = dom.chatPanel.classList.contains('collapsed') ? 'Expand' : 'Collapse';
  });

  if (window.matchMedia && window.matchMedia('(max-width: 980px)').matches && dom.chatPanel) {
    dom.chatPanel.classList.add('collapsed');
    if (dom.chatCollapseBtn) dom.chatCollapseBtn.textContent = 'Expand';
  }
  if (isProbablyMobile()) {
    enforceMobileCameraOnly('boot');
    if (dom.screenBtn) {
      dom.screenBtn.classList.add('disabled');
      dom.screenBtn.title = 'Mobile uses direct camera-only broadcast for stable /watch video.';
    }
  }
  window.addEventListener('pointerdown', () => {
    if (state.media.stt_enabled && state.media.mic_enabled && !state.speechProcessor) {
      startSpeechCaptureFromMic().catch((err) => console.warn('[broadcast.stt] gesture start skipped', err?.message || String(err)));
    }
  }, { passive: true });

  applyRoomState({ settings: state.media, runtime: { broadcaster_present: false, viewer_count: 0 } });
  installWakeLock();
  installMobileVisibilityRecovery();
  connectChat();
  connectSignal();

  // Best effort startup: UI stays ON even if browser prompts for permissions first.
  startCameraStream()
    .then(syncTracks)
    .then(() => { announceState(); startSpeechCaptureFromMic().catch((err) => console.warn('[broadcast.stt] start skipped', err?.message || String(err))); })
    .catch(() => announceState());
})();
