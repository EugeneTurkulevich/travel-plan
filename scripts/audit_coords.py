"""Audit POI coordinates in library place cards against OSM Nominatim.

For each POI in `## Цікаві POI` table:
  1. Build search query from POI name (Latin form preferred, e.g. "Peleș Castle")
     + city name derived from the card's own H1 and **Код** field.
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
UA = 'travel-plan-coord-auditor/0.2 (contact: owner of this repo clone)'
THRESHOLD_M = 300  # flag mismatches > 300 m
PLACE_META_SKIP = {'country_info.md', 'practical_info.md', 'CATALOG.md'}

# Універсальний факт (не дані поїздки): код країни ISO 3166-1 → англійська
# назва, потрібна лише щоб дати Nominatim запит англійською.
COUNTRY_EN = {
    'UA': 'Ukraine', 'RO': 'Romania', 'BG': 'Bulgaria', 'GR': 'Greece',
    'MK': 'North Macedonia', 'RS': 'Serbia', 'HU': 'Hungary', 'PL': 'Poland',
    'AL': 'Albania', 'MD': 'Moldova',
}


def card_city_context(card_text):
    """H1 картки (+ **Код**) → назва для дизамбігуації Nominatim-запиту.

    Раніше це був FILE_TO_CITY — таблиця «файл → місто» під старі `XXXX.md`.
    Файли бібліотеки тепер slug-картки (`cc-example-town.md`) і самі несуть назву
    та країну — окрема таблиця була б чужими даними в тулкіті, що старіє
    щоразу, як бібліотека поповнюється."""
    m = re.match(r'^#\s+(.+)$', card_text, re.MULTILINE)
    if not m:
        return ''
    parts = [p.strip() for p in m.group(1).split(' / ')]
    latin = next((p for p in reversed(parts)
                  if re.search(r'[A-Za-zÀ-ž]', p) and not re.search(r'[а-яА-ЯіІїЇєЄґҐ]', p)),
                 parts[-1])
    short = re.sub(r'\s*\([^)]*\)', '', latin).strip()
    cc_m = re.search(r'\*\*Код\*\*\s*\|\s*([A-Z]{2})', card_text)
    country = COUNTRY_EN.get(cc_m.group(1), '') if cc_m else ''
    return f'{short} {country}'.strip()


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
    files = sorted(p for p in ROOT.glob('*.md') if p.name not in PLACE_META_SKIP)
    for f in files:
        card_text = f.read_text()
        city = card_city_context(card_text)
        if not city:
            continue
        pois = parse_pois(card_text)
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
