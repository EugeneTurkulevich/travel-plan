"""
scripts/osrm_legs.py

Вимірює реальні дорожні відстані/час між точками маршруту через публічний OSRM.
Використовується при проєктуванні днів — щоб довжини переїздів були виміряні,
а не вгадані.

Джерело координат — бібліотека місць (`../travel-data/places/*.md`, через
CTX), а НЕ словник у коді: раніше тут жив словник PLACES з координатами й
Кириличними іменами конкретного маршруту — це дані поїздки, їм тут не місце
(STRUCTURE_PROPOSAL §7). Ключ точки — slug ID картки (`cc-example-town`), той
самий, що й `route.json.days[].points[].id` — це і є стабільний ідентифікатор,
на відміну від колишніх ad-hoc імен на кшталт «Пятра_Нямц».

Usage:
    python3 scripts/osrm_legs.py route  id1 id2 id3 ...  # послідовні перегони
    python3 scripts/osrm_legs.py matrix id1 id2 id3 ...  # повна матриця
    python3 scripts/osrm_legs.py plan                    # по днях з route.json

Порожній список імен (route/matrix) = усі точки бібліотеки.
"""
import json
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trip_ctx import paths  # noqa: E402

CTX = paths()

OSRM = "https://router.project-osrm.org"
PLACE_META_SKIP = {"country_info.md", "practical_info.md", "CATALOG.md"}


def _load_places():
    """slug ID → (lat, lon) — з рядків **Координати** усіх карток бібліотеки."""
    places = {}
    if not CTX.places_dir.is_dir():
        return places
    for card in sorted(CTX.places_dir.glob("*.md")):
        if card.name in PLACE_META_SKIP:
            continue
        text = card.read_text(encoding="utf-8")
        idm = re.search(r"\*\*ID\*\*\s*\|\s*`?([a-z0-9-]+)`?", text)
        coordm = re.search(r"\*\*Координати\*\*\s*\|\s*([-\d.]+)\s*,\s*([-\d.]+)", text)
        if idm and coordm:
            places[idm.group(1)] = (float(coordm.group(1)), float(coordm.group(2)))
    return places


PLACES = _load_places()


def _get(url: str, tries: int = 4):
    """GET з ретраями — публічний OSRM інколи 429/503."""
    last = None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return json.load(r)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"OSRM недоступний після {tries} спроб: {last}")


def _coords(names):
    missing = [n for n in names if n not in PLACES]
    if missing:
        raise SystemExit(f"Невідомі точки: {', '.join(missing)}\n"
                         f"Доступні: {', '.join(sorted(PLACES))}")
    return ";".join(f"{PLACES[n][1]},{PLACES[n][0]}" for n in names)


def leg(a: str, b: str):
    """Одна пара → (km, minutes)."""
    url = f"{OSRM}/route/v1/driving/{_coords([a, b])}?overview=false"
    data = _get(url)
    if data.get("code") != "Ok" or not data.get("routes"):
        raise RuntimeError(f"OSRM не побудував {a}→{b}: {data.get('code')}")
    r = data["routes"][0]
    return r["distance"] / 1000.0, r["duration"] / 60.0


def route(names):
    """Послідовні перегони — друкує таблицю і повертає підсумок."""
    total_km = total_min = 0.0
    print(f"{'Перегін':<44} {'км':>7} {'OSRM':>8}")
    print("-" * 62)
    for a, b in zip(names, names[1:]):
        km, mins = leg(a, b)
        total_km += km
        total_min += mins
        print(f"{a + ' → ' + b:<44} {km:>7.0f} {int(mins)//60:>5}:{int(mins)%60:02d}")
    print("-" * 62)
    print(f"{'РАЗОМ':<44} {total_km:>7.0f} {int(total_min)//60:>5}:{int(total_min)%60:02d}")
    return total_km, total_min


def matrix(names):
    """Повна матриця відстаней (км) — один запит до /table."""
    url = (f"{OSRM}/table/v1/driving/{_coords(names)}"
           f"?annotations=distance,duration")
    data = _get(url)
    if data.get("code") != "Ok":
        raise RuntimeError(f"OSRM table: {data.get('code')}")
    dist = data["distances"]
    w = max(len(n) for n in names) + 1
    print(" " * w + "".join(f"{n[:7]:>8}" for n in names))
    for i, a in enumerate(names):
        row = "".join(
            f"{(dist[i][j] / 1000):>8.0f}" if dist[i][j] is not None else f"{'—':>8}"
            for j in range(len(names))
        )
        print(f"{a:<{w}}{row}")
    return dist


