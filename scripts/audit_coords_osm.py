"""
scripts/audit_coords_osm.py

Звіряє координати POI у places/*.md з OpenStreetMap через Overpass API.

НАВІЩО. Координати більшості POI писались з знань агента і жодного разу не
перевірялись. Аудит Ітаки (03.08.2026) знайшов 7 неточностей із 10, найгіршу на
6.3 км: точка «Фрікес» стояла на **центроїді острова**, бо координата прийшла з
мітки учасника, а Nominatim на запит «Frikes» віддає релейшн острова.

ЧОМУ OSM, А НЕ GOOGLE. Карта малюється тайлами OSM, тож координата з OSM
збігається з підкладкою візуально. Google дав би іншу точку, і маркер «поїхав»
би відносно тла. Плюс серверний Google-ключ у map-server referrer-обмежений і
для CLI недоступний.

ЯК ПРАЦЮЄ. Один запит Overpass на місце (не на POI): тягне всі іменовані
обʼєкти потрібних типів у радіусі RADIUS_KM навколо точки маршруту, далі
зіставляє локально за латинською назвою з дужок. Відповіді кешуються у
scratch-файл, тож повторний прогін не б'є по API.

ЧОГО МЕТОД НЕ ВМІЄ (прогін 03.08.2026: 100 перевірено, 17 позначено, з них
реальних помилок 0 — усі 7 справжніх виправлено раніше в цьому ж прогоні):

  • Протяжні обʼєкти. Ущелина Еніпеас — 10 км, Вікос — 12 км, «Нижнє місто»
    і «Фортечні стіни» — квартал і лінія мурів. Одна точка для них умовна,
    тож розбіжність із центроїдом OSM у кілька км — норма, а не помилка.
  • Тезки. Правило «серед збігів за назвою брати найближчий» знімає більшість
    (було 40 спрацювань, стало 23), але якщо тезка ближча за оригінал —
    виграє вона. Приклад: «Майдан Незалежності» чіпляється за іншу вулицю
    Незалежності в Києві.
  • Грецькі відмінкові закінчення. `Παρηγορήτισσας` (родовий) ≠ `Parigoritissa`
    при збігу по цілих словах — стемінг для грецької не реалізований.
  • POI, якого в OSM немає, не відрізняється від POI з неправильною
    координатою: обидва дають «не перевірено».

Тобто це **сито на грубі помилки** (Фрікес на центроїді острова, монастир
не той із двох однойменних), а не гарантія точності.

Usage:
    python3 scripts/audit_coords_osm.py                 # усі місця
    python3 scripts/audit_coords_osm.py --only 1300.md
    python3 scripts/audit_coords_osm.py --warn 0.5      # інший поріг, км
    python3 scripts/audit_coords_osm.py --refresh       # ігнорувати кеш
"""
import json
import math
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

PLACES = Path("places")
CACHE = Path(".osm-audit-cache.json")
OVERPASS = "https://overpass-api.de/api/interpreter"
RADIUS_KM = 15
# Секції, які не звіряємо: це заклади й товари, а не картографічні обʼєкти.
SKIP_SECTIONS = ("Крафт", "Морозиво", "Місцева особливість")
WARN_KM = 1.0

# Типи обʼєктів, серед яких шукаємо відповідники нашим POI.
# ⚠️ Тільки конкретні значення і тільки з ["name"]: bare-key фільтр на кшталт
# nwr["historic"] тягне десятки тисяч обʼєктів і запит не вкладається в timeout.
TAGS = [
    'nwr["place"~"^(city|town|village|hamlet|suburb|quarter|locality|island)$"]["name"]',
    'nwr["amenity"~"^(monastery|place_of_worship|theatre|marketplace)$"]["name"]',
    'nwr["historic"~"^(castle|fort|ruins|monastery|church|archaeological_site|monument|memorial|city_gate|tower)$"]["name"]',
    'nwr["tourism"~"^(viewpoint|attraction|artwork|museum|gallery)$"]["name"]',
    'nwr["natural"~"^(cave_entrance|peak|spring)$"]["name"]',
    'nwr["waterway"="waterfall"]["name"]',
    'nwr["man_made"~"^(windmill|tower|watermill)$"]["name"]',
    'nwr["aerialway"="station"]["name"]',
    'nwr["leisure"="park"]["name"]',
]


