"""
scripts/trip.py

Вибір поїздки, з якою працюємо. Профіль — це не «налаштування», а відповідь на
питання «чий зараз маршрут»: від нього залежить, у яку карту піде публікація.

    python3 scripts/trip.py                 # хто активний + стан
    python3 scripts/trip.py list            # усі профілі в корені даних
    python3 scripts/trip.py use <id>        # перемкнути (пише .active-trip)
    python3 scripts/trip.py new <id>        # створити профіль
    python3 scripts/trip.py import <map_id> # зібрати профіль з карти

`show` навмисно друкує map_id повністю: перемикання профілю — це той момент,
коли треба бачити, куди саме поїде наступний пуш.
"""
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from trip_ctx import (  # noqa: E402
    TripContext, TripContextError, context, list_trips, resolve_data_root,
)

# Дефолт-воркер — той самий рядок, що DEFAULT_WORKER у cloud_push.py.
# НЕ імпортуємо cloud_push тут заради константи: модуль виконує `CTX = paths()`
# на рівні імпорту (див. cmd_import нижче) і впаде, якщо активного профілю ще
# нема, — а саме такий момент і є `trip.py new`/`import` до створення профілю.
WORKER_DEFAULT = "https://trip-map-mcp.e--t.workers.dev"

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")

# Ролі точок, для яких «та сама точка знову зустрілась на карті» — очікуваний
# і правильний привід переприв'язати другий запис до вже заведеної картки
# (див. cmd_import._do_import, self-merge коментар біля резервування нового
# слага). «visit» свідомо НЕ тут: дві різні пам'ятки поруч не мають злитись
# в одну картку лише тому, що координати в тому самому порозі.
SELF_MERGE_KINDS = {"home", "overnight", "border"}


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


# ── дрібні спільні помічники для new/import ──────────────────────────────────

def flag_value(argv, name):
    """--name value → value; --name=value теж. Не мутує argv."""
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def parse_countries(raw):
    """"UA,RO" → ["UA","RO"]. Пустий/None → []. Некоректний код — ValueError
    з людським текстом (виклик має надрукувати його й повернути код 2), бо
    мовчки відкинути код країни й мовчки продовжити — той самий сурогат,
    якого тулкіт свідомо уникає (див. sync_from_map.py resolve_new_place_slug)."""
    if not raw:
        return []
    out = []
    for code in raw.split(","):
        code = code.strip().upper()
        if not code:
            continue
        if not COUNTRY_RE.match(code):
            raise ValueError(
                f"Некоректний код країни «{code}» у --countries "
                f"(потрібно ISO 3166-1 alpha-2, напр. UA,RO)"
            )
        out.append(code)
    return out


def new_trip_skeleton(trip_id, title, start, end, countries, map_id):
    """Каркас trip.json — поля за схемою trips/<id>/trip.json, непередані
    лишаються порожніми (рядок/масив), а не вгаданими. tz_default теж
    порожній: жодного CLI-параметра під нього немає, а вигаданий зсув
    (напр. "+00:00") виглядав би як рішення, яким не є."""
    return {
        "id": trip_id,
        "title": title or "",
        "start": start or "",
        "end": end or "",
        "countries": countries or [],
        "tz_default": "",
        "tz_overrides": {},
        "map_id": map_id or "",
        "worker": WORKER_DEFAULT,
        "status": "active",
        "poi_categories": {},
    }


def print_fillin_advice(trip):
    """Спільний текст «що дозаповнити» — new і import хочуть той самий
    список, лише з різним приводом (SKILL.md /trip: «Нова поїздка»)."""
    print("\nДозаповнити:")
    if not trip.get("map_id"):
        print("  · map_id — створи карту (create_map) або підхопи наявну: "
              "trip.py import <map_id>")
    print("  · trip_rules (дати, старт/фініш, кільцевий чи лінійний маршрут, "
          "інтереси/темп/бюджет) — розмова за методичкою воркера; збережи "
          "відповідь на КАРТІ через set_trip_rules, показавши текст перед "
          "записом. У trip.json ці правила НЕ копіюються.")
    if not trip.get("tz_default"):
        print("  · tz_default — часовий пояс поїздки (напр. \"+02:00\"), впиши в trip.json руками")
    if trip.get("poi_categories"):
        print("  · poi_categories — виведено з карти автоматично (типи POI, які вже там "
              "проставлені); звір із trip_rules і додай needs_overnight/skip_geo_audit руками, "
              "де треба, — їх ніхто не вгадує")
    else:
        print("  · poi_categories — після того, як trip_rules відомі (таблиця "
              "«MD-секція ↔ type», а не самі правила)")


