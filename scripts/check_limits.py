"""
scripts/check_limits.py

Перевіряє CTX.route_state проти лімітів MCP-воркера, ДО пушу. Дешева
локальна відмова замість ToolError від воркера після пересилання мегабайта
через мережу.

Ліміти — властивість самого воркера (POINT_SCHEMA / set_route у
map-server), а не конкретної поїздки, тому їх можна безпечно тримати як
константи в тулкіті.

⚠️ Розмір payload рахується в БАЙТАХ (len(json.dumps(...).encode("utf-8"))),
а не в символах: кирилиця в UTF-8 — два байти на символ, len() рядка занижує
розмір удвічі.

Payload — це те, що реально піде в set_route (points/country_info/
booking_persons), не весь файл: version у payload не потрапляє.

Usage:
    python3 scripts/check_limits.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trip_ctx import paths  # noqa: E402

CTX = paths()

# Поля точки, які реально йдуть у set_route (те саме коло, що в cloud_push.py).
KNOWN_POINT_KEYS = {
    "id", "lat", "lon", "kind", "label", "popup_html", "date", "nights",
    "arrive_at", "stay_minutes", "nested_points", "extra_info",
}

# ── ліміти воркера ────────────────────────────────────────────────────────────
LIMIT_POINTS = 120
LIMIT_NESTED_PER_POINT = 60
LIMIT_POPUP_HTML = 20_000
LIMIT_POI_DESCRIPTION = 500
LIMIT_PHOTO_URL = 500
LIMIT_LABEL = 200
LIMIT_POI_NAME = 120
LIMIT_PAYLOAD_BYTES = 900_000  # рівно межа воркера (writeRouteWithRevision/
# assertLiveDocSizeOk у mcp-server.js рахують new TextEncoder().encode(...).length
# > 900_000, а не 900*1024=921600 — з тим числом наш guard був би лагідніший за
# реальний і пропустив би payload, який сервер усе одно відхилить)


def build_payload(state):
    """Те, що cloud_push.py фактично надішле в set_route: лише відомі поля
    точки плюс country_info/booking_persons, якщо вони є у файлі."""
    points = []
    for p in state.get("points") or []:
        points.append({k: v for k, v in p.items() if k in KNOWN_POINT_KEYS})
    payload = {"points": points}
    if state.get("country_info"):
        payload["country_info"] = state["country_info"]
    if state.get("booking_persons") is not None:
        payload["booking_persons"] = state["booking_persons"]
    return payload


def worst(items, extractor, label_of):
    """Максимум і те, на чому він досягнутий: (value, label) або (0, None)."""
    best_value, best_label = 0, None
    for item in items:
        v = extractor(item)
        if v > best_value:
            best_value, best_label = v, label_of(item)
    return best_value, best_label


def row_mark(value, limit):
    if value > limit:
        return "✗"
    if limit and value / limit > 0.8:
        return "❗"
    return " "


def print_row(name, limit, value, where=None):
    ratio = (value / limit) if limit else 0
    headroom = f"{max(0.0, (1 - ratio) * 100):5.0f}%"
    mark = row_mark(value, limit)
    where_s = f"  ({where})" if where else ""
    print(f" {mark} {name:<32} ≤{limit:<8} факт {value:<8}{where_s}  запас {headroom}")
    return value > limit


def main():
    state_path = CTX.route_state
    if not state_path.exists():
        print(f"⚠️  Немає {state_path} — спершу згенеруй route_state.json.",
              file=sys.stderr)
        return 1

    state = json.loads(state_path.read_text(encoding="utf-8"))
    points = state.get("points") or []
    payload = build_payload(state)
    payload_bytes = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    print(f"route_state: {state_path}\n")
    print(f" {'ліміт':<34}{'значення':<9}{'факт':<8}          запас")

    exceeded = False

    exceeded |= print_row("точок у set_route", LIMIT_POINTS, len(points))

    nested_val, nested_where = worst(
        points, lambda p: len(p.get("nested_points") or []), lambda p: p.get("label")
    )
    exceeded |= print_row("nested_points на точку", LIMIT_NESTED_PER_POINT,
                           nested_val, nested_where)

    popup_val, popup_where = worst(
        points, lambda p: len(p.get("popup_html") or ""), lambda p: p.get("label")
    )
    exceeded |= print_row("popup_html (символів)", LIMIT_POPUP_HTML,
                           popup_val, popup_where)

    all_nested = [(p.get("label"), n) for p in points for n in (p.get("nested_points") or [])]

    desc_val, desc_where = worst(
        all_nested, lambda pn: len(pn[1].get("description") or ""),
        lambda pn: f"{pn[0]} / {pn[1].get('name')}"
    )
    exceeded |= print_row("description POI (символів)", LIMIT_POI_DESCRIPTION,
                           desc_val, desc_where)

    photo_val, photo_where = worst(
        all_nested, lambda pn: len(pn[1].get("photo_url") or ""),
        lambda pn: f"{pn[0]} / {pn[1].get('name')}"
    )
    exceeded |= print_row("photo_url (символів)", LIMIT_PHOTO_URL,
                           photo_val, photo_where)

    label_val, label_where = worst(
        points, lambda p: len(p.get("label") or ""), lambda p: p.get("label")
    )
    exceeded |= print_row("label точки (символів)", LIMIT_LABEL,
                           label_val, label_where)

    name_val, name_where = worst(
        all_nested, lambda pn: len(pn[1].get("name") or ""),
        lambda pn: f"{pn[0]} / {pn[1].get('name')}"
    )
    exceeded |= print_row("name POI (символів)", LIMIT_POI_NAME,
                           name_val, name_where)

    exceeded |= print_row("payload сумарно (байт)", LIMIT_PAYLOAD_BYTES, payload_bytes)

    print()
    if exceeded:
        print("✗ є перевищення лімітів воркера — пуш відхилить set_route")
        return 1
    print("✓ у межах лімітів воркера")
    return 0


if __name__ == "__main__":
    sys.exit(main())
