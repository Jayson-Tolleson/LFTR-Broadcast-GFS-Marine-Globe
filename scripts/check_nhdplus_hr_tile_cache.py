#!/usr/bin/env python3
import gzip, json
from pathlib import Path
ROOT = Path('static/data/nhdplus_hr/tiles')

def main():
    idx = ROOT / 'index.json'
    out = {"status": "ok", "mode": "single_viewport_runtime_tile_cache", "root": str(ROOT), "index": str(idx), "installed": idx.exists(), "tiles": 0, "bytes_gz": 0, "discovered_gz_tiles": 0, "cache_policy": "31-day viewport runtime tile cache; active LOD only; read-only unless /build-cache is called"}
    if idx.exists():
        try:
            data = json.loads(idx.read_text())
            out['tiles'] = len(data.get('tiles') or []) if isinstance(data, dict) else 0
            out['lods'] = sorted({str(t.get('lod') or t.get('tier') or '') for t in data.get('tiles', []) if isinstance(t, dict)})
        except Exception as exc:
            out['status'] = 'index_read_failed'; out['error'] = str(exc)
    if ROOT.exists():
        for p in ROOT.rglob('*.json.gz'):
            out['discovered_gz_tiles'] += 1
            try: out['bytes_gz'] += p.stat().st_size
            except Exception: pass
    print(json.dumps(out, indent=2))
    return 0 if out['installed'] or out['discovered_gz_tiles'] else 1
if __name__ == '__main__':
    raise SystemExit(main())