# ── new ───────────────────────────────────────────────────────────────────────

def cmd_new(argv, trip_id):
    if not ID_RE.match(trip_id):
        print(f"Некоректний id профілю «{trip_id}»: лише [a-z0-9-], перший символ — [a-z0-9].",
              file=sys.stderr)
        return 2

    root = resolve_data_root(argv)
    trip_dir = root / "trips" / trip_id
    if trip_dir.exists():
        print(f"⚠️  Профіль «{trip_id}» уже існує ({trip_dir}) — нічого не змінено.",
              file=sys.stderr)
        return 1

    try:
        countries = parse_countries(flag_value(argv, "--countries"))
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    trip = new_trip_skeleton(
        trip_id,
        flag_value(argv, "--title"),
        flag_value(argv, "--start"),
        flag_value(argv, "--end"),
        countries,
        flag_value(argv, "--map"),
    )
    route = {"trip": trip_id, "days": [], "alternates": []}

    trip_dir.mkdir(parents=True)
    (trip_dir / "overlay").mkdir()
    (trip_dir / "exports").mkdir()
    TripContext.write_json(trip_dir / "trip.json", trip)
    TripContext.write_json(trip_dir / "route.json", route)

    print(f"✓ створено профіль «{trip_id}» ({trip_dir})")
    blank = [k for k in ("title", "start", "end", "map_id") if not trip[k]]
    if not countries:
        blank.append("countries")
    print(f"  порожні поля: {', '.join(blank) if blank else '(нема)'}, tz_default")
    print_fillin_advice(trip)
    print(f"\nПрофіль СТВОРЕНО, але НЕ активовано (щоб не перемкнути випадково): "
          f"python3 scripts/trip.py use {trip_id}")
    return 0


# ── import ───────────────────────────────────────────────────────────────────

def harvest_poi_categories(route_points, alt_points):
    """{type: {"icon": ..., "section": ...}} — ВИВЕДЕНО зі спостережених
    `type`/`type_icon`/`type_name` на nested_points точок маршруту й
    альтернатив, не вигадано: карта вже несе цю категоризацію (кожен POI
    proставлений з конкретним типом), sync_from_map.build_new_card_md лише
    розкладає картки по секціях за нею (group_nested_by_category). Без цього
    кроку всі POI-типи, яких нема серед poi_categories, зсипаються в базову
    секцію "Цікаві POI" — та сама вада, яку 07.08.2026 вже ловили планом
    публікації для sync_from_map (58 зайвих update_poi).

    Базовий `type == "poi"` СВІДОМО не потрапляє в результат: він завжди йде
    в фіксовану BASE_POI_SECTION (render_category_section), а не через
    таблицю poi_categories — так само, як робить сам generate_state.py.

    needs_overnight / skip_geo_audit НЕ проставляються: методичка воркера
    забороняє вгадувати їх (SKILL.md «Нова поїздка») — обидва мають зʼявитись
    лише як явне рішення людини, звірене з trip_rules.

    Повертає (poi_categories, notes) — notes лише коли type_name на карті
    порожній (тоді секція = сам ключ type) або коли для одного type
    трапляються РІЗНІ icon/type_name (узято найчастіший, розбіжність
    названа, не проковтнута мовчки)."""
    from collections import Counter

    seen = {}
    for mp in list(route_points) + list(alt_points):
        for n in (mp.get("nested_points") or []):
            t = n.get("type")
            if not t or t == "poi":
                continue
            icon = n.get("type_icon") or n.get("icon") or ""
            name = n.get("type_name") or ""
            seen.setdefault(t, Counter())[(icon, name)] += 1

    poi_categories = {}
    notes = []
    for t in sorted(seen):
        (icon, name), _hits = seen[t].most_common(1)[0]
        if not name:
            notes.append(f"  · {t}: type_name на карті порожній — секція = «{t}»")
            name = t
        poi_categories[t] = {"icon": icon, "section": name}
        if len(seen[t]) > 1:
            variants = ", ".join(f"{i!r}/{nm!r}×{c}" for (i, nm), c in seen[t].most_common())
            notes.append(f"  · {t}: на карті трапляються різні icon/type_name ({variants}) — "
                         f"узято найчастіший")
    return poi_categories, notes


