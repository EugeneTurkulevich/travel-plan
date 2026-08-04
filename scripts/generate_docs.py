"""
scripts/generate_docs.py

Генерує оглядові документи з даних замість того, щоб їх писали руками (і
вони розходились із дійсністю — у старому репо таблиця днів у README
описувала маршрут, якого вже не було в route.json).

Два файли:

  <дані>/trips/<профіль>/ROUTE.md
      Таблиця днів активного профілю з route.json: день, дата, ланцюжок
      точок (посилання на картки бібліотеки), позначка ночівлі. Один рядок
      на кожен елемент route.json["days"] — навіть якщо точок за день немає
      (тоді показуємо, що ще триває попередня ночівля).

  <дані>/places/CATALOG.md
      Реєстр бібліотеки: ID, назва (з заголовка картки), країна, глибина,
      і історія використань — у яких поїздках картка трапляється і в якій
      ролі. Історія збирається з усіх <дані>/trips/*/route.json, які
      знайдуться, плюс зі статичних секцій `## Історія` в самих картках.

Обидва файли починаються попередженням, що вони згенеровані.

Usage:
    python3 scripts/generate_docs.py             # згенерувати й записати
    python3 scripts/generate_docs.py --dry-run    # тільки надрукувати
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trip_ctx import paths  # noqa: E402

CTX = paths()

GENERATED_WARNING = (
    "> ⚠️ Файл згенеровано автоматично скриптом `scripts/generate_docs.py`. "
    "Правити руками безглуздо — наступний запуск перезапише. Онови джерела "
    "(картки бібліотеки / route.json) і перезапусти скрипт.\n"
)


# ── картки бібліотеки ────────────────────────────────────────────────────────

def parse_place_card(path):
    """Мінімальний парс картки: заголовок, ID/Країна/Глибина з таблиці
    метаданих, тіло секції `## Історія`, якщо є. Картки без рядка **ID**
    (country_info.md, practical_info.md, сам CATALOG.md) — не картки місць,
    повертаємо None."""
    text = path.read_text(encoding="utf-8")

    id_m = re.search(r"\|\s*\*\*ID\*\*\s*\|\s*`([^`]+)`\s*\|", text)
    if not id_m:
        return None

    title_m = re.search(r"^#\s+(.+)$", text, re.M)
    title = title_m.group(1).strip() if title_m else path.stem

    country_m = re.search(r"\|\s*\*\*Країна\*\*\s*\|\s*([^|]+?)\s*\|", text)
    depth_m = re.search(r"\|\s*\*\*Глибина\*\*\s*\|\s*([^|]+?)\s*\|", text)
    history_m = re.search(r"^##\s+Історія\s*\n(.*?)(?=\n##\s|\Z)", text, re.S | re.M)

    return {
        "id": id_m.group(1).strip(),
        "title": title,
        "country": country_m.group(1).strip() if country_m else "—",
        "depth": depth_m.group(1).strip() if depth_m else "—",
        "history_note": history_m.group(1).strip() if history_m else "",
    }


def load_cards(places_dir):
    cards = {}
    for path in sorted(places_dir.glob("*.md")):
        card = parse_place_card(path)
        if card:
            cards[card["id"]] = card
    return cards


# ── історія використань по всіх поїздках ─────────────────────────────────────

def collect_usage(data_root):
    """id → {trip_id: {"days": {kind: [day, ...]}, "alt": bool}}."""
    usage = {}
    for route_path in sorted((data_root / "trips").glob("*/route.json")):
        trip_id = route_path.parent.name
        try:
            route = json.loads(route_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        for day in route.get("days") or []:
            for p in day.get("points") or []:
                pid = p.get("id")
                if not pid:
                    continue
                entry = usage.setdefault(pid, {}).setdefault(
                    trip_id, {"days": {}, "alt": False}
                )
                entry["days"].setdefault(p.get("kind", "?"), []).append(day.get("day"))

        for alt in route.get("alternates") or []:
            pid = alt.get("id") if isinstance(alt, dict) else alt
            if not pid:
                continue
            entry = usage.setdefault(pid, {}).setdefault(
                trip_id, {"days": {}, "alt": False}
            )
            entry["alt"] = True

    return usage


def format_history(usage_for_id):
    if not usage_for_id:
        return "—"
    parts = []
    for trip_id in sorted(usage_for_id):
        info = usage_for_id[trip_id]
        bits = []
        for kind, days in sorted(info["days"].items()):
            days_s = ", ".join(f"D{d}" for d in sorted(set(days)) if d is not None)
            bits.append(f"{kind} — {days_s}" if days_s else kind)
        if info["alt"]:
            bits.append("альтернатива (не в маршруті)" if not bits else "+ альтернатива")
        parts.append(f"`{trip_id}`: " + "; ".join(bits))
    return "; ".join(parts)


# ── CATALOG.md ───────────────────────────────────────────────────────────────

def build_catalog_md(cards, usage):
    lines = [GENERATED_WARNING, "", "# Бібліотека місць — реєстр", ""]
    for pid in sorted(cards):
        card = cards[pid]
        lines.append(f"## `{card['id']}` — {card['title']}")
        lines.append("")
        lines.append("| Країна | Глибина |")
        lines.append("|:--|:--|")
        lines.append(f"| {card['country']} | {card['depth']} |")
        lines.append("")
        lines.append(f"**Використання:** {format_history(usage.get(pid, {}))}")
        if card["history_note"]:
            lines.append("")
            lines.append("**Історія (з картки):**")
            lines.append("")
            lines.append(card["history_note"])
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ── ROUTE.md ─────────────────────────────────────────────────────────────────

def build_route_md(ctx, cards):
    trip = ctx.trip
    route = ctx.read_json(ctx.route_json)

    lines = [
        GENERATED_WARNING, "",
        f"# Маршрут — {trip.get('title', '—')}", "",
        f"{trip.get('start', '?')} … {trip.get('end', '?')}", "",
        "| День | Дата | Точки | Ночівля |",
        "|:--|:--|:--|:--|",
    ]

    # Стоянка на кілька ночей — ОДНА точка з nights: N (методичка воркера:
    # «базу не повторюємо»). Тому ночівля дня не завжди записана в цьому дні:
    # точка з date=D і nights=N покриває ночі D … D+N-1. День радіальної
    # вилазки (Пріонія з Літохоро) має власну visit-точку, але спимо ми того
    # вечора там само — показувати в такому рядку «—» означало б брехати
    # людині, яка читає таблицю. Це не здогад: діапазон рахується з даних.
    pending = None  # {"title": str, "remaining": int}

    for day in route.get("days") or []:
        points = day.get("points") or []
        overnight_bit = "—"

        if pending and pending["remaining"] > 0:
            overnight_bit = f"🛏️ {pending['title']} (продовження)"
            pending["remaining"] -= 1

        if points:
            chain_bits = []
            for p in points:
                pid = p.get("id")
                card = cards.get(pid)
                title = card["title"] if card else (pid or "?")
                chain_bits.append(f"[{title}](../../places/{pid}.md)" if pid else title)
                if p.get("kind") == "overnight":
                    nights = p.get("nights") or 1
                    overnight_bit = f"🛏️ {title}" + (f" ×{nights}" if nights > 1 else "")
                    pending = {"title": title, "remaining": nights - 1}
            chain = " → ".join(chain_bits)
        elif pending is not None and overnight_bit != "—":
            chain = f"— (ще одна ніч: {pending['title']})"
        else:
            chain = "—"

        lines.append(f"| {day.get('day')} | {day.get('date', '?')} | {chain} | {overnight_bit} |")

    return "\n".join(lines) + "\n"


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv):
    parser = argparse.ArgumentParser(description="Генерує ROUTE.md і CATALOG.md з даних")
    parser.add_argument("--dry-run", action="store_true", help="тільки надрукувати, нічого не писати")
    args = parser.parse_args(argv[1:])

    cards = load_cards(CTX.places_dir)
    usage = collect_usage(CTX.data_root)

    route_md = build_route_md(CTX, cards)
    catalog_md = build_catalog_md(cards, usage)

    route_path = CTX.trip_dir / "ROUTE.md"
    catalog_path = CTX.places_dir / "CATALOG.md"

    if args.dry_run:
        print(f"── {route_path} " + "─" * 20)
        print(route_md)
        print(f"── {catalog_path} " + "─" * 20)
        print(catalog_md)
        print(f"[dry-run] нічого не записано. Днів: "
              f"{len(CTX.read_json(CTX.route_json).get('days') or [])}, "
              f"карток у каталозі: {len(cards)}")
        return 0

    route_path.write_text(route_md, encoding="utf-8")
    catalog_path.write_text(catalog_md, encoding="utf-8")

    print(f"✓ {route_path} ({len(CTX.read_json(CTX.route_json).get('days') or [])} днів)")
    print(f"✓ {catalog_path} ({len(cards)} карток)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
