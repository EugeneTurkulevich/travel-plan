"""
scripts/pull_map.py

READ-ONLY знімок хмарної карти в локальний файл. Жодного write-тула не
викликає — лише get_map/get_route/list_alt_route/list_marks/get_places.

НАВІЩО. Карта — спільна й живе окремо від репозиторію (CLAUDE.md, «Три
рівні»): маршрут, альтернативи, мітки, рейтинги, треди — усе там може
змінитись без нас (інший агент, власник карти). Перш ніж будувати план
публікації або просто зрозуміти «що зараз на карті», потрібен один файл,
який можна прочитати офлайн і в дифі — без походу в мережу щоразу. Це і є
live_mirror.json: дзеркало, не джерело істини. Джерело істини лишається
карта; редагувати цей файл руками безглуздо — наступний запуск перезапише.

merge_cloud_photos.py вже робив вужчу версію того самого (тягнув лише
фото POI з get_route). Цей скрипт — загальний «pull-before-push»: пʼять
тулів разом, один файл, один людський звіт про розбіжності.

Usage:
    python3 scripts/pull_map.py              # знімок активного профілю
    python3 scripts/pull_map.py --map <id>   # знімок довільної карти (профіль НЕ змінює)
    python3 scripts/pull_map.py --quiet      # без людського звіту, тільки записати файл

Потрібен токен у .env кореня даних (те саме, що cloud_push.py). Токен
ніколи не друкується.
"""
import argparse
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cloud_push import CTX, call_tool, get_access_token, get_worker, load_env_file  # noqa: E402


def pull(worker, token, map_id):
    """Пʼять read-тулів по черзі. Жодного виклику, що пише на карту."""
    m = call_tool(worker, token, "get_map", {"map_id": map_id})
    route = call_tool(worker, token, "get_route", {"map_id": map_id})
    alt = call_tool(worker, token, "list_alt_route", {"map_id": map_id})
    marks = call_tool(worker, token, "list_marks", {"map_id": map_id})
    places = call_tool(worker, token, "get_places", {"map_id": map_id})
    return m, route, alt, marks, places


def build_mirror(map_id, m, route, alt, marks, places):
    """Точний формат — контракт. Масиви йдуть як віддав тул, без нормалізації;
    порожні поля — {}/[], ніколи null.

    ДВА свідомі рішення, обидва оголошені в самому файлі:

    1. `route` беремо ЦІЛКОМ, а не за списком полів. Перша редакція контракту
       перелічувала points/booked/ratings/threads/links/booking_persons/
       country_info — і мовчки губила schedule/legs_km_auto/legs_min_auto/
       legs_source, які get_route теж віддає. Для дзеркала, по якому потім
       будується план публікації, це дірка: розклад, порахований сервером, —
       саме те, з чим ми звіряємось. Копіюємо все, іменовані ключі лише
       ґарантуємо, щоб споживачі не писали перевірок на відсутність.

    2. `alt.points` СОРТУЄМО за id. Воркер віддає їх у нестабільному порядку:
       набір і вміст ті самі, а порядок різний щоразу — через це кожен diff
       дзеркала був би шумом. Сортування тут — не «підганяння під очікуване»,
       а умова придатності файлу до порівняння, і воно оголошене в `_alt_order`.
    """
    pulled_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")

    route_all = dict(route)
    route_all.pop("rev", None)                     # rev живе на верхньому рівні
    for key, empty in (("points", []), ("booked", {}), ("ratings", {}),
                       ("threads", {}), ("links", []), ("country_info", {})):
        route_all[key] = route_all.get(key) or empty
    route_all["booking_persons"] = route_all.get("booking_persons") or 0

    alt_points = sorted(alt.get("points") or [], key=lambda p: p.get("id") or "")

    return {
        "_": (
            "Дзеркало route- і live-шарів карти. ДЖЕРЕЛО ІСТИНИ — КАРТА, не "
            "цей файл. Редагувати безглуздо: наступний pull_map.py "
            "перезапише. Оновити: python3 scripts/pull_map.py"
        ),
        "pulled_at": pulled_at,
        "map_id": map_id,
        "map_name": m.get("name") or "",
        "server_tools_rev": m.get("server_tools_rev") or "",
        "rev": route.get("rev") or 0,
        "trip_rules": m.get("trip_rules") or "",
        "members_count": m.get("members_count") or 0,
        "_alt_order": ("alt.points відсортовано за id: воркер віддає їх у "
                       "нестабільному порядку, а дзеркало має бути придатним "
                       "до diff. Решта масивів — як віддав тул."),
        "route": route_all,
        "alt": {
            "points": alt_points,
            "links": alt.get("links") or [],
            "ratings": alt.get("ratings") or {},
        },
        "marks": marks.get("marks") or [],
        "places": places,
    }


