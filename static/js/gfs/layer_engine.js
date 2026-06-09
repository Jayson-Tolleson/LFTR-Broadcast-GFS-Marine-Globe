export class LayerEngine {
  constructor(){
    this.layers = {}
    this.lastTs = performance.now()
    this.latestData = null
  }
  register(name, layer){
    this.layers[name] = { enabled:false, instance:layer }
    if (this.latestData && typeof layer.onData === 'function') {
      try { layer.onData(this.latestData) } catch (err) { console.error('[gfs layers] onData failed', name, err) }
    }
  }
  setData(payload){
    this.latestData = payload || null
    Object.entries(this.layers).forEach(([name, layer]) => {
      if (layer.enabled) {
        try { layer.instance.onData?.(this.latestData) } catch (err) { console.error('[gfs layers] data sync failed', name, err) }
      }
    })
  }
  setEnabled(name, enabled){
    const layer = this.layers[name]
    if(!layer){ console.warn('[gfs layers] missing layer:', name); return false }
    if(layer.enabled === enabled) {
      if(enabled && this.latestData) {
        try { layer.instance.onData?.(this.latestData) } catch (err) { console.error('[gfs layers] onData failed', name, err) }
      }
      return true
    }
    layer.enabled = enabled
    try {
      if(enabled){
        layer.instance.show?.()
        if (this.latestData) layer.instance.onData?.(this.latestData)
      } else {
        layer.instance.hide?.()
      }
    } catch (err) {
      console.error('[gfs layers] toggle failed', name, err)
    }
    return true
  }
  toggle(name){
    const layer = this.layers[name]
    if(!layer){ console.warn('[gfs layers] missing layer:', name); return false }
    return this.setEnabled(name, !layer.enabled)
  }
  update(){
    const now = performance.now()
    const dt = Math.max(0, (now - this.lastTs) / 1000)
    this.lastTs = now
    Object.values(this.layers).forEach((layer) => {
      if(layer.enabled){
        try { layer.instance.update?.(dt) } catch (err) { console.error('[gfs layers] update failed', err) }
      }
    })
  }
}
