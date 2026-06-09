#!/usr/bin/env python3
from __future__ import annotations
import json, math, sys, urllib.request
from pathlib import Path
from typing import Any


def load_payload(src: str) -> dict[str, Any]:
    if src.startswith('http://') or src.startswith('https://'):
        with urllib.request.urlopen(src, timeout=30) as r:
            data = r.read().decode('utf-8', 'replace')
        if '<html' in data[:200].lower():
            raise SystemExit(f'not JSON: HTML returned from {src}')
        return json.loads(data)
    return json.loads(Path(src).read_text())


def finite(value: Any) -> float | None:
    try: f=float(value)
    except Exception: return None
    return f if math.isfinite(f) else None


def safety_color(wave_ft: Any) -> str:
    ft=finite(wave_ft)
    if ft is None: return 'gray'
    if ft <= 3: return 'green'
    if ft <= 4: return 'yellow'
    return 'red'


def validate(payload: dict[str, Any]) -> dict[str, int]:
    boats=payload.get('boats') if isinstance(payload.get('boats'), list) else []
    invalid=0; wave_null=0
    for boat in boats:
        lat=finite(boat.get('lat')); lon=finite(boat.get('lon', boat.get('lng')))
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180): invalid += 1
        waves=boat.get('waves') if isinstance(boat.get('waves'), dict) else {}
        wh=finite(waves.get('sigHeightFt', waves.get('waveHeightFt')))
        if wh is None: wave_null += 1
        color=((boat.get('safety') or {}) if isinstance(boat.get('safety'), dict) else {}).get('color')
        expected=safety_color(wh)
        if wh is not None and color not in {expected, None}:
            raise SystemExit(f'boat safety color {color!r} does not match threshold {expected!r} for {wh} ft')
    diag=payload.get('diagnostics') if isinstance(payload.get('diagnostics'), dict) else {}
    cells=int(payload.get('current_zone_points_count') or len(payload.get('points') or []) or len(payload.get('ocean_points') or []) or 0)
    stations=int(((diag.get('boater') or {}) if isinstance(diag.get('boater'), dict) else {}).get('stations_considered') or 0)
    if payload.get('ok') is True and (cells > 0 or stations > 0) and not boats:
        raise SystemExit('boats ok=true with ocean/station data but zero boats')
    if invalid:
        raise SystemExit(f'invalid boat coordinates: {invalid}')
    return {'boats': len(boats), 'invalid': invalid, 'wave_null': wave_null, 'ocean_cells': cells}


def self_test() -> None:
    assert safety_color(3) == 'green'
    assert safety_color(3.1) == 'yellow'
    assert safety_color(4) == 'yellow'
    assert safety_color(4.01) == 'red'
    assert safety_color(float('nan')) == 'gray'
    p={'ok': True, 'boats': [{'lat': 32, 'lon': -118, 'waves': {'sigHeightFt': float('nan')}, 'safety': {'color': 'gray'}}]}
    assert validate(p)['boats'] == 1
    print('PASS boats contract self-test')

if __name__ == '__main__':
    if len(sys.argv) == 2 and sys.argv[1] == '--self-test': self_test(); raise SystemExit(0)
    if len(sys.argv) != 2:
        print('usage: check_boats_payload_contract.py <payload.json|url>', file=sys.stderr); raise SystemExit(2)
    print(json.dumps({'ok': True, **validate(load_payload(sys.argv[1]))}, indent=2))