class _ImportAbort(RuntimeError):
    """Керована зупинка імпорту після того, як trip_dir уже створено —
    сигнал прибрати каталог і вийти з кодом 1, а не лишити «половину профілю»."""


def cmd_import(argv, map_id, as_id):
    profile_id = as_id or map_id
    if not ID_RE.match(profile_id):
        print(f"Некоректний id профілю «{profile_id}» (з --as або map_id): "
              f"лише [a-z0-9-], перший символ — [a-z0-9].", file=sys.stderr)
        return 2

    root = resolve_data_root(argv)
    trip_dir = root / "trips" / profile_id
    if trip_dir.exists():
        print(f"⚠️  Профіль «{profile_id}» уже існує ({trip_dir}) — імпорт зупинено, "
              f"нічого не чіпав.", file=sys.stderr)
        return 1

    # Мінімальний каркас ДО читання карти: cloud_push/pull_map/sync_from_map
    # усі резолвлять активний профіль через trip_ctx.paths() на рівні
    # ІМПОРТУ модуля — без наявного trips/<id>/trip.json вони падають ще до
    # першого виклику функції. .active-trip НЕ чіпаємо (команда не перемикає
    # профіль) — резолв іде через $TRIP нижче.
    trip_dir.mkdir(parents=True)
    (trip_dir / "overlay").mkdir()
    (trip_dir / "exports").mkdir()
    placeholder = new_trip_skeleton(profile_id, None, None, None, None, map_id)
    TripContext.write_json(trip_dir / "trip.json", placeholder)
    TripContext.write_json(trip_dir / "route.json", {"trip": profile_id, "days": [], "alternates": []})

    os.environ["TRIP"] = profile_id
    os.environ.setdefault("TRAVEL_DATA", str(root))

    try:
        result = _do_import(argv, profile_id, map_id, trip_dir, placeholder)
    except _ImportAbort as e:
        shutil.rmtree(trip_dir, ignore_errors=True)
        print(str(e), file=sys.stderr)
        return 1
    except SystemExit:
        shutil.rmtree(trip_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(trip_dir, ignore_errors=True)
        print(f"⚠️  Імпорт не вдався: {e}. Каркас профілю прибрано, нічого не лишилось "
              f"наполовину.", file=sys.stderr)
        return 1

    print_report_import(result, profile_id, map_id, trip_dir)
    print(f"\nПрофіль СТВОРЕНО, але НЕ активовано (щоб не перемкнути випадково): "
          f"python3 scripts/trip.py use {profile_id}")
    return 0


def _do_import(argv, profile_id, map_id, trip_dir, placeholder):
    """Мережа + запис файлів. Кидає _ImportAbort на керовану зупинку (вироджені
    slug-и — те саме правило, що в sync_from_map.py). Повертає dict для звіту."""
    import cloud_push
    import pull_map as pull_map_mod
    import sync_from_map as sfm

    env_vars = cloud_push.load_env_file()
    worker = cloud_push.get_worker(env_vars)
    token = cloud_push.get_access_token(env_vars, worker)

    m, route, alt, marks, places = pull_map_mod.pull(worker, token, map_id)
    mirror = pull_map_mod.build_mirror(map_id, m, route, alt, marks, places)

    ctx = context(argv, trip_id=profile_id)
    ctx.write_json(ctx.live_mirror, mirror)

    import_date_ukr = sfm.iso_to_ukr_date((mirror.get("pulled_at") or "")[:10])
    route_points = mirror["route"]["points"]
    alt_points = mirror["alt"]["points"]
    poi_categories, poi_category_notes = harvest_poi_categories(route_points, alt_points)

    library_index = sfm.load_library_index(ctx.places_dir)
    taken_slugs = {c["slug"] for c in library_index}
    lib_text_by_slug = {c["slug"]: c["text"] for c in library_index}
    slugmap = sfm.load_slugmap()
    map_slugs = sfm.load_map_slugs(slugmap)
    # sfm.load_slugmap_may_count() свідомо не використовується тут: та
    # звірка належить sfm.process_alternates(), яку import не викликає
    # (див. коментар нижче біля обробки альтернатив).

    report = sfm.Report()
    new_cards_pending = []
    cards_to_regenerate = []
    queued_regen_slugs = set()

    # ── дні: групуємо точки маршруту за датою (своя карта, немає «старого»
    # route.json, з якого sync_from_map.build_new_days бере скелет днів) ──
    points_by_date = {}
    dateless_points = []
    for mp in route_points:
        date = mp.get("date")
        if not date:
            dateless_points.append(mp)
            continue
        points_by_date.setdefault(date, []).append(mp)

    # Пул «щойно заведених карток, які МОЖНА повторно знайти в цьому ж
    # прогоні» — окремо від library_index (реальні картки з диска, звірені
    # sfm.find_library_match за координатами БЕЗ огляду на kind). Той самий
    # фізичний об'єкт справді зустрічається двічі під різними map_point_id:
    # домашня база на старті й фініші кільцевого маршруту (home), та сама
    # ночівля кілька разів (overnight), той самий кордон під різними
    # підписами напрямку (border, route.schema.json: point.label). Без цього
    # пулу друге входження тієї ж точки створювало б ДРУГУ картку з тим самим
    # змістом (`slug-2`) — реально трапилось на тестовому імпорті: старт і
    # фініш у тому самому місті дали два записи.
    #
    # Пошук у пулі — ЛИШЕ серед записів ТОГО САМОГО kind (окремий список на
    # kind, а не спільний library_index): інакше той самий поріг
    # _COORD_THRESHOLD (~5 км) зловив би ДВІ РІЗНІ пам'ятки поруч як «одне
    # місце» — на тестовому імпорті overnight-точка помилково притягла
    # наступного дня visit-точку за ~2.7 км (той самий гірський район, інша
    # назва), поки перевірку kind не додали. Для visit картка на дублікат
    # (якщо він справді трапиться) лишається окремою — дешевше виправити
    # руками (/place), ніж мовчки приписати опис одного місця іншому.
    self_merge_pool = {k: [] for k in SELF_MERGE_KINDS}

    days = []
    for i, date in enumerate(sorted(points_by_date), start=1):
        day_points = []
        for mp in points_by_date[date]:
            kind = mp.get("kind")
            pool = self_merge_pool.get(kind) or []
            visible_index = library_index + pool if pool else library_index
            slug, _old, origin = sfm.resolve_point_slug(
                mp, {}, visible_index, taken_slugs, map_slugs, lib_text_by_slug,
                queued_regen_slugs, report, new_cards_pending, cards_to_regenerate,
            )
            if slug is None:
                continue  # вироджений slug — причина вже в report.unresolved_slugs
            if origin == "new" and kind in SELF_MERGE_KINDS:
                self_merge_pool[kind].append({
                    "slug": slug, "title": mp.get("label") or slug,
                    "lat": mp.get("lat"), "lon": mp.get("lon"), "path": None, "text": "",
                })
            point = {"id": slug, "map_point_id": mp["id"], "kind": kind}
            if mp.get("nights") and mp["nights"] > 1:
                point["nights"] = mp["nights"]
            if mp.get("arrive_at") is not None:
                point["arrive_at"] = mp["arrive_at"]
            if mp.get("stay_minutes") is not None:
                point["stay_minutes"] = mp["stay_minutes"]
            day_points.append(point)
        days.append({"day": i, "date": date, "points": day_points})

    # НЕ sfm.process_alternates(): та функція ще й відкладає альтернативи з
    # маркером «з травневої карти» в коментарях (_MAY_CANDIDATE_MARKER) —
    # це логіка МІГРАЦІЇ конкретного профілю (Ф6 slugmap.json), а не власне
    # import. Тут беремо/створюємо картку для КОЖНОЇ альтернативи карти —
    # решта (matched-by-coords / new / regenerate) та сама логіка.
    new_alternates = []
    for ap in alt_points:
        name = ap.get("name") or ap["id"]
        lat, lon = ap.get("lat"), ap.get("lon")

        lib_slug = sfm.find_library_match(lat, lon, name, library_index, report)
        if lib_slug:
            sfm.maybe_queue_regeneration(
                lib_slug, lib_text_by_slug, queued_regen_slugs, name, lat, lon,
                ap.get("popup_html"), ap.get("nested_points"), ap.get("extra_info"),
                f"альтернатива «{name}» (перегенеровано)", cards_to_regenerate, report,
            )
            slug = lib_slug
        else:
            code, country_name, flag = sfm.guess_country(lat, lon)
            base_slug = sfm.resolve_new_place_slug(
                ap.get("id"), name, lat, lon, code, map_slugs, report, "альтернатива",
            )
            if base_slug is None:
                continue  # вироджений slug — причина вже в report.unresolved_slugs
            slug = sfm.unique_slug(base_slug, taken_slugs)
            taken_slugs.add(slug)
            new_cards_pending.append({
                "slug": slug, "title": name, "lat": lat, "lon": lon,
                "code": code, "country_name": country_name, "flag": flag,
                "popup_html": ap.get("popup_html"), "nested_points": ap.get("nested_points") or [],
                "extra_info": ap.get("extra_info") or [],
                "source": f"альтернатива «{name}»",
            })
            report.new_cards.append((slug, name, lat, lon, code, "альтернатива"))
            # НЕ реєструємо в library_index (на відміну від циклу днів вище):
            # альтернативи не мають "kind", тому нема дешевого способу
            # відрізнити «та сама точка вдруге» від «інша пам'ятка поруч» —
            # той самий ризик хибного злиття, задля якого цикл днів обмежили
            # SELF_MERGE_KINDS. Дублікат картки для двох різних альтернатив
            # поруч трапляється рідко й дешево виправляється руками (/place);
            # мовчки приписаний чужий опис — ні.
        note = sfm.synthesize_alt_note(name, ap.get("popup_html"), import_date_ukr)
        new_alternates.append({"id": slug, "note": note})

    sfm.check_role_divergence(days, library_index, report)

    if report.unresolved_slugs:
        raise _ImportAbort(
            "=" * 70 + "\n"
            "ЗУПИНКА імпорту: не вдалось вивести slug для нових місць\n"
            + "=" * 70 + "\n"
            + "\n".join(report.unresolved_slugs) +
            "\n\nВпиши бракуючі id в _migration/slugmap.json → map_slugs і повтори."
        )

    for spec in new_cards_pending + cards_to_regenerate:
        card_md = sfm.build_new_card_md(
            spec["slug"], spec["title"], spec["lat"], spec["lon"], spec["code"],
            spec["country_name"], spec["flag"], spec["popup_html"], spec["nested_points"],
            spec["extra_info"], import_date_ukr, poi_categories,
        )
        ctx.places_dir.mkdir(parents=True, exist_ok=True)
        (ctx.places_dir / f"{spec['slug']}.md").write_text(card_md, encoding="utf-8")

    dates_all = sorted(points_by_date)
    countries = sorted((mirror["route"].get("country_info") or {}).keys())
    trip = dict(placeholder)
    trip["title"] = mirror.get("map_name") or ""
    trip["start"] = dates_all[0] if dates_all else ""
    trip["end"] = dates_all[-1] if dates_all else ""
    trip["countries"] = countries
    trip["poi_categories"] = poi_categories
    ctx.write_json(ctx.trip_json, trip)
    ctx.write_json(ctx.route_json, {"trip": profile_id, "days": days, "alternates": new_alternates})

    return {
        "trip": trip, "days": days, "alternates": new_alternates,
        "new_cards": new_cards_pending, "regenerated": cards_to_regenerate,
        "report": report, "dateless_points": dateless_points,
        "poi_categories": poi_categories, "poi_category_notes": poi_category_notes,
    }


def print_report_import(result, profile_id, map_id, trip_dir):
    trip, days, alternates = result["trip"], result["days"], result["alternates"]
    report = result["report"]
    n_points = sum(len(d["points"]) for d in days)

    print(f"✓ профіль «{profile_id}» імпортовано з карти {map_id} ({trip_dir})")
    print(f"  назва        {trip['title'] or '—'}")
    print(f"  дати         {trip['start'] or '?'} … {trip['end'] or '?'}")
    print(f"  країни       {', '.join(trip['countries']) or '— (не визначились з country_info)'}")
    print(f"  днів         {len(days)}")
    print(f"  точок        {n_points}")
    print(f"  альтернатив  {len(alternates)}")
    print(f"  нові картки  {len(result['new_cards'])}")
    print(f"  перегенеровано (машинний імпорт) {len(result['regenerated'])}")

    cats = result["poi_categories"]
    if cats:
        pretty = " · ".join(f"{v['icon']}{k}→«{v['section']}»" for k, v in cats.items())
        print(f"  poi_categories  виведено з карти: {pretty}")
    else:
        print("  poi_categories  на карті не знайдено жодної категорії, крім базової poi")
    if result["poi_category_notes"]:
        print("\n-- poi_categories: примітки --")
        print("\n".join(result["poi_category_notes"]))

    if result["dateless_points"]:
        print(f"\n⚠️  {len(result['dateless_points'])} точок маршруту без дати — пропущено:")
        for mp in result["dateless_points"]:
            print(f"    · {mp.get('label') or mp.get('id')}")

    if report.ambiguous_matches:
        print("\n-- Неоднозначні збіги координат --")
        print("\n".join(report.ambiguous_matches))

    if report.divergences:
        print("\n-- Розбіжності картка ↔ карта (на розгляд людини, нічого не виправлено) --")
        print("\n".join(report.divergences))

    if report.regenerated_cards:
        print("\n-- Перегенеровано (позначені «Імпортовано з карти», не вичитані людиною) --")
        for slug, title, source in report.regenerated_cards:
            print(f"  {slug}  «{title}»  ← {source}")

    print(
        "\n⚠️  Імпорт лосі за визначенням: popup_html — згенерований HTML, розібрати "
        "його назад у ту саму прозу неможливо. Усі нові й перегенеровані картки — рівня "
        "«базовий» з позначкою «проза не розколота»; наступний крок — довести їх через /place."
    )
    print_fillin_advice(trip)


VALUE_FLAGS = ("--trip", "--data", "--title", "--start", "--end", "--countries",
               "--map", "--as")


def positional(argv):
    """Позиційні аргументи без прапорців — і без ЗНАЧЕНЬ прапорців.

    Інакше `trip.py --trip <id>` читає <id> як команду, а `trip.py new x
    --title Foo` читає `Foo` як другий позиційний аргумент.
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
    raw = argv[1:]
    args = positional(raw)
    cmd = args[0] if args else "show"

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
        if cmd == "new":
            if len(args) < 2:
                print("Треба id профілю: trip.py new <id> [--title …] [--start YYYY-MM-DD] "
                      "[--end …] [--countries UA,RO] [--map <id>]", file=sys.stderr)
                return 2
            return cmd_new(raw, args[1])
        if cmd == "import":
            if len(args) < 2:
                print("Треба map_id: trip.py import <map_id> [--as <profile-id>]", file=sys.stderr)
                return 2
            return cmd_import(raw, args[1], flag_value(raw, "--as"))
    except TripContextError as e:
        print(f"⚠️  {e}", file=sys.stderr)
        return 1

    print(f"Невідома команда «{cmd}». Є: show, list, use, new, import",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
