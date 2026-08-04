"""
scripts/build_schedule.py

Рахує розклад днів за моделлю з TRAVEL_TIMING_MODEL.md і друкує готовий
словник TIMINGS для scripts/add_timing_to_md.py.

Модель (спрощено):
  clean_min(leg) — час руху; беремо з OSRM (він уже враховує клас дороги
                   краще, ніж band-таблиця, і є єдиним джерелом для км в INDEX)
  k = (clean_total + DAY_BUFFER) / clean_total     — денний буфер на обід/заправку
  leg_min = clean_min × k, округлення до 15 хв
  arrive(i+1) = leave(i) + leg_min ; leave(i+1) = arrive(i+1) + stay

Гірські й прикордонні дні отримують власний множник (див. DAYS.friction) —
OSRM оптимістичний на серпантинах і не знає про черги.

Usage:
    python3 scripts/build_schedule.py            # розклад + TIMINGS
    python3 scripts/build_schedule.py --check    # тільки перевірка фінішу дня
"""
import sys
from datetime import datetime, timedelta

from osrm_legs import PLACES, leg  # noqa: F401  (той самий словник координат)

DAY_BUFFER = 60          # хв на обід + заправку + «тертя», на день
FERRY_MIN = 200          # Астакос ⇄ Пісаетос: ~3:20 з посадкою/висадкою
ISLAND = {"Вати_Ітака", "Фрікес"}   # перегони сюди/звідси — тільки паром

WD = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]

# ── План днів ────────────────────────────────────────────────────────────────
# stops: (ім'я в PLACES | None, файл місця, stay_хв). Останній stop дня — ночівля.
# stay для ночівлі ігнорується (рахується до старту наступного дня).
# friction: множник поверх OSRM (1.0 = як є). Гори/кордони/міста — більше.
DAYS = [
 dict(d=1,  date="2026-09-13", start="07:00", friction=1.05, stops=[
    ("Київ", "0100.md", 0), ("Чернівці", "0200.md", None)]),
 dict(d=2,  date="2026-09-14", start="07:00", friction=1.05, stops=[
    ("Чернівці", "0200.md", 0), ("Сірет", "0250.md", 60), ("Сучава", "0280.md", 75),
    ("Пятра_Нямц", "0320.md", 105), ("Червоне_озеро", "0360.md", None)]),
 # D3: коротко і рано — щоб у Брашові лишилось пів дня на міську гірочку
 # (Тимпа або Цитадель на Стражі). Ущелину Биказ проїжджаємо вранці, до автобусів.
 dict(d=3,  date="2026-09-15", start="08:00", friction=1.15, stops=[
    ("Червоне_озеро", "0360.md", 0), ("Биказ_ущелина", "0380.md", 60),
    ("Брашов", "0500.md", None)]),
 # D4 — найдовший день маршруту. Зняття Сібіу коштувало саме цього: до входу
 # на Трансфагараш тепер 106 зайвих км від Брашова. Тому цитадель Фегераша
 # прибрана з плану (лишається опційним проїздом — див. 0550.md), а старт о 07:00.
 dict(d=4,  date="2026-09-16", start="07:00", friction=1.35, stops=[   # Трансфагараш
    ("Брашов", "0500.md", 0), ("Бельа_Лак", "0650.md", 90),
    ("Відрару", "0680.md", 90), ("Римніку_Вилча", "0690.md", None)]),
 # D5: фортеця Белоградчика зачиняється о 18:00 — тому старт о 07:00 і
 # Крайова скорочена до швидкого обіду. Хорезу (ЮНЕСКО) лишаємо повноцінним.
 dict(d=5,  date="2026-09-17", start="07:00", friction=1.10, stops=[
    ("Римніку_Вилча", "0690.md", 0), ("Хорезу", "0720.md", 75),
    ("Крайова", "0750.md", 60), ("Калафат", "0800.md", 45), ("Белоградчик", "0900.md", None)]),
 dict(d=6,  date="2026-09-18", start="07:30", friction=1.10, stops=[
    ("Белоградчик", "0900.md", 0), ("Кулата", "1050.md", 20), ("Керкіні", "1080.md", None)]),
 dict(d=7,  date="2026-09-19", start="09:00", friction=1.05, stops=[   # човен на озері вранці
    ("Керкіні", "1080.md", 0), ("Літохоро", "1100.md", None)]),
 # Сходження: основний час НЕ за кермом — він у stay=480 на трейлхеді, тому
 # friction лишається «дорожнім». Подвійного обліку тут немає.
 dict(d=8,  date="2026-09-20", start="07:00", friction=1.10, stops=[
    ("Літохоро", "1100.md", 0), ("Пріонія", "1150.md", 480), ("Літохоро", "1100.md", None)]),
 dict(d=9,  date="2026-09-21", start="07:00", friction=1.05, stops=[
    ("Літохоро", "1100.md", 0), ("Калхіда", "1180.md", None)]),
 dict(d=10, date="2026-09-22", start="07:00", friction=1.05, stops=[
    ("Калхіда", "1180.md", 0), ("Астакос", "1250.md", 105), ("Вати_Ітака", "1300.md", None)]),
 dict(d=11, date="2026-09-23", start="09:00", friction=1.0, stops=[
    ("Вати_Ітака", "1300.md", None)]),                                 # вільний день
 dict(d=12, date="2026-09-24", start="09:00", friction=1.05, stops=[
    ("Вати_Ітака", "1300.md", 0), ("Астакос", "1250.md", 0),
    ("Папінго", "1400.md", None)]),
 dict(d=13, date="2026-09-25", start="08:30", friction=1.15, stops=[
    ("Папінго", "1400.md", 0), ("Едесса", "1500.md", None)]),
 dict(d=14, date="2026-09-26", start="07:00", friction=1.05, stops=[
    ("Едесса", "1500.md", 0), ("Кулата", "1550.md", 20),
    ("Мелник", "1600.md", 90), ("Копривщиця", "1700.md", None)]),
 dict(d=15, date="2026-09-27", start="08:00", friction=1.05, stops=[
    ("Копривщиця", "1700.md", 0), ("Русе", "1750.md", 45), ("Бухарест", "1800.md", None)]),
 dict(d=16, date="2026-09-28", start="07:30", friction=1.05, stops=[
    ("Бухарест", "1800.md", 0), ("Сірет", "1850.md", 90), ("Чернівці", "1900.md", None)]),
 dict(d=17, date="2026-09-29", start="08:30", friction=1.05, stops=[
    ("Чернівці", "1900.md", 0), ("Київ", "0100.md", None)]),
]


