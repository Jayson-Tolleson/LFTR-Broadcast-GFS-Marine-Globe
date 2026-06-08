globalThis.window = globalThis;
globalThis.localStorage = { getItem(){return null}, setItem(){}, removeItem(){} };
globalThis.document = {
  documentElement:{style:{setProperty(){}}},
  querySelector(){return null}, querySelectorAll(){return []}, getElementById(){return null},
  createElement(tag){return {tagName:tag, style:{}, classList:{add(){},remove(){},toggle(){}}, setAttribute(){}, appendChild(){}, remove(){}, addEventListener(){}, dataset:{}, children:[]}},
  addEventListener(){}, body:{appendChild(){}}
};
Object.defineProperty(globalThis, 'navigator', {value:{userAgent:''}, configurable:true});
globalThis.customElements={get(){return false}, define(){}};
globalThis.HTMLElement=class{};
globalThis.requestAnimationFrame=(cb)=>setTimeout(cb,0);
globalThis.cancelAnimationFrame=(id)=>clearTimeout(id);
globalThis.google={maps:{maps3d:{}}};
import('./static/js/gfs/main.js').then(()=>console.log('IMPORT_OK')).catch(e=>{console.error('IMPORT_FAIL', e && e.stack || e); process.exit(1);});
