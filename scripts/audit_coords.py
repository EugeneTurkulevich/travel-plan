"""Audit POI coordinates in places/XXXX.md against OSM Nominatim.

For each POI in `## Цікаві POI` table:
  1. Build search query from POI name (Latin form preferred, e.g. "Peleș Castle")
     + city name from FILE_TO_CITY map.
  2. Query OSM Nominatim.
  3. Compare current coords to OSM result; flag if distance > 250 m.

Output: a list of (file, poi, current, osm, distance_m) for review.

This DOES NOT modify files — just produces a report.
"""
import json
import re
import time
import urllib.parse
import urllib.request
from math import asin, cos, radians, sin, sqrt
from pathlib import Path

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trip_ctx import paths
CTX = paths()

ROOT = CTX.places_dir          # був абсолютний шлях, якого не існує
UA = 'travel2026-coord-auditor/0.1 (eugene.turkulevich@gmail.com)'
THRESHOLD_M = 300  # flag mismatches > 300 m

# city context for disambiguation (English form for Nominatim)
FILE_TO_CITY = {
    '0100.md': 'Kyiv',
    '0200.md': 'Chernivtsi',
    '0300.md': 'Sucevița',
    '0400.md': 'Voroneț',
    '0500.md': 'Sinaia',
    '0550.md': 'Brașov',
    '0600.md': 'Ruse',
    '0700.md': 'Ivanovo Bulgaria',
    '0800.md': 'Veliko Tarnovo',
    '0900.md': 'Plovdiv',
    '1000.md': 'Kavala',
    '1100.md': 'Philippi',
    '1200.md': 'Thessaloniki',
    '1300.md': 'Vergina',
    '1500.md': 'Litochoro',
    '1600.md': 'Thermopylae',
    '1650.md': 'Galaxidi',
    '1700.md': 'Athens',
    '1710.md': 'Corinth',
    '1720.md': 'Nafplio',
    '1730.md': 'Patras',
    '1740.md': 'Rio Antirrio',
    '1750.md': 'Nafpaktos',
    '1760.md': 'Mesolongi',
    '1800.md': 'Delphi',
    '1900.md': 'Arachova',
    '2200.md': 'Meteora',
    '2300.md': 'Metsovo',
    '2400.md': 'Ioannina',
    '2500.md': 'Kastoria',
    '2700.md': 'Skopje',
    '2750.md': 'Matka Skopje',
    '2800.md': 'Rila Monastery',
    '2900.md': 'Sofia Bulgaria',
    '3000.md': 'Vidin',
    '3200.md': 'Horezu',
    '3300.md': 'Sibiu',
    '3400.md': 'Alba Iulia',
    '3500.md': 'Maramureș',
    '3600.md': 'Rakhiv',
}


def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * R * asin(sqrt(a))


def osm_search(query: str) -> tuple[float, float] | None:
    url = 'https://nominatim.openstreetmap.org/search?' + urllib.parse.urlencode({
        'q': query, 'format': 'json', 'limit': '1',
    })
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
        if data:
            return float(data[0]['lat']), float(data[0]['lon'])
    except Exception:
        return None
    return None


def latin_name(cell: str) -> str:
    """Extract latin/english part from POI name like 'Замок Пелеш (Peleș Castle)'."""
    m = re.search(r'\(([^()]+)\)', cell)
    if m:
        alt = m.group(1).strip()
        if re.search(r'[A-Za-zĂăÂâÎîȘșȚțČčŠšŽžĞğÖöÜüА-Яа-я]', alt):
            return alt
    return cell


def parse_pois(md: str):
    m = re.search(r'## Цікаві POI\n.*?(?=\n##|\Z)', md, re.S)
    if not m:
        return []
    out = []
    for r in m.group(0).split('\n'):
        if not r.startswith('|') or re.match(r'^\|\s*Назва|^\|\s*:', r):
            continue
        cells = [c.strip() for c in r.strip('|').split('|')]
        if len(cells) < 4:
            continue
        name = re.sub(r'\*\*', '', cells[0]).strip()
        coord = cells[2].strip().strip('`')
        lm = re.match(r'\s*([\d.]+)\s*,\s*([\d.]+)\s*', coord)
        if lm:
            out.append((name, float(lm.group(1)), float(lm.group(2))))
    return out


def main():
    issues = []
    files = sorted(p for p in ROOT.glob('????.md')
                   if p.name in FILE_TO_CITY)
    for f in files:
        city = FILE_TO_CITY[f.name]
        pois = parse_pois(f.read_text())
        if not pois:
            continue
        print(f'\n=== {f.name} ({city}) ===')
        for name, lat, lon in pois:
            nlat = latin_name(name)
            q = f'{nlat} {city}'
            osm = osm_search(q)
            time.sleep(1.1)  # Nominatim 1 req/sec policy
            if osm is None:
                print(f'  ? {name[:45]:45s} → NO OSM RESULT for "{q[:60]}"')
                continue
            d = haversine_m(lat, lon, osm[0], osm[1])
            tag = '⚠️' if d > THRESHOLD_M else 'ok'
            print(f'  {tag} {name[:45]:45s} cur=({lat:.4f},{lon:.4f}) osm=({osm[0]:.4f},{osm[1]:.4f}) {d:6.0f}m')
            if d > THRESHOLD_M:
                issues.append((f.name, name, (lat, lon), osm, d))

    print(f'\n=== SUMMARY: {len(issues)} mismatches > {THRESHOLD_M}m ===')
    for fname, name, cur, osm, d in issues:
        print(f'  {fname}: {name[:35]:35s} {d:6.0f}m  cur=({cur[0]:.4f},{cur[1]:.4f}) → osm=({osm[0]:.4f},{osm[1]:.4f})')


if __name__ == '__main__':
    main()