# ── людський звіт ────────────────────────────────────────────────────────────

def print_report(mirror):
    route = mirror["route"]
    alt = mirror["alt"]
    points = route["points"]

    poi_total = 0
    poi_with_photo = 0
    for p in points:
        nested = p.get("nested_points") or []
        poi_total += len(nested)
        poi_with_photo += sum(1 for n in nested if n.get("photo_url"))

    marks = mirror["marks"]
    open_marks = sum(1 for mk in marks if (mk.get("status") or "open") == "open")

    # Треди — місця з непорожнім обговоренням: route.threads (dict за id, лише
    # непорожні списки) + альтернативи з непорожнім полем comments (там зараз
    # і живуть треди, див. scripts/README.md).
    thread_places = {pid for pid, cs in (route.get("threads") or {}).items() if cs}
    thread_places |= {p.get("id") for p in alt["points"] if p.get("comments")}

    rated_places = len(route["ratings"]) + len(alt["ratings"])

    print(f"маршрут      {len(points)} точки · rev {mirror['rev']}")
    print(f"контент      {poi_total} POI · {poi_with_photo} з фото")
    print(f"альтернативи {len(alt['points'])} · звʼязків {len(alt['links'])}")
    print(f"мітки        {len(marks)} · відкритих {open_marks}")
    print(f"обговорення  треди на {len(thread_places)} місцях")
    print(f"рейтинги     {rated_places} місця оцінено")
    print(f"бронювання   {len(route['booked'])}")

    stale = [p.get("label") or p.get("id") for p in points if p.get("timing_stale")]
    if stale:
        print(f"❗ timing_stale: {', '.join(stale)}")

    server_rev = mirror["server_tools_rev"]
    profile_rev = CTX.trip.get("server_tools_rev")
    if profile_rev is None:
        print(f"server_tools_rev карти: {server_rev}")
    elif profile_rev != server_rev:
        print(
            f"❗ server_tools_rev не збігається: профіль знає «{profile_rev}», "
            f"карта віддає «{server_rev}» — набір тулів застарів, перепідключи "
            "конектор (/mcp)."
        )
    else:
        print(f"server_tools_rev карти: {server_rev} (збігається з профілем)")


# ── main ──────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--map", metavar="MAP_ID", help="знімок довільної карти замість активного профілю (профіль не змінюється)")
    p.add_argument("--quiet", action="store_true", help="без людського звіту, тільки записати файл")
    return p.parse_args()


def main():
    args = parse_args()
    map_id = args.map or CTX.map_id
    if not map_id:
        sys.exit("Немає map_id: активний профіль без map_id і --map не вказано.")

    env_vars = load_env_file()
    worker = get_worker(env_vars)
    token = get_access_token(env_vars, worker)

    m, route, alt, marks, places = pull(worker, token, map_id)
    mirror = build_mirror(map_id, m, route, alt, marks, places)

    # Знімок ЧУЖОЇ карти не має права стати дзеркалом профілю: план публікації
    # звірявся б із зовсім іншою картою й не помітив цього.
    target = CTX.live_mirror
    if args.map and args.map != CTX.map_id:
        target = CTX.live_mirror.with_name(f"live_mirror.{map_id}.json")
        print(f"⚠️  --map {map_id} ≠ карта профілю {CTX.map_id or '—'} — пишу в "
              f"окремий файл {target.name}, дзеркало профілю не чіпаю.",
              file=sys.stderr)

    CTX.write_json(target, mirror)

    if not args.quiet:
        print_report(mirror)
        print(f"\n→ {target}")


if __name__ == "__main__":
    main()
