"""
scripts/check_trip_rules.py

Перевіряє поточний розклад (DAYS у build_schedule.py) проти правил подорожі,
які лежать на хмарній карті у полі `trip_rules` (get_map через trip-map MCP).

Показує навантаження по днях: скільки за кермом, скільки на поромі, разом.
Паромний час рахується окремо — він у «за кермом» не входить, але на довжину
дня впливає.

⚠️ Ліміт годин за кермом **більше не є правилом подорожі** — його прибрано з
`trip_rules` на прохання власника (03.08.2026), бо поточний розклад влаштовує.
Тому за замовчуванням скрипт нічого не валідує, лише друкує таблицю. Якщо
колись знадобиться перевірка — `--limit N` вмикає її знову і дає exit 1.

Решта правил (ЮНЕСКО, фортеці, високі точки, морозиво, крафт, «не цікавлять
музеї») — якісні, їх перевіряє людина або модель по картках у places/.

Usage:
    PYTHONPATH=scripts python3 scripts/check_trip_rules.py             # тільки звіт
    PYTHONPATH=scripts python3 scripts/check_trip_rules.py --limit 8   # + перевірка
"""
import sys

from build_schedule import DAYS, DAY_BUFFER, dur, r15, leg_minutes, is_ferry_leg


def main():
    limit = None
    if "--limit" in sys.argv:
        limit = int(float(sys.argv[sys.argv.index("--limit") + 1]) * 60)

    print(f"Правило: ≤{limit // 60} год за кермом на день (паром окремо)\n"
          if limit else "Звіт по навантаженню днів (ліміт не задано)\n")
    print(f"{'День':<5}{'дата':<12}{'кермо':>7}{'паром':>8}{'разом':>8}  статус")
    print("-" * 62)

    over = []
    for day in DAYS:
        stops = day["stops"]
        legs = [leg_minutes(a, b) for (a, _, _), (b, _, _) in zip(stops, stops[1:])]
        clean = sum(legs) or 1
        k = (clean + DAY_BUFFER) / clean * day["friction"]

        drive = ferry = 0
        for i, ((a, _, _), (b, _, _)) in enumerate(zip(stops, stops[1:])):
            seg = r15(legs[i] * k)
            if is_ferry_leg(a, b):
                ferry += seg
            else:
                drive += seg

        bad = limit is not None and drive > limit
        if bad:
            over.append((day["d"], drive))
        status = ("❌" if bad else "✓") if limit is not None else ""
        print(f"D{day['d']:<4}{day['date']:<12}{dur(drive):>7}"
              f"{(dur(ferry) if ferry else '—'):>8}{dur(drive + ferry):>8}"
              f"  {status}")

    print("-" * 62)
    if limit is None:
        return 0
    if not over:
        print("✓ усі дні в межах правила")
        return 0
    print(f"❌ порушень: {len(over)} — "
          + ", ".join(f"D{d} ({dur(m)})" for d, m in over))
    print("\nЗауваж: перший і останній день (виїзд і повернення) можуть "
          "законно перевищувати ліміт — вони задані самою відстанню старту й "
          "фінішу поїздки, і без проміжної ночівлі коротшими не стануть.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