# Грецька → латиниця. Без цього матчер сліпий до половини маршруту: OSM у
# Греції підписаний грецькою, і «Το Γεφύρι της Άρτας» давало порожній набір
# токенів, а точка чіплялась до випадкового латинського тезки за 15 км.
_GR = str.maketrans({
    "α": "a", "β": "v", "γ": "g", "δ": "d", "ε": "e", "ζ": "z", "η": "i",
    "ι": "i", "κ": "k", "λ": "l", "μ": "m", "ν": "n", "ξ": "x", "ο": "o",
    "π": "p", "ρ": "r", "σ": "s", "ς": "s", "τ": "t", "υ": "y", "φ": "f",
    "ω": "o",
})
_GR_DI = (("θ", "th"), ("χ", "ch"), ("ψ", "ps"), ("ου", "ou"), ("μπ", "b"))


def flat(s: str) -> str:
    # NFD знімає і латинські, і грецькі наголоси: ά → α, ș → s
    s = "".join(c for c in unicodedata.normalize("NFD", s)
                if unicodedata.category(c) != "Mn").lower()
    for a, b in _GR_DI:
        s = s.replace(a, b)
    return s.translate(_GR).replace("ș", "s").replace("ț", "t")


def toks(s: str) -> set:
    return {t for t in re.split(r"[^a-z0-9]+", flat(s)) if len(t) >= 4}


def haversine(a, b, c, d) -> float:
    r = 6371.0
    p1, p2 = math.radians(a), math.radians(c)
    dp, dl = math.radians(c - a), math.radians(d - b)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def latin_variants(name: str) -> list:
    """«Озеро Бельа (Bâlea Lac, 2034 м)» → ['Bâlea Lac']."""
    m = re.search(r"\(([^)]*[A-Za-zÀ-ž][^)]*)\)", name)
    out = []
    if m:
        for part in re.split(r"\s*[/,]\s*", m.group(1)):
            part = re.sub(r"\d+\s*[мm]\b", "", part).strip()
            if part and re.search(r"[A-Za-zÀ-ž]{3}", part):
                out.append(part)
    # назва без дужок, якщо вона сама латиницею (Strada Sforii, Piața Mare)
    base = re.sub(r"\s*\([^)]*\)", "", name).strip()
    if re.search(r"[A-Za-zÀ-ž]{3}", base) and not re.search(r"[А-Яа-яЇїІіЄєҐґ]", base):
        out.append(base)
    return out


def parse_place(path: Path):
    """→ (main_lat, main_lon, [(section, name, lat, lon), ...])"""
    text = path.read_text(encoding="utf-8")
    m = re.search(r"\*\*Координати\*\*\s*\|\s*([-\d.]+)\s*,\s*([-\d.]+)", text)
    if not m:
        return None, None, []
    main = (float(m.group(1)), float(m.group(2)))
    rows, section = [], ""
    for line in text.splitlines():
        h = re.match(r"^##\s+(.*)$", line)
        if h:
            section = h.group(1).strip()
            continue
        if not line.startswith("|"):
            continue
        c = [x.strip() for x in line.split("|")[1:-1]]
        if len(c) < 3:
            continue
        name = re.sub(r"\*\*", "", c[0]).strip()
        cm = re.match(r"`?\s*([-\d.]+)\s*,\s*([-\d.]+)", c[2].strip("`"))
        if not name or not cm or name in ("Назва", "Заклад"):
            continue
        rows.append((section, name, float(cm.group(1)), float(cm.group(2))))
    return main[0], main[1], rows


