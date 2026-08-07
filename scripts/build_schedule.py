"""
scripts/build_schedule.py

Рахує розклад днів (арайв/виїзд по зупинках) з рішень, які лежать у
route.json — а не з літерала DAYS у коді (STRUCTURE_PROPOSAL §7: старт дня,
friction і послідовність зупинок — рішення про розклад ЦІЄЇ поїздки, тобто
дані, їм місце в ../travel-data).

Модель (спрощено):
  clean_min(leg) — час руху; беремо з OSRM (він уже враховує клас дороги
                   краще, ніж band-таблиця)
  k = (clean_total + DAY_BUFFER) / clean_total     — денний буфер на обід/заправку
  leg_min = clean_min × k, округлення до 15 хв
  arrive(i+1) = leave(i) + leg_min ; leave(i+1) = arrive(i+1) + stay

Гірські й прикордонні дні отримують власний множник (route.json →
days[].friction) — OSRM оптимістичний на серпантинах і не знає про черги.

ДЖЕРЕЛО ЗУПИНОК ДНЯ. route.json.days[].points не завжди несе точку, з якої
день фактично стартує: багатоночівельна зупинка лишає базу незмінною для
всіх ночей (нема окремого запису на кожен день), а radial-день (виїзд на
обʼєкт і повернення туди ж того самого вечора — напр. день сходження) не
має власного overnight-запису взагалі. Тому послідовність зупинок дня
рахується від точки останньої ночівлі (БАЗА), а не від voice "останній
point дня": базу рухають лише kind=overnight/home; якщо за день база не
змінилась — це radial-день, і в кінець дня дописується неявне повернення
до бази.

ПОРОМ. route.json.points[].ferry = {via_id, minutes} — точка (типово
острів), куди/звідки OSRM не порахує дорогу; leg_minutes() рахує фіксовану
переправу, а якщо другий кінець перегону — не сам via_id, додає ще й
реальну дорожню частину via_id↔другий кінець (див. leg_minutes).

Usage:
    python3 scripts/build_schedule.py            # розклад по днях
    python3 scripts/build_schedule.py --check    # те саме (сумісність CLI)
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trip_ctx import paths  # noqa: E402
from osrm_legs import leg  # noqa: E402

CTX = paths()

DAY_BUFFER = 60          # хв на обід + заправку + «тертя», на день — модель
                          # часу, однакова для будь-якої поїздки, не дані.
WD = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]


def _load_route():
    return CTX.read_json(CTX.route_json)


def _ferry_by_id(route):
    """point id → {"via_id", "minutes"} для точок, куди OSRM не долетить
    суходолом (route.json.points[].ferry)."""
    out = {}
    for day in route.get("days", []):
        for p in day.get("points", []):
            if p.get("ferry"):
                out[p["id"]] = p["ferry"]
    return out


def _build_days(route):
    """route.json → [{"d","date","start","friction","stops":[(id,None,stay)]}]
    — та сама форма, що раніше несла літеральна DAYS (третій елемент stops
    лишається на місці для сумісності з check_trip_rules.py, яка читає лише
    stops[i][0])."""
    days = []
    base = None
    for day in route.get("days", []):
        pts = day.get("points", [])
        ids = [p["id"] for p in pts]
        stay_lookup = {p["id"]: p.get("stay_minutes") for p in pts}

        stop_ids = ([base] if base else []) + ids
        new_base = base
        for p in pts:
            if p.get("kind") in ("overnight", "home"):
                new_base = p["id"]
        if ids and new_base == base:
            stop_ids.append(base)   # база не змінилась — неявне повернення
        base = new_base

        stops = []
        for i, sid in enumerate(stop_ids):
            if i == 0:
                stay = 0
            elif i == len(stop_ids) - 1:
                stay = None
            else:
                stay = stay_lookup.get(sid)
            stops.append((sid, None, stay))

        days.append(dict(d=day["day"], date=day["date"],
                          start=day.get("start"), friction=day.get("friction"),
                          stops=stops))
    return days


_ROUTE = _load_route()
DAYS = _build_days(_ROUTE)
FERRY_BY_ID = _ferry_by_id(_ROUTE)


def leg_minutes(a: str, b: str) -> float:
    """Час перегону в хвилинах: звичайний OSRM leg, або — якщо один з кінців
    має `ferry` у route.json — фіксована переправа, за потреби складена з
    реальної дорожньої частини до/від найближчого порту (via_id). Завжди
    повертає число (на відміну від osrm_legs.resolved_pair, який може
    сказати «пропусти» — тут пропускати нічого не можна, розклад мусить
    мати час на кожен перегін)."""
    fa = FERRY_BY_ID.get(a)
    fb = FERRY_BY_ID.get(b)
    if fa and fa["via_id"] == b:
        return fa["minutes"]
    if fb and fb["via_id"] == a:
        return fb["minutes"]
    total = 0.0
    if fa:
        total += fa["minutes"]
        a = fa["via_id"]
    if fb:
        total += fb["minutes"]
        b = fb["via_id"]
    if a == b:
        return total
    return total + leg(a, b)[1]


def is_ferry_leg(a: str, b: str) -> bool:
    """True — цілий перегін суто поромний (без дорожньої частини). Для
    перегону, що ЧАСТКОВО пором + дорога (напр. виїзд з острова одразу до
    точки, що не є портом), рахуємо як "дорога" — наближення: check_trip_rules
    лише звітує навантаження, нічого не гейтить."""
    fa = FERRY_BY_ID.get(a)
    fb = FERRY_BY_ID.get(b)
    return bool((fa and fa["via_id"] == b) or (fb and fb["via_id"] == a))


def r15(minutes: float) -> int:
    return int(round(minutes / 15.0) * 15)


def hhmm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def dur(mins: int) -> str:
    return f"{mins // 60}:{mins % 60:02d}"


def build():
    rows = []
    for day in DAYS:
        if day["start"] is None or day["friction"] is None:
            raise SystemExit(
                f"route.json: day {day['d']} ({day['date']}) не має start/friction — "
                "розклад дня без цих полів не порахувати. Не вгадуємо дефолт (07:00 чи "
                "коефіцієнт — це рішення про КОНКРЕТНУ поїздку, не механізм скрипта): "
                "додай 'start' (HH:MM) і 'friction' (число) у days[] цього дня в route.json."
            )
        d0 = datetime.fromisoformat(f"{day['date']}T{day['start']}")
        stops = day["stops"]
        legs = [leg_minutes(a, b) for (a, _, _), (b, _, _) in zip(stops, stops[1:])]

        clean = sum(legs) or 1
        k = (clean + DAY_BUFFER) / clean * day["friction"]

        t = d0
        drive = 0
        chain = [f"{stops[0][0]} {hhmm(t)}"]
        for i, (name, _, stay) in enumerate(stops[1:], start=1):
            seg = r15(legs[i - 1] * k)
            drive += seg
            t += timedelta(minutes=seg)
            is_last = i == len(stops) - 1
            if is_last:
                chain.append(f"→ {name} {hhmm(t)} 🛏️")
            else:
                chain.append(f"→ {name} {hhmm(t)} (+{stay}хв)")
                t += timedelta(minutes=stay or 0)
        rows.append((day["d"], day["date"], drive, " ".join(chain)))

    return rows


def main():
    rows = build()
    print(f"{'День':<5}{'Дата':<12}{'за кермом':<11}Ланцюг")
    print("-" * 100)
    for d, date_s, drive, chain in rows:
        print(f"D{d:<4}{date_s:<12}{dur(drive):<11}{chain}")


if __name__ == "__main__":
    main()
