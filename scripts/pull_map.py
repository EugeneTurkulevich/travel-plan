"""
scripts/pull_map.py

READ-ONLY знімок хмарної карти в локальний файл. Жодного write-тула не
викликає — лише get_map/get_route/list_alt_route/list_marks/get_places/
list_alts/get_route_brief.

НАВІЩО. Карта — спільна й живе окремо від репозиторію (CLAUDE.md, «Три
рівні»): маршрут, альтернативи, мітки, рейтинги, треди — усе там може
змінитись без нас (інший агент, власник карти). Перш ніж будувати план
публікації або просто зрозуміти «що зараз на карті», потрібен один файл,
який можна прочитати офлайн і в дифі — без походу в мережу щоразу. Це і є
live_mirror.json: дзеркало, не джерело істини. Джерело істини лишається
карта; редагувати цей файл руками безглуздо — наступний запуск перезапише.

merge_cloud_photos.py вже робив вужчу версію того самого (тягнув лише
фото POI з get_route). Цей скрипт — загальний «pull-before-push»: шість
тулів разом (плюс по одному get_route_brief на кожну збірку-альтернативу
з list_alts), один файл, один людський звіт про розбіжності.

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
    """Шість read-тулів по черзі, жодного виклику, що пише на карту — плюс
    get_route_brief один раз на кожну збірку-альтернативу, яку віддав
    list_alts (окремий шар route_alts, не list_alt_route/`alt`: та — точки-
    кандидати, це — цілі альтернативні збірки маршруту)."""
    m = call_tool(worker, token, "get_map", {"map_id": map_id})
    route = call_tool(worker, token, "get_route", {"map_id": map_id})
    alt = call_tool(worker, token, "list_alt_route", {"map_id": map_id})
    marks = call_tool(worker, token, "list_marks", {"map_id": map_id})
    places = call_tool(worker, token, "get_places", {"map_id": map_id})
    list_alts_result = call_tool(worker, token, "list_alts", {"map_id": map_id})
    alt_briefs = {}
    for a in (list_alts_result.get("alts") or []):
        alt_id = a.get("alt_id")
        if not alt_id:
            continue
        alt_briefs[alt_id] = call_tool(worker, token, "get_route_brief",
                                        {"map_id": map_id, "alt_id": alt_id})
    return m, route, alt, marks, places, list_alts_result, alt_briefs


def build_mirror(map_id, m, route, alt, marks, places, list_alts_result, alt_briefs):
    """Точний формат — контракт. Масиви йдуть як віддав тул, без нормалізації;
    порожні поля — {}/[], ніколи null.

    ТРИ свідомі рішення, усі оголошені в самому файлі:

    1. `route` і `alt` беремо ЦІЛКОМ, а не за списком полів. Перша редакція
       контракту перелічувала для route points/booked/ratings/threads/links/
       booking_persons/country_info — і мовчки губила schedule/legs_km_auto/
       legs_min_auto/legs_source, які get_route теж віддає. Для `alt` та сама
       вада повторилась мовчки 08.08.2026: перелік points/links/ratings
       губив новий poi_spread з list_alt_route, доки не перевели на той
       самий принцип. Для дзеркала, по якому потім будується план публікації,
       це дірка: те, що порахував сервер, — саме те, з чим ми звіряємось.
       Копіюємо все, іменовані ключі лише ґарантуємо, щоб споживачі не
       писали перевірок на відсутність.

    2. `alt.points` СОРТУЄМО за id. Воркер віддає їх у нестабільному порядку:
       набір і вміст ті самі, а порядок різний щоразу — через це кожен diff
       дзеркала був би шумом. Сортування тут — не «підганяння під очікуване»,
       а умова придатності файлу до порівняння, і воно оголошене в `_alt_order`.
       Причина (08.08.2026): `live.alt_points` — Firestore map-поле, воно не
       гарантує порядок ключів між читаннями (на відміну від array-полів).
       Подано воркеру: `map-server/docs/ALT-POINTS-ORDER-DESIGN.md`.

    3. Шар збірок-альтернатив маршруту (`route_alts`, окремо від `alt` —
       той шар про точки-кандидати з list_alt_route) — теж беремо ЦІЛКОМ:
       `list_alts` як є (порядок його `alts` лишаємо — детермінований,
       updated_at desc), плюс по одному `get_route_brief(alt_id)` на кожну
       збірку з відповіді. Ключі словника `briefs` сортуємо за alt_id — сам
       список збірок міг прийти в тому самому порядку щоразу (updated_at
       desc), але порядок ключів словника не гарантований, і без сортування
       diff файлу був би шумом так само, як з alt.points у пункті 2.
       `diff_alts` свідомо НЕ кладемо в дзеркало: це похідний звіт
       (порівняння двох збірок чи ревізій), не стан карти — рахується на
       вимогу, а не тягнеться про запас.
    """
    pulled_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")

    route_all = dict(route)
    route_all.pop("rev", None)                     # rev живе на верхньому рівні
    for key, empty in (("points", []), ("booked", {}), ("ratings", {}),
                       ("threads", {}), ("links", []), ("country_info", {})):
        route_all[key] = route_all.get(key) or empty
    route_all["booking_persons"] = route_all.get("booking_persons") or 0

    alt_all = dict(alt)
    alt_all["points"] = sorted(alt.get("points") or [], key=lambda p: p.get("id") or "")
    alt_all["links"] = alt_all.get("links") or []
    alt_all["ratings"] = alt_all.get("ratings") or {}

    route_alts = dict(list_alts_result)        # вербатим, за принципом п.1
    route_alts["alts"] = route_alts.get("alts") or []
    route_alts["briefs"] = {alt_id: alt_briefs[alt_id] for alt_id in sorted(alt_briefs)}

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
                       "до diff. Решта масивів — як віддав тул. Ключі "
                       "route_alts.briefs — за alt_id, з тієї ж причини."),
        "route": route_all,
        "alt": alt_all,
        "route_alts": route_alts,
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

    route_alts = mirror["route_alts"]["alts"]

    print(f"маршрут      {len(points)} точки · rev {mirror['rev']}")
    print(f"контент      {poi_total} POI · {poi_with_photo} з фото")
    print(f"альтернативи {len(alt['points'])} · звʼязків {len(alt['links'])}")
    if route_alts:
        names = " · ".join(a.get("name") or a.get("alt_id") or "?" for a in route_alts)
        print(f"збірки       {len(route_alts)} route_alts ({names})")
    else:
        print("збірки       0 route_alts")
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

    m, route, alt, marks, places, list_alts_result, alt_briefs = pull(worker, token, map_id)
    mirror = build_mirror(map_id, m, route, alt, marks, places, list_alts_result, alt_briefs)

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
