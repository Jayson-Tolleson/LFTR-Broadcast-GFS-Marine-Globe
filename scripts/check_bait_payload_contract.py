#!/usr/bin/env python3
from __future__ import annotations
import json, math, sys, urllib.request
from pathlib import Path
from typing import Any


def load_payload(src: str) -> dict[str, Any]:
    if src.startswith('http://') or src.startswith('https://'):
        with urllib.request.urlopen(src, timeout=30) as r:
            ctype = r.headers.get('content-type', '')
            data = r.read().decode('utf-8', 'replace')
        if '<html' in data[:200].lower():
            raise SystemExit(f'not JSON: HTML returned from {src}')
        return json.loads(data)
    return json.loads(Path(src).read_text())


def finite_lat_lng(pt: Any) -> bool:
    if not isinstance(pt, dict): return False
    try:
        lat = float(pt.get('lat'))
        lng = float(pt.get('lng', pt.get('lon')))
    except Exception:
        return False
    return math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180


def polygons(payload: dict[str, Any]) -> list[dict[str, Any]]:
    bait = payload.get('bait') if isinstance(payload.get('bait'), dict) else {}
    rows: list[Any] = []
    for key in ('polygons','zones'):
        if isinstance(payload.get(key), list): rows.extend(payload[key])
    for key in ('polygons','inner_polygons','outer_polygons','core_polygons'):
        if isinstance(bait.get(key), list): rows.extend(bait[key])
    out=[]
    seen=set()
    for p in rows:
        if not isinstance(p, dict): continue
        ident=id(p)
        if ident in seen: continue
        seen.add(ident)
        out.append(p)
    return out


def validate(payload: dict[str, Any]) -> dict[str, int]:
    polys=polygons(payload)
    invalid=0; path_points=0
    for poly in polys:
        path=poly.get('path') or []
        if not isinstance(path, list) or len(path) < 3:
            invalid += 1; continue
        bad=sum(1 for pt in path if not finite_lat_lng(pt))
        invalid += bad
        path_points += len(path)
    diag=payload.get('diagnostics') if isinstance(payload.get('diagnostics'), dict) else {}
    valid=int(payload.get('valid_ocean_point_count') or payload.get('water_mask_count') or diag.get('valid_ocean_point_count') or diag.get('water_mask_count') or 0)
    if payload.get('ok') is True and valid > 0 and not polys:
        raise SystemExit('bait ok=true with valid ocean/SST cells but zero polygons')
    if invalid:
        raise SystemExit(f'bait invalid coordinates/path entries: {invalid}')
    return {'polygons': len(polys), 'path_points': path_points, 'invalid': invalid, 'valid_ocean_points': valid}


def self_test() -> None:
    p={'ok': True, 'valid_ocean_point_count': 1, 'polygons': [{'path': [{'lat': 32, 'lng': -118}, {'lat': 32.1, 'lng': -118}, {'lat': 32.1, 'lng': -117.9}]}]}
    assert validate(p)['polygons'] == 1
    try:
        validate({'ok': True, 'valid_ocean_point_count': 3, 'polygons': []})
    except SystemExit:
        pass
    else:
        raise AssertionError('zero polygon payload should fail')
    print('PASS bait contract self-test')

if __name__ == '__main__':
    if len(sys.argv) == 2 and sys.argv[1] == '--self-test':
        self_test(); raise SystemExit(0)
    if len(sys.argv) != 2:
        print('usage: check_bait_payload_contract.py <payload.json|url>', file=sys.stderr); raise SystemExit(2)
    stats=validate(load_payload(sys.argv[1]))
    print(json.dumps({'ok': True, **stats}, indent=2))
