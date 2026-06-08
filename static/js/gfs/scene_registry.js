// Persistent scene-object registry for heavy 3D layers.
// Renderers can use this to atomically swap only changed objects while keeping a
// layer-owned disposer for pill-off cleanup. It prevents the old blanking cycle:
// clear everything -> fetch/build -> append everything.

const registries = new WeakMap();

function mapRegistry(mapEl) {
  let reg = registries.get(mapEl);
  if (!reg) {
    reg = new Map();
    registries.set(mapEl, reg);
  }
  return reg;
}

function nodeKey(node, index, layerName) {
  try {
    return String(node?.dataset?.gfsSceneKey || node?.getAttribute?.('data-gfs-scene-key') || node?.id || `${layerName}:${index}`);
  } catch (_) {
    return `${layerName}:${index}`;
  }
}

export function attachSceneKey(node, key) {
  if (!node || !key) return node;
  try { node.setAttribute?.('data-gfs-scene-key', String(key)); } catch (_) {}
  return node;
}

export function reconcileSceneLayer(mapEl, layerName, nodes, { disposeNode, fadeMs = 1600 } = {}) {
  if (!mapEl || !layerName) return () => {};
  const layerNodes = Array.isArray(nodes) ? nodes.filter(Boolean) : [];
  const reg = mapRegistry(mapEl);
  const previous = reg.get(layerName) || new Map();
  const next = new Map();
  const frag = document.createDocumentFragment();

  layerNodes.forEach((node, index) => {
    const key = nodeKey(node, index, layerName);
    attachSceneKey(node, key);
    next.set(key, node);
    if (previous.get(key) === node && node.isConnected) return;
    frag.append(node);
  });
  if (frag.childNodes.length) mapEl.append(frag);

  previous.forEach((oldNode, key) => {
    if (next.has(key)) return;
    try {
      oldNode.setAttribute?.('data-gfs-fading-out', 'true');
      if (oldNode.style) oldNode.style.pointerEvents = 'none';
    } catch (_) {}
    const remove = () => {
      try { disposeNode?.(oldNode); } catch (_) {}
      try { oldNode.remove?.(); } catch (_) {}
    };
    if (fadeMs > 0 && oldNode.style) {
      try { oldNode.style.transition = `opacity ${fadeMs}ms ease`; oldNode.style.opacity = '0'; } catch (_) {}
      setTimeout(remove, fadeMs + 80);
    } else {
      remove();
    }
  });

  reg.set(layerName, next);
  const disposer = () => {
    const rows = reg.get(layerName) || new Map();
    rows.forEach((node) => {
      try { disposeNode?.(node); } catch (_) {}
      try { node.remove?.(); } catch (_) {}
    });
    reg.delete(layerName);
  };
  disposer.__gfsKeepExisting = true;
  disposer.__gfsDidRender = true;
  return disposer;
}
