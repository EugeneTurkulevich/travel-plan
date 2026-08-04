"""
scripts/cloud_push.py

Пряма заливка places/exports/route_state.json у хмарну карту через remote MCP
worker (trip-map-mcp) — HTTP JSON-RPC tools/call, без моделі й без локального
map-server. Заміна старої звʼязки load_state_from_file + push_to_cloud (той
канал лишається, поки що робочий; цей — цільовий, див. CLAUDE.md).

Автентифікація — .env у корені репо (простий KEY=VALUE, без бібліотек):
    TRIP_MAP_TOKEN=<готовий access token>            # якщо вже є
    TRIP_MAP_REFRESH_TOKEN=<refresh token>            # інакше — обміняємо на access
    TRIP_MAP_WORKER=https://...                       # опційно, перевизначає воркер

Токен отримуєш через конектор-онбординг Trip Map (trip-map.web.app → підключити
агента) або попросивши Claude мінтнути його через MCP-flow — сам скрипт токен
не видає.

Usage:
    python3 scripts/cloud_push.py --draft "Назва"        # створити чернетку, залити маршрут
    python3 scripts/cloud_push.py                        # залити в карту з .cloud-draft-id
    python3 scripts/cloud_push.py --map <map_id>          # залити у вказану карту
    python3 scripts/cloud_push.py --promote <target_id>   # після заливки в чернетку — promote_draft
    python3 scripts/cloud_push.py --dry-run               # показати підсумок payload, нічого не слати

--dry-run не потребує токена й не ходить у мережу — рахує й показує, що
пішло б на воркер.

Обмін route_state.json → аргументи set_route:
  - points: залишаються лише відомі поля (id/lat/lon/kind/label/popup_html/
    date/nights/arrive_at/stay_minutes/nested_points/extra_info) — решта
    відкидається з попередженням у stderr.
  - country_info/booking_persons передаються, лише якщо є у файлі (інакше
    воркер лишає як було на карті).
  - overnight_places/parking_places (якщо колись з'являться у файлі) —
    live-шар, ним володіють учасники; ІГНОРУЄМО завжди, лише попереджаємо
    якщо непорожні.

Конфлікт ревізій (карту хтось змінив паралельно) — exit 2. Інші помилки
тула/мережі — exit 1.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trip_ctx import paths  # noqa: E402

DEFAULT_WORKER = "https://trip-map-mcp.e--t.workers.dev"
USER_AGENT = "trip-map-toolkit/1.0"

# Шляхи й карта — з активного профілю, не з припущення про робочий каталог.
CTX = paths()
STATE_DEFAULT = CTX.route_state
ENV_FILE = CTX.env_file
DRAFT_ID_FILE = CTX.draft_id_file

# Поля точки, які приймає set_route (POINT_SCHEMA у map-server/worker/mcp/mcp-server.js).
KNOWN_POINT_KEYS = {
    "id", "lat", "lon", "kind", "label", "popup_html", "date", "nights",
    "arrive_at", "stay_minutes", "nested_points", "extra_info",
}
# Top-level ключі route_state.json, які ми свідомо споживаємо/ігноруємо мовчки.
TOP_LEVEL_CONSUMED = {"version", "points", "country_info", "booking_persons"}
# Live-шар (right-click користувача) — не наш, лише попереджаємо якщо непорожній.
LIVE_LAYER_KEYS = ("overnight_places", "parking_places")


# ── .env parser (KEY=VALUE, без бібліотек) ───────────────────────────────────

def load_env_file(path=ENV_FILE):
    data = {}
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if k:
            data[k] = v
    return data


def get_worker(env_vars):
    # Пріоритет: оточення → .env даних → профіль поїздки → загальний дефолт.
    return (os.environ.get("TRIP_MAP_WORKER")
            or env_vars.get("TRIP_MAP_WORKER")
            or CTX.trip.get("worker")
            or DEFAULT_WORKER)


# ── auth ──────────────────────────────────────────────────────────────────────

def get_access_token(env_vars, worker):
    token = os.environ.get("TRIP_MAP_TOKEN") or env_vars.get("TRIP_MAP_TOKEN")
    if token:
        return token

    refresh = os.environ.get("TRIP_MAP_REFRESH_TOKEN") or env_vars.get("TRIP_MAP_REFRESH_TOKEN")
    if not refresh:
        sys.exit(
            "Немає токена доступу до хмарної карти.\n"
            f"Постав у {ENV_FILE} (корінь репо) один з рядків:\n"
            "  TRIP_MAP_TOKEN=<готовий access token>\n"
            "  TRIP_MAP_REFRESH_TOKEN=<refresh token>\n"
            "Отримати: пройди конектор-онбординг Trip Map (застосунок trip-map.web.app →\n"
            "«Підключити агента») або попроси Claude мінтнути токен через MCP-flow."
        )

    body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": refresh}).encode()
    req = urllib.request.Request(f"{worker}/token", data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:800]
        sys.exit(
            f"Не вдалося оновити токен через {worker}/token: HTTP {e.code}: {detail}\n"
            "TRIP_MAP_REFRESH_TOKEN міг протермінуватись (90 днів) — онов онбордингом."
        )
    except urllib.error.URLError as e:
        sys.exit(f"Мережева помилка звернення до {worker}/token: {e}")

    access = data.get("access_token")
    if not access:
        sys.exit(f"Відповідь {worker}/token без access_token: {data}")
    return access


# ── JSON-RPC tools/call ───────────────────────────────────────────────────────

def call_tool(worker, token, name, arguments, timeout=120, strict=True):
    """strict=False — повернути None замість завершення скрипта.

    Потрібно, щоб відрізнити «карти немає» від справжньої помилки: чернетка
    одноразова, її могли видалити після promote, і це нормальний стан.
    """
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }).encode()
    req = urllib.request.Request(worker, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:800]
        if not strict:
            return None
        if e.code == 401:
            sys.exit(
                f"Неавторизовано (401) на {worker}: {detail}\n"
                "Токен недійсний/протермінований — онов TRIP_MAP_REFRESH_TOKEN у .env "
                "(конектор-онбординг Trip Map або попроси Claude мінтнути токен)."
            )
        sys.exit(f"HTTP {e.code} від {worker} (tool={name}): {detail}")
    except urllib.error.URLError as e:
        if not strict:
            return None
        sys.exit(f"Мережева помилка звернення до {worker} (tool={name}): {e}")

    if "error" in resp:
        if not strict:
            return None
        err = resp["error"]
        sys.exit(f"JSON-RPC помилка ({err.get('code')}) у {name}: {err.get('message')}")

    result = resp.get("result") or {}
    content = result.get("content") or []
    text = content[0].get("text", "") if content else ""

    if result.get("isError"):
        if not strict:
            return None
        if "Конфлікт ревізій" in text:
            print(f"Конфлікт ревізій у {name}: {text}", file=sys.stderr)
            sys.exit(2)
        sys.exit(f"Помилка тула {name}: {text}")

    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        sys.exit(f"Не вдалося розпарсити відповідь тула {name}: {text[:500]}")


def carry_ids(points, cloud_points):
    """Перенести id існуючих точок з хмари в payload.

    ⚠️ КРИТИЧНО. set_route бере id з payload, а якщо його нема — ГЕНЕРУЄ новий.
    Зірочкові рейтинги (rate_place) живуть під id точки, тож точка з новим id
    стає «без оцінок» і всі голоси учасників зникають із показу. Методичка
    називає це «найлегшим способом мовчки знищити чуже голосування».

    Наш route_state.json генерується з MD і id не містить — тому переносимо їх
    сюди перед кожним повним записом.

    Зіставлення каскадом: (label, date) → унікальний label → округлені координати.
    """
    by_ld, by_l, by_xy = {}, {}, {}
    for c in cloud_points:
        cid = c.get("id")
        if not cid:
            continue
        by_ld[(c.get("label"), c.get("date"))] = cid
        by_l.setdefault(c.get("label"), []).append(cid)
        by_xy[(round(float(c["lat"]), 4), round(float(c["lon"]), 4))] = cid

    carried = 0
    for p in points:
        if p.get("id"):
            continue
        key = (p.get("label"), p.get("date"))
        cid = by_ld.get(key)
        if not cid:
            same = by_l.get(p.get("label")) or []
            cid = same[0] if len(same) == 1 else None
        if not cid:
            cid = by_xy.get((round(float(p["lat"]), 4), round(float(p["lon"]), 4)))
        if cid:
            p["id"] = cid
            carried += 1
    return carried


# ── route_state.json → аргументи set_route ───────────────────────────────────

def build_payload(state):
    """Повертає (payload, warnings). payload — лише ключі, які приймає set_route."""
    warnings = []

    unknown_top = sorted(set(state.keys()) - TOP_LEVEL_CONSUMED - set(LIVE_LAYER_KEYS))
    if unknown_top:
        warnings.append(f"невідомі top-level ключі у файлі проігноровано: {unknown_top}")

    for key in LIVE_LAYER_KEYS:
        val = state.get(key)
        if val:
            warnings.append(
                f"{key} непорожній ({len(val)} записів) у {STATE_DEFAULT.name} — це live-шар, "
                "яким володіють учасники карти; ІГНОРУЄМО (не пушимо і не чіпаємо в хмарі)"
            )

    raw_points = state.get("points") or []
    points = []
    dropped_keys = set()
    for p in raw_points:
        if not isinstance(p, dict):
            continue
        dropped_keys |= (set(p.keys()) - KNOWN_POINT_KEYS)
        points.append({k: v for k, v in p.items() if k in KNOWN_POINT_KEYS})
    if dropped_keys:
        warnings.append(f"з points[] відкинуто невідомі поля: {sorted(dropped_keys)}")

    payload = {"points": points}
    if state.get("country_info"):
        payload["country_info"] = state["country_info"]
    if state.get("booking_persons") is not None:
        payload["booking_persons"] = state["booking_persons"]

    return payload, warnings


def payload_size_bytes(payload):
    return len(json.dumps(payload, ensure_ascii=False).encode())


# ── .cloud-draft-id ───────────────────────────────────────────────────────────

def read_draft_id():
    if DRAFT_ID_FILE.exists():
        val = DRAFT_ID_FILE.read_text(encoding="utf-8").strip()
        return val or None
    return None


def write_draft_id(map_id):
    DRAFT_ID_FILE.write_text(map_id + "\n", encoding="utf-8")
    print(f"→ {DRAFT_ID_FILE}: {map_id}")


# ── допоміжний друк ───────────────────────────────────────────────────────────

def warn_if_not_osrm(legs_source):
    if legs_source and legs_source != "osrm":
        print(
            f"[WARN] legs_source={legs_source!r} (не 'osrm') — публічний OSRM "
            "(router.project-osrm.org) не відповів вчасно під час вимірювання на воркері, "
            "тому маршрут залито з прямими лініями (haversine). Повтори cloud_push.py "
            "пізніше — він перерахує legs і запише нову ревізію.",
            file=sys.stderr,
        )


def print_push_result(map_id, result):
    print(
        f"✓ set_route: map={map_id} rev={result.get('rev')} "
        f"points={len(result.get('points') or [])} "
        f"total_km={result.get('total_km')} total_min={result.get('total_min')} "
        f"legs_source={result.get('legs_source')}"
    )
    if result.get("snapshot_warning"):
        print(f"[WARN] {result['snapshot_warning']}", file=sys.stderr)
    warn_if_not_osrm(result.get("legs_source"))


def print_promote_result(from_map_id, to_map_id, result):
    print(
        f"✓ promote_draft: {from_map_id} → {to_map_id} rev={result.get('rev')} "
        f"points={len(result.get('points') or [])} "
        f"total_km={result.get('total_km')} total_min={result.get('total_min')} "
        f"legs_source={result.get('legs_source')}"
    )
    if result.get("snapshot_warning"):
        print(f"[WARN] {result['snapshot_warning']}", file=sys.stderr)
    warn_if_not_osrm(result.get("legs_source"))


# ── main ──────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--draft", metavar="NAME", help="створити приватну чернетку з такою назвою і залити маршрут у неї")
    p.add_argument("--map", metavar="MAP_ID", help="залити маршрут у вказану карту (інакше — .cloud-draft-id)")
    p.add_argument("--promote", metavar="TARGET_MAP_ID", help="після заливки викликати promote_draft у цю карту")
    p.add_argument("--state", default=str(STATE_DEFAULT), help=f"шлях до route_state.json (дефолт {STATE_DEFAULT})")
    p.add_argument("--dry-run", action="store_true", help="показати підсумок payload, нічого не відправляти")
    return p.parse_args()


def main():
    args = parse_args()

    if args.draft and args.map:
        sys.exit("--draft і --map взаємовиключні: --draft сам створює нову карту і пише її id у .cloud-draft-id")

    state_path = Path(args.state)
    if not state_path.exists():
        sys.exit(f"{state_path} не існує — спершу python3 scripts/generate_state_from_md.py")

    state = json.loads(state_path.read_text(encoding="utf-8"))
    payload, warnings = build_payload(state)
    size = payload_size_bytes(payload)

    if args.draft:
        map_desc = f"(буде створено create_map, назва «{args.draft}»)"
        existing_map_id = None
    else:
        existing_map_id = args.map or read_draft_id()
        map_desc = existing_map_id or "<не задано — вкажи --map або --draft, або .cloud-draft-id має існувати>"

    print(f"Стан: {state_path}")
    print(f"Карта: {map_desc}")
    print(f"Точок: {len(payload['points'])}   payload: {size} байт")
    if "country_info" in payload:
        print(f"country_info: {len(payload['country_info'])} країн(и)")
    if "booking_persons" in payload:
        print(f"booking_persons: {payload['booking_persons']}")
    for w in warnings:
        print(f"[WARN] {w}", file=sys.stderr)

    if size > 900_000:
        sys.exit(f"payload завеликий ({size} байт) — ліміт документа Firestore 1 МБ. Скороти popup_html/extra_info.")
    if not payload["points"]:
        sys.exit("route_state.json не містить точок — спершу generate_state_from_md.py")

    if args.dry_run:
        print("[DRY RUN] нічого не відправлено в хмару")
        if args.promote:
            print(f"[DRY RUN] після заливки викликав би promote_draft(from={map_desc}, to={args.promote})")
        return

    env_vars = load_env_file()
    worker = get_worker(env_vars)

    if args.draft:
        token = get_access_token(env_vars, worker)
        created = call_tool(worker, token, "create_map", {"name": args.draft, "draft": True})
        map_id = created.get("map_id")
        if not map_id:
            sys.exit(f"create_map не повернув map_id: {created}")
        write_draft_id(map_id)
        print(f"✓ create_map: {map_id} «{args.draft}» draft={created.get('draft')} — {created.get('url', '')}")
    else:
        map_id = existing_map_id
        if not map_id:
            sys.exit("Немає карти: вкажи --map <id>, або спершу --draft \"Назва\" (запише .cloud-draft-id)")
        token = get_access_token(env_vars, worker)

    route = call_tool(worker, token, "get_route", {"map_id": map_id}, strict=False)
    if route is None:
        # Чернетка одноразова: після promote власник міг її видалити, і це
        # штатний стан, а не збій. Воркер віддає 403, що читається як проблема
        # з правами — тому не переносимо цю плутанину далі, а просто
        # створюємо нову. Явну карту (--map) НЕ підміняємо ніколи.
        if args.map:
            sys.exit(f"Карта {map_id} недоступна або не існує. Перевір id "
                     f"(list_my_maps) — явно вказану карту не підміняю.")
        name = args.draft or f"чернетка {state_path.stem}"
        print(f"↻ Чернетки {map_id} більше немає (видалена після promote?) — створюю нову.")
        created = call_tool(worker, token, "create_map", {"name": name, "draft": True})
        map_id = created.get("map_id")
        if not map_id:
            sys.exit(f"create_map не повернув map_id: {created}")
        write_draft_id(map_id)
        print(f"✓ create_map: {map_id} «{name}» — {created.get('url', '')}")
        route = call_tool(worker, token, "get_route", {"map_id": map_id})
    base_rev = route.get("rev")

    # Перенести id існуючих точок — інакше рейтинги учасників осиротіють.
    carried = carry_ids(payload["points"], route.get("points") or [])
    fresh = len(payload["points"]) - carried
    rated = len(route.get("ratings") or {})
    print(f"id збережено: {carried}, нових точок: {fresh}"
          + (f" · оцінок на карті: {rated}" if rated else ""))

    set_args = dict(payload)
    set_args["map_id"] = map_id
    if base_rev is not None:
        set_args["base_rev"] = base_rev

    result = call_tool(worker, token, "set_route", set_args)
    print_push_result(map_id, result)

    if args.promote:
        target_route = call_tool(worker, token, "get_route", {"map_id": args.promote})
        target_base_rev = target_route.get("rev")
        promote_args = {"from_map_id": map_id, "to_map_id": args.promote}
        if target_base_rev is not None:
            promote_args["base_rev"] = target_base_rev
        presult = call_tool(worker, token, "promote_draft", promote_args)
        print_promote_result(map_id, args.promote, presult)


if __name__ == "__main__":
    main()