def r15(minutes: float) -> int:
    return int(round(minutes / 15.0) * 15)


def hhmm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def dur(mins: int) -> str:
    return f"{mins // 60}:{mins % 60:02d}"


def build():
    timings = {}          # fname -> list[(key, value)]
    arrivals = {}         # (day, fname) -> datetime
    day_end = {}          # day -> (fname, datetime)
    rows = []

    for day in DAYS:
        d0 = datetime.fromisoformat(f"{day['date']}T{day['start']}")
        stops = day["stops"]
        legs = []
        for (a, _, _), (b, _, _) in zip(stops, stops[1:]):
            if {a, b} & ISLAND:                 # морський перегін — OSRM не рахує
                legs.append(FERRY_MIN)
            else:
                legs.append(leg(a, b)[1])

        clean = sum(legs) or 1
        k = (clean + DAY_BUFFER) / clean * day["friction"]

        t = d0
        drive = 0
        chain = [f"{stops[0][0]} {hhmm(t)}"]
        for i, (name, fname, stay) in enumerate(stops[1:], start=1):
            seg = r15(legs[i - 1] * k)
            drive += seg
            t += timedelta(minutes=seg)
            arrivals[(day["d"], fname)] = t
            is_last = i == len(stops) - 1
            if is_last:
                chain.append(f"→ {name} {hhmm(t)} 🛏️")
                day_end[day["d"]] = (fname, t)
            else:
                chain.append(f"→ {name} {hhmm(t)} (+{stay}хв)")
                arr_v = t
                t += timedelta(minutes=stay)
                if stay > 0:          # транзит без стоянки таймінгу не отримує
                    timings.setdefault(fname, []).append(
                        ("Тайминг", f"{hhmm(arr_v)}, ~{dur(stay)}"))
        if len(stops) == 1:                      # вільний день
            day_end[day["d"]] = (stops[0][1], d0)
        rows.append((day["d"], day["date"], drive, " ".join(chain)))

    # ── ночівлі ──────────────────────────────────────────────────────────────
    # Кілька ночей поспіль в одному місці — це ОДИН інтервал: від прибуття
    # першого вечора до старту дня, коли ми звідти нарешті їдемо.
    i = 0
    while i < len(DAYS):
        fname, arr = day_end[DAYS[i]["d"]]
        if fname == "0100.md":
            i += 1
            continue
        j = i
        while j + 1 < len(DAYS) and day_end[DAYS[j + 1]["d"]][0] == fname:
            j += 1                                # ще одна ніч тут же
        if j + 1 < len(DAYS):
            nxt = DAYS[j + 1]
            dep = datetime.fromisoformat(f"{nxt['date']}T{nxt['start']}")
            timings.setdefault(fname, []).append(
                ("Тайминг", f"{WD[arr.weekday()]}.{hhmm(arr)}-{WD[dep.weekday()]}.{hhmm(dep)}"))
        i = j + 1

    # ── дім ──────────────────────────────────────────────────────────────────
    d1, dl = DAYS[0], DAYS[-1]
    start_dt = datetime.fromisoformat(f"{d1['date']}T{d1['start']}")
    finish_dt = day_end[dl["d"]][1]
    timings["0100.md"] = [
        ("Тайминг (старт)", f"{WD[start_dt.weekday()]}.{hhmm(start_dt)} виїзд"),
        ("Тайминг (фініш)", f"{WD[finish_dt.weekday()]}.~{hhmm(finish_dt)} повернення"),
    ]

    return rows, timings


def main():
    rows, timings = build()
    print(f"{'День':<5}{'Дата':<12}{'за кермом':<11}Ланцюг")
    print("-" * 100)
    for d, date_s, drive, chain in rows:
        print(f"D{d:<4}{date_s:<12}{dur(drive):<11}{chain}")

    if "--check" in sys.argv:
        return
    print("\n\n# ── вставити у scripts/add_timing_to_md.py ──\nTIMINGS = {")
    for fname in sorted(timings):
        vals = timings[fname]
        # для точки, яку відвідуємо двічі, лишаємо перший (візит) і ночівлю
        uniq = []
        for kv in vals:
            if kv not in uniq:
                uniq.append(kv)
        body = ", ".join(f'("{k}", "{v}")' for k, v in uniq)
        print(f'    "{fname}": [{body}],')
    print("}")


if __name__ == "__main__":
    main()
