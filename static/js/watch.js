(() => {
  const wsProto = location.protocol === 'https:' ? 'wss' : 'ws';
  const wsBase = `${wsProto}://${location.host}`;
  const room = (new URLSearchParams(location.search).get('room') || 'default').trim() || 'default';
  const WATCH_VIDEO_TARGET = { width: 1920, height: 1080, fpsIdeal: 30, fpsMin: 15, fpsMax: 60 };


  const dom = {
    video: document.getElementById('remoteVideo'),
    standby: document.getElementById('standby'),
    statusText: document.getElementById('statusText'),
    conn: document.getElementById('conn'),
    mode: document.getElementById('mode'),
    ai: document.getElementById('ai'),
    label: document.getElementById('label'),
    watchers: document.getElementById('watchers'),
    joinOverlay: document.getElementById('joinStreamOverlay'),
    joinBtn: document.getElementById('joinStreamBtn'),
    joinHint: document.getElementById('joinHint'),
    chatDock: document.getElementById('chatDock'),
    chatCollapseBtn: document.getElementById('chatCollapseBtn'),
    chat: document.getElementById('chat'),
    chatInput: document.getElementById('chatInput'),
    sendBtn: document.getElementById('sendBtn'),
    attachBtn: document.getElementById('attachBtn'),
    fileInput: document.getElementById('file'),
    webBtn: document.getElementById('webBtn'),
    searchCloseBtn: document.getElementById('searchCloseBtn'),
    searchPane: document.getElementById('searchPane'),
    searchResults: document.getElementById('searchResults'),
    aiEnableBtn: document.getElementById('aiEnableBtn'),
    ttsMonBtn: document.getElementById('ttsMonBtn'),
  };
  const v = dom.video;

  let ws = null;
  let pc = null;
  let reconnectDelayMs = 1000;
  let viewerId = `watch-${Math.random().toString(36).slice(2, 10)}`;
  let broadcasterPresent = false;
  let requestPending = false;
  let hasRequestedStream = false;
  let retryTimer = null;
  let requestTimeout = null;
  let aiEnabled = false;
  let hearAiVoice = false;
  let remoteMediaStream = new MediaStream();
  let wakeLockSentinel = null;
  const DEBUG_CHAT = false;
  let lastChatSendAt = 0;
  let lastChatText = '';


  function setStatus(text) {
    if (dom.statusText) dom.statusText.textContent = text;
  }

  function showJoinOverlay(_show, _reason = '') {
    // No join/audio obstacle on /watch. Browser policies may keep audio muted,
    // but video should start immediately and controls let the viewer unmute.
    if (dom.joinOverlay) dom.joinOverlay.style.display = 'none';
  }

  function setStandby(show, reason = 'Waiting for live stream…') {
    if (dom.standby) dom.standby.style.display = show ? 'block' : 'none';
    if (show) setStatus(reason);
  }

  async function playAiVoice(url) {
    if (!url || !hearAiVoice) return;
    try {
      const audio = new Audio(url);
      audio.volume = 0.9;
      await audio.play();
    } catch (_) {}
  }

  function updateAiButtons() {
    if (dom.aiEnableBtn) dom.aiEnableBtn.textContent = `AI: ${aiEnabled ? 'on' : 'off'}`;
    if (dom.ttsMonBtn) dom.ttsMonBtn.textContent = `AI voice: ${hearAiVoice ? 'on' : 'off'}`;
    if (dom.ai) dom.ai.textContent = aiEnabled ? 'AI idle' : 'AI off';
  }

  function sendAiState() {
    sendJson('toggle_state', { state: { ai_enabled: aiEnabled, tts_enabled: hearAiVoice, hear_ai_voice: hearAiVoice } });
    updateAiButtons();
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

  function appendChat(entry) {
    if (!dom.chat) return;
    const wrap = document.createElement('div');
    wrap.className = 'entry';
    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.textContent = `${entry.user || entry.sender || 'room'} • ${new Date().toLocaleTimeString()}`;
    const body = document.createElement('div');
    body.textContent = entry.text || '';
    wrap.append(meta, body);
    if (entry.attachment?.url || entry.attachment?.path) appendIframeAttachment(wrap, entry.attachment);
    dom.chat.appendChild(wrap);
    dom.chat.scrollTop = dom.chat.scrollHeight;
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

  function sendJson(type, extra = {}) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    ws.send(JSON.stringify({ type, room, clientId: viewerId, role: 'viewer', ...extra }));
    return true;
  }

  async function acquireWakeLock(reason = 'init') {
    if (!('wakeLock' in navigator)) return;
    if (document.visibilityState !== 'visible') return;
    try {
      if (wakeLockSentinel && !wakeLockSentinel.released) return;
      wakeLockSentinel = await navigator.wakeLock.request('screen');
      wakeLockSentinel.addEventListener('release', () => {
        console.info('[watch] wake lock released', { reason });
        if (document.visibilityState === 'visible') {
          setTimeout(() => acquireWakeLock('released').catch(() => {}), 250);
        }
      }, { once: true });
      console.info('[watch] wake lock active', { reason });
    } catch (err) {
      console.info('[watch] wake lock unavailable', { reason, message: err?.message || String(err) });
    }
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


  function setLiveMutedAutoplay() {
    if (!v) return;
    v.playsInline = true;
    v.autoplay = true;
    v.muted = true;
    v.defaultMuted = true;
    v.controls = true;
    v.width = WATCH_VIDEO_TARGET.width;
    v.height = WATCH_VIDEO_TARGET.height;
  }

  async function tryPlay(reason) {
    if (!dom.video) return;
    setLiveMutedAutoplay();
    showJoinOverlay(false);
    try {
      await dom.video.play();
      console.info('[watch] playback started', { reason, muted: dom.video.muted, controls: dom.video.controls });
    } catch (err) {
      console.warn('[watch] autoplay video play retry pending', { reason, message: err?.message || String(err) });
      // No blocking overlay. Keep controls visible and retry when tracks/state change.
      setTimeout(() => { dom.video?.play?.().catch(() => {}); }, 500);
    }
  }

  function requestStream(force = false) {
    if (force) hasRequestedStream = false;
    scheduleStreamRequest(0);
  }

  function scheduleStreamRequest(delayMs = 350) {
    if (retryTimer) clearTimeout(retryTimer);
    retryTimer = setTimeout(() => {
      retryTimer = null;
      if (!broadcasterPresent || requestPending || hasRequestedStream) return;
      requestPending = sendJson('request_stream');
      hasRequestedStream = requestPending || hasRequestedStream;
      if (requestPending) {
        if (requestTimeout) clearTimeout(requestTimeout);
        requestTimeout = setTimeout(() => {
          if (!requestPending || !broadcasterPresent) return;
          requestPending = false;
          hasRequestedStream = false;
          console.info('[watch] request_stream timeout; retrying');
          scheduleStreamRequest(250);
        }, 3200);
      }
      console.info('[watch] request_stream sent', { room, viewerId, requestPending });
    }, delayMs);
  }

  async function iceServers() {
    try {
      const r = await fetch('/webrtc/ice-config');
      const j = await r.json();
      return j.iceServers || [{ urls: 'stun:stun.l.google.com:19302' }];
    } catch {
      return [{ urls: 'stun:stun.l.google.com:19302' }];
    }
  }

  async function ensurePeerConnection(reset = false) {
    if (pc && !reset) return pc;
    if (pc && reset) {
      try { pc.close(); } catch (_) {}
      pc = null;
    }
    pc = new RTCPeerConnection({ iceServers: await iceServers() });
    try {
      pc.addTransceiver('video', { direction: 'recvonly' });
      pc.addTransceiver('audio', { direction: 'recvonly' });
    } catch (_) {}
    pc.ontrack = async (event) => {
      const incomingStream = event.streams?.[0] || null;
      if (incomingStream) {
        remoteMediaStream = incomingStream;
      } else if (event.track && !remoteMediaStream.getTracks().includes(event.track)) {
        remoteMediaStream.addTrack(event.track);
      }
      if (dom.video.srcObject !== remoteMediaStream) dom.video.srcObject = remoteMediaStream;
      setStandby(false);
      dom.mode && (dom.mode.textContent = 'LIVE');
      const settings = event.track?.getSettings?.() || {};
      console.info('[watch] remote track attached', {
        kind: event.track?.kind || 'unknown',
        readyState: event.track?.readyState || 'unknown',
        muted: !!event.track?.muted,
        width: settings.width,
        height: settings.height,
        frameRate: settings.frameRate,
        streamVideoTracks: remoteMediaStream.getVideoTracks().length,
        streamAudioTracks: remoteMediaStream.getAudioTracks().length,
        target: WATCH_VIDEO_TARGET,
      });
      event.track?.addEventListener?.('unmute', () => console.info('[watch] remote track unmuted', { kind: event.track.kind }), { once: true });
      event.track?.addEventListener?.('mute', () => {
        console.warn('[watch] remote track muted', { kind: event.track.kind });
        if (event.track.kind === 'video') setTimeout(() => requestStream(true), 1800);
      });
      event.track?.addEventListener?.('ended', () => {
        console.warn('[watch] remote track ended', { kind: event.track.kind });
        if (event.track.kind === 'video') requestStream(true);
      }, { once: true });
      await tryPlay('remote_track_attach');
    };
    pc.onicecandidate = (e) => {
      if (!e.candidate) return;
      sendJson('ice-candidate', { viewerId, candidate: e.candidate });
      sendJson('webrtc_ice', { candidate: e.candidate });
    };
    pc.onconnectionstatechange = () => {
      const state = pc?.connectionState || 'unknown';
      console.info('[watch] pc connection state', { state });
      if (state === 'failed' || state === 'disconnected' || state === 'closed') {
        requestPending = false;
        if (requestTimeout) { clearTimeout(requestTimeout); requestTimeout = null; }
        hasRequestedStream = false;
        setStandby(true, 'Reconnecting stream…');
        if (broadcasterPresent) scheduleStreamRequest(500);
      }
    };
    return pc;
  }

  async function onOffer(payload, sourceType) {
    if (!payload?.sdp) return;
    console.info('[watch] offer received', { room, viewerId, sourceType });
    requestPending = false;
    hasRequestedStream = false;
    const localPc = await ensurePeerConnection(true);
    remoteMediaStream = new MediaStream();
    if (dom.video) dom.video.srcObject = remoteMediaStream;
    await localPc.setRemoteDescription(payload);
    console.info('[watch] remote description set', { transceivers: localPc.getTransceivers?.().map((t) => ({ mid: t.mid, direction: t.direction, currentDirection: t.currentDirection, receiverTrack: t.receiver?.track?.kind })) || [] });
    const answer = await localPc.createAnswer();
    await localPc.setLocalDescription(answer);
    sendJson('answer', { viewerId, sdp: answer.sdp, type: answer.type });
    sendJson('webrtc_answer', { sdp: answer.sdp, type: answer.type });
    console.info('[watch] answer sent', { viewerId });
  }

  async function onServerMessage(msg) {
    if (msg.type === 'connected' && msg.clientId) {
      viewerId = String(msg.clientId);
      console.info('[watch] viewer websocket connected', { room, viewerId });
      return;
    }
    if (msg.type === 'state_sync' || msg.type === 'state_update') {
      const st = msg.state || {};
      broadcasterPresent = !!st.runtime?.broadcaster_present;
      if (dom.watchers) dom.watchers.textContent = `watchers ${st.runtime?.viewer_count ?? 0}`;
      if (dom.ai) dom.ai.textContent = 'AI removed';
      hearAiVoice = false;
      const present = broadcasterPresent;
      if (present) {
        requestStream();
      }
      if (present && !requestPending) {
        requestStream();
      }
      return;
    }
    if (msg.type === 'presence') {
      broadcasterPresent = !!msg.broadcaster_present;
      if (dom.watchers) dom.watchers.textContent = `watchers ${msg.viewer_count ?? 0}`;
      if (broadcasterPresent) {
        dom.mode && (dom.mode.textContent = 'LIVE');
        scheduleStreamRequest(100);
      } else {
        dom.mode && (dom.mode.textContent = 'OFFLINE');
        setStandby(true, 'Waiting for broadcaster…');
        requestPending = false;
      }
      return;
    }
    if (msg.type === 'stream_started') {
      broadcasterPresent = true;
      requestStream(true);
      return;
    }
    if (msg.type === 'broadcaster-start') {
      broadcasterPresent = true;
      requestStream(true);
      return;
    }
    if (msg.type === 'broadcaster-stop') {
      broadcasterPresent = false;
      requestPending = false;
      if (requestTimeout) { clearTimeout(requestTimeout); requestTimeout = null; }
      hasRequestedStream = false;
      setStandby(true, 'Broadcast ended');
      showJoinOverlay(false);
      return;
    }
    if (msg.type === 'ai_status' && dom.ai) {
      dom.ai.textContent = aiEnabled ? `AI ${msg.status || 'idle'}` : 'AI off';
      return;
    }
    if (msg.type === 'stage_state') {
      const p = msg.payload || {};
      if (dom.label) dom.label.textContent = p.label || 'PUBLIC ACCESS';
      if (p.mode === 'upload' && p.latestUploadUrl) {
        if (pc) { try { pc.close(); } catch (_) {} pc = null; }
        dom.video.srcObject = null;
        dom.video.src = p.latestUploadUrl;
        setLiveMutedAutoplay();
        await tryPlay('fallback_upload');
        dom.mode && (dom.mode.textContent = 'LATEST UPLOAD');
        setStandby(false);
      }
      return;
    }
    if (msg.type === 'chat' || msg.type === 'attachment') {
      appendChat(msg);
      if (msg.source === 'ai') playAiVoice(msg.voiceUrl || msg.voice);
      return;
    }
    if (msg.type === 'web_search_result') {
      renderSearchResults(msg.query || '', msg.result || {});
      return;
    }
    if (msg.type === 'waiting' || msg.type === 'error') {
      if (msg.message === 'stream_offline' || msg.message === 'no_broadcaster') {
        requestPending = false;
        if (requestTimeout) { clearTimeout(requestTimeout); requestTimeout = null; }
        hasRequestedStream = false;
        setStandby(true, msg.message === 'stream_offline' ? 'Broadcaster connected, waiting for media…' : 'Waiting for broadcaster…');
      }
      return;
    }
    if (msg.type === 'offer' || msg.type === 'watch_offer' || msg.type === 'webrtc_offer') {
      await onOffer(msg.payload, msg.type);
      return;
    }
    if (msg.type === 'ice-candidate' || msg.type === 'webrtc_ice') {
      const candidate = msg.candidate || msg.payload?.candidate;
      if (!candidate) return;
      await ensurePeerConnection();
      try {
        await pc.addIceCandidate(candidate);
        console.info('[watch] ice received', { viewerId, type: msg.type });
      } catch (err) {
        console.warn('[watch] ice add failed', { message: err?.message || String(err) });
      }
    }
  }

  function connect() {
    const url = `${wsBase}/ws/watch`;
    ws = new WebSocket(url);
    dom.conn && (dom.conn.textContent = 'connecting');

    ws.onopen = () => {
      reconnectDelayMs = 1000;
      dom.conn && (dom.conn.textContent = 'connected');
      requestPending = false;
      if (requestTimeout) { clearTimeout(requestTimeout); requestTimeout = null; }
      hasRequestedStream = false;
      setStandby(true, 'Waiting for live stream…');
      setLiveMutedAutoplay();
      sendJson('join');
      requestStream();
    };

    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      onServerMessage(msg).catch((err) => console.warn('[watch] message handling failed', err));
    };

    ws.onerror = (err) => console.warn('[watch] websocket error', { room, err });

    ws.onclose = (ev) => {
      dom.conn && (dom.conn.textContent = 'reconnecting');
      requestPending = false;
      hasRequestedStream = false;
      setStandby(true, 'Reconnecting viewer socket…');
      console.warn('[watch] websocket disconnected', { code: ev.code, reason: ev.reason, reconnectDelayMs });
      setTimeout(connect, reconnectDelayMs);
      reconnectDelayMs = Math.min(20000, Math.round(reconnectDelayMs * 1.8));
    };
  }

  dom.chatCollapseBtn?.addEventListener('click', () => {
    if (!dom.chatDock) return;
    dom.chatDock.classList.toggle('collapsed');
    dom.chatCollapseBtn.textContent = dom.chatDock.classList.contains('collapsed') ? 'Expand' : 'Collapse';
  });

  function sendChatMessage() {
    const text = (dom.chatInput?.value || '').trim();
    if (!text) return;
    const now = Date.now();
    if (text === lastChatText && (now - lastChatSendAt) < 400) return;
    lastChatText = text;
    lastChatSendAt = now;
    if (DEBUG_CHAT) console.debug('[watch.chat] send', { chars: text.length });
    sendJson('chat', { text });
    dom.chatInput.value = '';
  }

  dom.sendBtn?.addEventListener('click', sendChatMessage);

  dom.chatInput?.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && !ev.shiftKey) {
      ev.preventDefault();
      sendChatMessage();
    }
  });

  dom.attachBtn?.addEventListener('click', () => dom.fileInput?.click());
  dom.fileInput?.addEventListener('change', async () => {
    const f = dom.fileInput.files?.[0];
    if (!f) return;
    const fd = new FormData();
    fd.append('file', f, f.name);
    const uploadType = /^image\//i.test(f.type || '') ? 'image' : 'location_video';
    fd.append('upload_type', uploadType);
    try {
      const r = await fetch('/api/upload', { method: 'POST', body: fd });
      const payload = await r.json();
      const attachment = { ...payload, name: f.name, mime: f.type, size: f.size, kind: /^image\//i.test(f.type || '') ? 'image' : 'file' };
      sendJson('attachment', { attachment });
      sendJson('attachment_uploaded', { attachment });
      appendChat({ user: 'you', text: `Uploaded: ${f.name}`, attachment });
    } catch (err) {
      appendChat({ user: 'system', text: `Upload failed: ${err?.message || String(err)}` });
    } finally {
      dom.fileInput.value = '';
    }
  });

  dom.webBtn?.addEventListener('click', () => {
    const query = (dom.chatInput?.value || '').trim();
    if (!query) return;
    if (DEBUG_CHAT) console.debug('[watch.chat] google_ai_iframe', { query_len: query.length });
    const googleUrl = googleAiUrl(query);
    try { window.open(googleUrl, '_blank', 'noopener'); } catch (_) {}
    appendGoogleAiSearchFrame(query, true);
  });

  dom.aiEnableBtn?.addEventListener('click', () => { aiEnabled = !aiEnabled; sendAiState(); });
  dom.ttsMonBtn?.addEventListener('click', () => { hearAiVoice = !hearAiVoice; sendAiState(); });
  dom.searchCloseBtn?.addEventListener('click', () => dom.searchPane?.classList.remove('open'));
  updateAiButtons();

  setLiveMutedAutoplay();
  installWakeLock();
  connect();
})();
