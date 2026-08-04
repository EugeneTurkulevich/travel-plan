"""
scripts/trip.py

Вибір поїздки, з якою працюємо. Профіль — це не «налаштування», а відповідь на
питання «чий зараз маршрут»: від нього залежить, у яку карту піде публікація.

    python3 scripts/trip.py                 # хто активний + стан
    python3 scripts/trip.py list            # усі профілі в корені даних
    python3 scripts/trip.py use <id>        # перемкнути (пише .active-trip)
    python3 scripts/trip.py new <id>        # створити профіль        (Ф7)
    python3 scripts/trip.py import <map_id> # зібрати профіль з карти (Ф7)

`show` навмисно друкує map_id повністю: перемикання профілю — це той момент,
коли треба бачити, куди саме поїде наступний пуш.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from trip_ctx import (  # noqa: E402
    TripContextError, context, list_trips, resolve_data_root,
)

NOT_YET = {
    "new": "Ф7 плану міграції — створення профілю з нуля (з питаннями методички "
           "і пропозицією set_trip_rules).",
    "import": "Ф7 плану міграції — реконструкція профілю й бібліотеки з наявної "
              "карти (get_map / get_route / list_alt_route).",
}


def human_size(path):
    return f"{path.stat().st_size / 1024:.0f} КБ" if path.exists() else "—"


def cmd_show():
    ctx = context()
    ctx.banner()
    t = ctx.trip
    print()
    print(f"  назва      {t.get('title', '—')}")
    print(f"  дати       {t.get('start', '?')} … {t.get('end', '?')}")
    print(f"  країни     {', '.join(t.get('countries') or []) or '—'}")
    print(f"  карта      {ctx.map_id or '—'}")
    print(f"  воркер     {t.get('worker', '—')}")
    print(f"  статус     {t.get('status', '—')}")

    cats = t.get("poi_categories") or {}
    if cats:
        pretty = " · ".join(f"{v.get('icon', '')}{k}" for k, v in cats.items())
        print(f"  категорії  {pretty}")

    print("\n  дані:")
    cards = sorted(ctx.places_dir.glob("*.md")) if ctx.places_dir.exists() else []
    print(f"    бібліотека   {len(cards)} карток  ({ctx.places_dir})")
    for label, path in (
        ("route.json  ", ctx.route_json),
        ("live_mirror ", ctx.live_mirror),
        ("route_state ", ctx.route_state),
    ):
        mark = "✓" if path.exists() else "·"
        print(f"    {mark} {label} {human_size(path)}")

    if not ctx.env_file.exists():
        print(f"\n  ⚠️  немає {ctx.env_file} — публікація не працюватиме")
    return 0


def cmd_list():
    root = resolve_data_root()
    active = (root / ".active-trip")
    active_id = active.read_text(encoding="utf-8").strip() if active.exists() else None
    trips = list_trips(root)
    if not trips:
        print(f"У {root}/trips/ немає жодного профілю.")
        return 1
    print(f"Корінь даних: {root}\n")
    for tid, t in trips:
        mark = "▶" if tid == active_id else " "
        status = t.get("status", "?")
        print(f" {mark} {tid:24} {status:9} {t.get('title', '')}")
    return 0


def cmd_use(trip_id):
    root = resolve_data_root()
    known = {tid for tid, _ in list_trips(root)}
    if trip_id not in known:
        print(f"Профілю «{trip_id}» немає. Наявні: {', '.join(sorted(known)) or '—'}",
              file=sys.stderr)
        return 1
    (root / ".active-trip").write_text(trip_id + "\n", encoding="utf-8")
    return cmd_show()


VALUE_FLAGS = ("--trip", "--data")


def positional(argv):
    """Позиційні аргументи без прапорців — і без ЗНАЧЕНЬ прапорців.

    Інакше `trip.py --trip <id>` читає <id> як команду.
    """
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
            continue
        if a in VALUE_FLAGS:
            skip = True
            continue
        if a.startswith("--"):
            continue
        out.append(a)
    return out


def main(argv):
    args = positional(argv[1:])
    cmd = args[0] if args else "show"

    if cmd in NOT_YET:
        print(f"«{cmd}» ще не реалізовано.\n{NOT_YET[cmd]}", file=sys.stderr)
        return 2
    try:
        if cmd == "show":
            return cmd_show()
        if cmd == "list":
            return cmd_list()
        if cmd == "use":
            if len(args) < 2:
                print("Треба id профілю: trip.py use <id>", file=sys.stderr)
                return 2
            return cmd_use(args[1])
    except TripContextError as e:
        print(f"⚠️  {e}", file=sys.stderr)
        return 1

    print(f"Невідома команда «{cmd}». Є: show, list, use, new, import",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