# ── План маршруту — з route.json, а не літералом (STRUCTURE_PROPOSAL §7) ────
# route.json.days[].points не завжди несе точку, з якої день ФАКТИЧНО
# стартує (це попередня ночівля з учорашнього дня — порожній день
# відпочинку взагалі не має власних points). Тому послідовність зупинок дня
# для вимірювання — це точка, де закінчився попередній день, + точки цього
# дня. day1 виключення не потребує: у ньому вже є "дім" першим записом.
_WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]


def route_day_stops():
    """[(day, date_label, [point_id, ...]), ...] — послідовність зупинок
    кожного дня, з точки, де закінчився попередній день (БАЗА).

    БАЗА — точка останньої ночівлі (kind: overnight/home), а НЕ просто
    останній point дня: багатоночівельна зупинка лишає базу незмінною для
    ВСІХ ночей, а radial-день (виїзд на обʼєкт і повернення на ту саму базу
    того ж вечора, напр. день сходження) взагалі не має власного overnight-
    запису в route.json (nights уже пораховано на попередньому дні) — тоді
    його one-way point у бібліотеці означає РАУНД-ТРІП: неявний зворотний
    перегін до бази дописується в кінець дня."""
    import datetime
    route = CTX.read_json(CTX.route_json)
    out = []
    base = None
    for day in route.get("days", []):
        pts = day.get("points", [])
        ids = [p["id"] for p in pts]
        stops = ([base] if base else []) + ids
        new_base = base
        for p in pts:
            if p.get("kind") in ("overnight", "home"):
                new_base = p["id"]
        if ids and new_base == base:
            stops.append(base)   # база не змінилась — неявне повернення
        base = new_base
        d = datetime.date.fromisoformat(day["date"])
        label = f"{d.day:02d}.{d.month:02d} {_WD[d.weekday()]}"
        out.append((day["day"], label, stops))
    return out


def resolved_pair(a, b, ferry_by_id):
    """(a, b) для OSRM-вимірювання, або None — якщо це чисто морський
    перегін, який OSRM не порахує (так само як стара ROUTE_PLAN просто НЕ
    включала острівну точку в перегін). Якщо один з кінців — точка з полем
    `ferry` (route.json), а інший кінець — не її via_id, підміняємо цей
    кінець на via_id: реальна дорожня частина лишається вимірюваною, морська
    — ні (`plan()` — це виключно ДОРОЖНЯ відстань, за задумом)."""
    fa = ferry_by_id.get(a)
    fb = ferry_by_id.get(b)
    if fa and fa["via_id"] == b:
        return None
    if fb and fb["via_id"] == a:
        return None
    if fa:
        a = fa["via_id"]
    if fb:
        b = fb["via_id"]
    return (a, b)


def _ferry_by_id():
    route = CTX.read_json(CTX.route_json)
    out = {}
    for day in route.get("days", []):
        for p in day.get("points", []):
            if p.get("ferry"):
                out[p["id"]] = p["ferry"]
    return out


def plan():
    days = route_day_stops()
    if not days:
        raise SystemExit("route.json не має днів — нічого рахувати")
    ferry_by_id = _ferry_by_id()
    grand_km = grand_min = 0.0
    for day, date_label, pts in days:
        km = mins = 0.0
        for a, b in zip(pts, pts[1:]):
            resolved = resolved_pair(a, b, ferry_by_id)
            if resolved is None:
                continue          # морський перегін — OSRM не рахує
            k, m = leg(*resolved)
            km += k
            mins += m
        grand_km += km
        grand_min += mins
        chain = " → ".join(pts)
        print(f"D{day:<2} {date_label}  {km:>5.0f} км  {int(mins)//60}:{int(mins)%60:02d}   {chain}")
    print("-" * 78)
    print(f"РАЗОМ {grand_km:.0f} км, {int(grand_min)//60}:{int(grand_min)%60:02d} чистого руху "
          f"(без зупинок і буферів)")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "matrix"
    args = sys.argv[2:] or sorted(PLACES)
    if mode == "route":
        route(args)
    elif mode == "matrix":
        matrix(args)
    elif mode == "plan":
        plan()
    else:
        raise SystemExit(__doc__)
