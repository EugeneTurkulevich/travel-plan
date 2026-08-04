"""
scripts/check_submap_spread.py

Шукає nested_points, які лежать далеко від своєї точки маршруту.

НАВІЩО. Клієнт масштабує «Детальну карту» точки по bounding box усіх її
nested_points. Один віддалений POI розтягує сабкарту на десятки кілометрів —
власні обʼєкти міста злипаються в одну купку, і карта стає нечитабельною.
Саме так сталося з Едессою: до неї було прив'язано Пеллу за 40 км.

ПРАВИЛО. nested_points — це те, що реально оглядають з цієї зупинки: пішки або
коротким перегоном. Обʼєкт, який далі, — або **окрема точка маршруту**
(`add_point` / рядок в INDEX.md), або згадка в `## Нотатки` з координатами,
але не POI сусіднього міста.

Пороги: попередження від 12 км, помилка від 25 км. Для протяжних локацій
(острів, гірський масив) розкид більший — такі випадки виносити у ALLOW.

Run:
    python3 scripts/check_submap_spread.py
    python3 scripts/check_submap_spread.py --warn 8 --fail 20
"""
import json
import math
import sys
from pathlib import Path

STATE = Path("places/exports/route_state.json")

# Точки, де великий розкид виправданий: локація сама по собі протяжна.
ALLOW: dict[str, str] = {
    # Ітака — острів ~25 км завдовжки, і ми дві ночі стоїмо у Ваті на півдні,
    # а Кіоні/Фрікес/Ставрос лежать на північній долі. Розкид тут не помилка
    # прив'язки, а географія; перевірено по OSM 03.08.2026.
    "Ітака": "острів ~25 км завдовжки — обʼєкти по обох долях",
}


def haversine(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def main() -> int:
    argv = sys.argv
    warn = float(argv[argv.index("--warn") + 1]) if "--warn" in argv else 12.0
    fail = float(argv[argv.index("--fail") + 1]) if "--fail" in argv else 25.0

    state = json.loads(STATE.read_text(encoding="utf-8"))
    problems = 0
    print(f"Пороги: попередження >{warn:.0f} км, помилка >{fail:.0f} км\n")

    for p in state["points"]:
        nested = p.get("nested_points") or []
        if not nested:
            continue
        far = [(haversine(p["lat"], p["lon"], n["lat"], n["lon"]), n)
               for n in nested]
        spread = max(d for d, _ in far)
        label = p["label"]

        if label in ALLOW:
            print(f"  [skip] {label:<22} розкид {spread:5.1f} км — {ALLOW[label]}")
            continue
        if spread <= warn:
            continue

        print(f"  {'[FAIL]' if spread > fail else '[warn]'} {label:<22} "
              f"розкид {spread:5.1f} км")
        for d, n in sorted(far, key=lambda x: -x[0]):
            if d > warn:
                print(f"         · {n['name'][:44]:<44} {d:5.1f} км  [{n.get('type')}]")
        if spread > fail:
            problems += 1

    if problems:
        print(f"\n❌ точок із критичним розкидом: {problems}")
        print("   Винеси далекий обʼєкт в окрему точку маршруту або в ## Нотатки.")
        return 1
    print("\n✓ критичного розкиду немає")
    return 0


if __name__ == "__main__":
    sys.exit(main())