def overpass(lat, lon, cache, refresh=False):
    key = f"{lat:.4f},{lon:.4f}"
    if not refresh and key in cache:
        return cache[key]
    # bbox, а не around: around сканує геометрію і на 15 км з такими фільтрами
    # не вкладається в timeout, тоді як bbox бере готовий індекс.
    dlat = RADIUS_KM / 111.0
    dlon = RADIUS_KM / (111.0 * max(0.2, math.cos(math.radians(lat))))
    bbox = f"{lat - dlat:.4f},{lon - dlon:.4f},{lat + dlat:.4f},{lon + dlon:.4f}"
    body = ("[out:json][timeout:120];(\n"
            + "\n".join(f'  {t}({bbox});' for t in TAGS)
            + "\n);out center tags;")
    req = urllib.request.Request(
        OVERPASS, data=urllib.parse.urlencode({"data": body}).encode(),
        headers={"User-Agent": "travel202609-coord-audit/1.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.load(resp)
            break
        except Exception as e:
            if attempt == 2:
                print(f"      ! Overpass: {e}")
                return []
            time.sleep(8 * (attempt + 1))
    feats = []
    for e in data.get("elements", []):
        t = e.get("tags") or {}
        names = [t[k] for k in t if k == "name" or k.startswith("name:") or k == "int_name"]
        if not names:
            continue
        la = e.get("lat") or (e.get("center") or {}).get("lat")
        lo = e.get("lon") or (e.get("center") or {}).get("lon")
        if la is None:
            continue
        feats.append({"names": names, "lat": la, "lon": lo,
                      "kind": t.get("place") or t.get("amenity") or t.get("historic")
                              or t.get("tourism") or t.get("natural") or t.get("man_made")
                              or t.get("building") or t.get("waterway") or ""})
    cache[key] = feats
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    time.sleep(2)
    return feats


def best_match(variants, feats, plat, plon):
    """НайБЛИЖЧИЙ OSM-обʼєкт серед тих, чия назва збіглась.

    ⚠️ Спершу тут бралась найкраща НАЗВА в усьому радіусі — і це давало масу
    хибних спрацювань: «Piața Sfatului» чіплялась за тезку за 11 км, монастир
    Катаріотісса — за обʼєкт на сусідньому острові. Тезки в радіусі 15 км
    трапляються постійно, тому серед збігів за назвою правильний майже завжди
    той, що ближчий; далекий однофамілець — це не «наша точка зміщена».
    """
    key = set()
    for v in variants:
        key |= toks(v)
    if not key:
        return None, 0.0
    cands = []
    for f in feats:
        for nm in f["names"]:
            hits = key & toks(nm)
            if hits:
                cands.append((haversine(plat, plon, f["lat"], f["lon"]), len(hits), f))
                break
    if not cands:
        return None, 0.0
    d, _, f = min(cands, key=lambda x: x[0])
    return f, d


def main():
    argv = sys.argv
    only = argv[argv.index("--only") + 1] if "--only" in argv else None
    warn = float(argv[argv.index("--warn") + 1]) if "--warn" in argv else WARN_KM
    refresh = "--refresh" in argv

    cache = {}
    if CACHE.exists() and not refresh:
        try:
            cache = json.loads(CACHE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cache = {}

    flagged, checked, skipped = [], 0, 0
    for path in sorted(PLACES.glob("[0-9][0-9][0-9][0-9].md")):
        if only and path.name != only:
            continue
        lat, lon, rows = parse_place(path)
        if lat is None or not rows:
            continue
        feats = overpass(lat, lon, cache, refresh)
        print(f"\n── {path.name}  ({len(rows)} POI, OSM-обʼєктів поряд: {len(feats)})")
        for section, name, plat, plon in rows:
            if any(k in section for k in SKIP_SECTIONS):
                continue          # паби, джелатерії, продукти — їх в OSM нема
            variants = latin_variants(name)
            if not variants:
                skipped += 1
                print(f"   ?  {name[:42]:<42} немає латинської назви — не перевірено")
                continue
            m, d = best_match(variants, feats, plat, plon)
            if not m:
                skipped += 1
                print(f"   ?  {name[:42]:<42} немає в OSM поряд — не перевірено")
                continue
            checked += 1
            if d > warn:
                flagged.append((path.name, name, plat, plon, m, d))
                print(f"   ❌ {name[:42]:<42} {d:5.1f} км → OSM {m['lat']:.4f},{m['lon']:.4f} "
                      f"[{m['kind']}] {m['names'][0][:28]}")
            else:
                print(f"   ✓  {name[:42]:<42} {d:5.2f} км")

    print(f"\n{'=' * 78}\nперевірено {checked} · не перевірено {skipped} · "
          f"розбіжність >{warn} км: {len(flagged)}")
    for f, name, plat, plon, m, d in sorted(flagged, key=lambda x: -x[5]):
        print(f"  {d:6.1f} км  {f}  {name[:38]:<38} "
              f"{plat:.4f},{plon:.4f} → {m['lat']:.4f},{m['lon']:.4f}")
    return 1 if flagged else 0


if __name__ == "__main__":
    sys.exit(main())
