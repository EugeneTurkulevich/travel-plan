"""
scripts/check_threads.py

Заміна `check_marks.py`, яка бачить не лише мітки, а й треди на МІСЦЯХ.

НАВІЩО. `check_marks.py` читає лише мітки (`list_marks`). Але треди тепер
живуть і на місцях — плоско за id (`live.threads.{place_id}`), однаково для
точок маршруту й альтернатив (методичка воркера). 04.08.2026 виміряно: 20
тредів, усі на альтернативах — тобто чинна перевірка перед пушем не бачила
жодного з них. `check_marks.py` навмисно НЕ видалений — його прибере окремий
коміт, коли цей скрипт доведе себе.

Ловить три стани окремо:
  ⏳ мітка status="open"                       — побажання ще в роботі. Це
                                                    завжди exit 1, незалежно
                                                    від дати: невиконане
                                                    побажання не «переглядається»
                                                    — воно або враховане, або
                                                    свідомо відхилене через
                                                    resolve_mark;
  ⚠️ мітка закрита, але після resolved_ts       — статус застарів. «Застарів»
     зʼявились нові коментарі                     тут МАЄ дату: якщо остання
                                                    репліка старіша за мітку
                                                    перегляду — уже бачили;
  💬 тред на МІСЦІ (точка маршруту чи           — так само за датою: тред зі
     альтернатива) з репліками                    старою останньою реплікою
                                                    вважається переглянутим і
                                                    не тримає exit 1; окремо
                                                    позначені ті, де остання
                                                    репліка не наша.

Run:
    python3 scripts/check_threads.py                # з дзеркала (офлайн)
    python3 scripts/check_threads.py --fresh        # спершу python3 scripts/pull_map.py
    python3 scripts/check_threads.py --mark-reviewed  # поставити мітку перегляду на «зараз»

⚠️ «Не наша репліка» визначається звірянням автора з ідентичністю власника
токена. Дзеркало (get_map/get_route/…) її не містить — жодного read-тула
методички не віддає «хто я». Джерело — trip.json активного профілю
(owner_email), запасний варіант — .env кореня даних (OWNER_EMAIL). Немає
жодного — позначку «не наша» скрипт НЕ вгадує: показує треди без неї і
попереджає, що ідентичність не налаштована.

⚠️ ГЕЙТ, ЯКИЙ СВІТИТЬСЯ ЗАВЖДИ, ПЕРЕСТАЮТЬ ЧИТАТИ. Усі 20 тредів на момент
написання — власні нотатки-походження автора точок, а не чужі питання; без
мітки часу перевірка була б exit 1 назавжди, і за тиждень її перестали б
дивитись. `trip.json.threads_reviewed_until` — час, до якого людина вже
все переглянула. Тред чи «застаріла» мітка зі СТАРІШОЮ за неї останньою
реплікою — уже бачили, exit 1 не тримає, лише рахується в зведенні. Немає
цього поля в trip.json — рахуємо, що не переглядали НІКОЛИ (усе «нове»):
безпечний дефолт, а не мовчазне «усе гаразд».

Код виходу 1 — є що подивитись перед пушем: відкрита мітка (завжди), або
будь-яка «нова» (за міткою перегляду) застаріла мітка чи тред на місці.
"""
import argparse
import datetime
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trip_ctx import paths  # noqa: E402

CTX = paths()

_EPOCH_AWARE = datetime.datetime.fromtimestamp(0, tz=datetime.timezone.utc)


# ── дрібні помічники (як у check_marks.py) ───────────────────────────────────

def ts(v) -> str:
    try:
        return datetime.datetime.fromtimestamp(int(v) / 1000).strftime("%d.%m %H:%M")
    except (TypeError, ValueError):
        return "?"


def ts_to_dt(v):
    """epoch-мілісекунди → aware datetime (UTC). None/сміття → епоха (найстаріше)."""
    try:
        return datetime.datetime.fromtimestamp(int(v) / 1000, tz=datetime.timezone.utc)
    except (TypeError, ValueError):
        return _EPOCH_AWARE


def comments_of(obj) -> list:
    """obj.comments може бути списком або (рідше) dict'ом за id — приводимо
    до відсортованого за часом списку в обох випадках."""
    c = obj.get("comments") or []
    if isinstance(c, dict):
        c = list(c.values())
    return sorted(c, key=lambda x: x.get("ts", 0))


def resolve_owner_email():
    """Ідентичність власника токена — для позначки «не наша репліка».
    Немає надійного офлайн-джерела (дзеркало його не містить), тому шукаємо
    ЯВНО заданий ключ: спершу trip.json (owner_email), потім .env кореня
    даних (OWNER_EMAIL). Обидва — приватні дані, у публічний тулкіт не
    потрапляють."""
    owner = CTX.trip.get("owner_email")
    if owner:
        return owner
    env = CTX.env()
    return env.get("OWNER_EMAIL") or None


def resolve_reviewed_until():
    """trip.json.threads_reviewed_until → aware datetime. Немає поля —
    епоха (усе «нове»): безпечний дефолт, а не мовчазне «усе переглянуто»."""
    raw = CTX.trip.get("threads_reviewed_until")
    if not raw:
        return _EPOCH_AWARE, False
    try:
        dt = datetime.datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt, True
    except ValueError:
        return _EPOCH_AWARE, False


# ── збір тредів на місцях (route.threads + alt.points[].comments) ───────────

def collect_place_threads(mirror):
    """[{place_id, label, origin, comments}] — де origin: 'маршрут'|'альтернатива'.
    Лише непорожні треди (правило дзеркала: route.threads несе тільки непорожні
    списки, alt.points фільтруємо самі)."""
    threads = []

    label_by_id = {p.get("id"): (p.get("label") or p.get("id")) for p in mirror["route"]["points"]}
    for place_id, cs in (mirror["route"].get("threads") or {}).items():
        if not cs:
            continue
        threads.append({
            "place_id": place_id,
            "label": label_by_id.get(place_id, place_id),
            "origin": "маршрут",
            "comments": sorted(cs, key=lambda x: x.get("ts", 0)) if isinstance(cs, list)
            else sorted(cs.values(), key=lambda x: x.get("ts", 0)),
        })

    for p in mirror["alt"]["points"]:
        cs = comments_of(p)
        if not cs:
            continue
        threads.append({
            "place_id": p.get("id"),
            "label": p.get("name") or p.get("id"),
            "origin": "альтернатива",
            "comments": cs,
        })

    return threads


# ── звіт: мітки ───────────────────────────────────────────────────────────────

def report_marks(marks, reviewed_until):
    """Друкує й повертає (open_ids, stale_new, stale_reviewed_count).

    open_ids       — status="open", завжди exit 1, незалежно від дати.
    stale_new      — закрито, є коментарі після resolved_ts, і
                      найновіший з них НОВІШИЙ за reviewed_until → exit 1.
    stale_reviewed — те саме, але найновіший коментар СТАРІШИЙ за
                      reviewed_until — уже бачили, лише число в зведенні."""
    open_ids, stale_new = [], []
    stale_reviewed_count = 0

    print(f"міток на карті: {len(marks)}\n")
    for m in marks:
        cs = comments_of(m)
        first = cs[0].get("text", "") if cs else ""
        status = m.get("status", "open")
        head = f"[{status}] {m.get('title') or first[:44] or '(без назви)'}  ({m['id']})"

        if status == "open":
            open_ids.append(m["id"])
            print(f"⏳ {head}")
            if cs:
                last = cs[-1]
                print(f"      ↳ {ts(last.get('ts'))}  {str(last.get('author'))[:26]:<26} "
                      f"{last.get('text', '')[:80]}")
            continue

        rts = m.get("resolved_ts") or 0
        later = [c for c in cs if c.get("ts", 0) > rts]
        if not later:
            print(f"✓  {head}   (rev {m.get('resolved_rev')})")
            continue

        last_later = later[-1]
        if ts_to_dt(last_later.get("ts")) > reviewed_until:
            stale_new.append(m["id"])
            print(f"⚠️  {head}   (закрито {ts(rts)}, rev {m.get('resolved_rev')}) — НОВЕ")
            for c in later:
                print(f"      ↳ {ts(c.get('ts'))}  {str(c.get('author'))[:26]:<26} "
                      f"{c.get('text', '')[:80]}")
        else:
            stale_reviewed_count += 1
            print(f"⚠️  {head}   (закрито {ts(rts)}, rev {m.get('resolved_rev')}) "
                  f"— переглянуто (остання репліка {ts(last_later.get('ts'))})")

    return open_ids, stale_new, stale_reviewed_count


# ── звіт: треди на місцях ─────────────────────────────────────────────────────

def report_threads(threads, owner_email, reviewed_until):
    """Друкує й повертає (new_ids, reviewed_count).

    Нові (остання репліка новіша за reviewed_until) друкуються повністю й
    тримають exit 1. Переглянуті (старіша) — лише число в зведенні плюс
    компактний рядок тут, щоб не ховати склад від людини, яка захоче
    перевірити очима."""
    new_ids = []
    reviewed_lines = []
    n_route = sum(1 for t in threads if t["origin"] == "маршрут")
    n_alt = sum(1 for t in threads if t["origin"] == "альтернатива")
    print(f"тредів на місцях: {len(threads)}  ({n_alt} на альтернативах, {n_route} на точках маршруту)\n")

    if owner_email is None:
        print("⚠️  Ідентичність власника токена не налаштована (trip.json.owner_email "
              "або .env: OWNER_EMAIL) — позначку «не наша репліка» пропущено, "
              "перевіряй авторів очима.\n")

    for t in sorted(threads, key=lambda x: x["label"]):
        last = t["comments"][-1]
        is_new = ts_to_dt(last.get("ts")) > reviewed_until
        not_ours = owner_email is not None and last.get("author") != owner_email

        if is_new:
            new_ids.append(t["place_id"])
            flag = "  ⚠️ не наша — ще не відповіли" if not_ours else ""
            print(f"💬 [{t['origin']}] {t['label']}  ({t['place_id']}, {len(t['comments'])} реплік){flag}")
            print(f"      ↳ {ts(last.get('ts'))}  {str(last.get('author'))[:26]:<26} "
                  f"{last.get('text', '')[:90]}")
        else:
            reviewed_lines.append(
                f"  ✓ [{t['origin']}] {t['label']}  ({t['place_id']}) — "
                f"переглянуто, остання репліка {ts(last.get('ts'))}"
            )

    if reviewed_lines:
        print(f"-- переглянуті ({len(reviewed_lines)}, не впливають на код виходу) --")
        for line in reviewed_lines:
            print(line)
    else:
        print("-- переглянуті: (нема) --")

    return new_ids, len(reviewed_lines)


_REVIEWED_UNTIL_RE = re.compile(r'("threads_reviewed_until"\s*:\s*)"[^"]*"')


def write_reviewed_until(now_iso):
    """Точкова правка одного значення в trip.json замість перезапису через
    CTX.write_json: trip.json — файл, який людина читає й редагує руками
    (компактні однорядкові масиви/обʼєкти), а не згенерований артефакт.
    json.dumps(indent=1) розгорнув би КОЖЕН масив/обʼєкт у файлі на окремі
    рядки — diff тоді показував би «змінено все», хоча змінилось одне
    значення. Ключа ще нема (профіль без threads_reviewed_until) — тоді
    таки дописуємо через CTX.write_json, це одноразова ціна додавання поля."""
    raw = CTX.trip_json.read_text(encoding="utf-8")
    if _REVIEWED_UNTIL_RE.search(raw):
        new_raw = _REVIEWED_UNTIL_RE.sub(
            lambda m: m.group(1) + json.dumps(now_iso, ensure_ascii=False), raw, count=1
        )
        CTX.trip_json.write_text(new_raw, encoding="utf-8")
    else:
        trip = dict(CTX.trip)
        trip["threads_reviewed_until"] = now_iso
        CTX.write_json(CTX.trip_json, trip)


# ── --mark-reviewed ────────────────────────────────────────────────────────────

def cmd_mark_reviewed(mirror, reviewed_until, had_field):
    """Ставить threads_reviewed_until у trip.json на «зараз». Друкує, що
    саме тепер вважається переглянутим. Вимагає підтвердження, якщо є
    відкриті мітки — щоб не можна було одним рухом «погасити» непророблене
    побажання (відкриті мітки після цього і далі тримають exit 1, але
    людина має свідомо побачити, що вони лишаються)."""
    marks = mirror.get("marks") or []
    open_marks = [m for m in marks if (m.get("status") or "open") == "open"]

    if open_marks:
        print(f"⚠️  Відкритих міток: {len(open_marks)} — вони НЕ зникнуть з exit-коду, "
              f"позначка перегляду на них не діє:")
        for m in open_marks:
            cs = comments_of(m)
            first = cs[0].get("text", "") if cs else ""
            print(f"   ⏳ {m.get('title') or first[:60] or '(без назви)'}  ({m['id']})")
        answer = input("\nПродовжити й посунути мітку перегляду тредів? (yes/no): ").strip().lower()
        if answer not in ("yes", "y", "так", "т"):
            print("Скасовано — trip.json не змінено.")
            return 1

    threads = collect_place_threads(mirror)
    newly_reviewed_threads = [t for t in threads if ts_to_dt(t["comments"][-1].get("ts")) > reviewed_until]

    stale_marks_newly_reviewed = []
    for m in marks:
        if (m.get("status") or "open") == "open":
            continue
        cs = comments_of(m)
        rts = m.get("resolved_ts") or 0
        later = [c for c in cs if c.get("ts", 0) > rts]
        if later and ts_to_dt(later[-1].get("ts")) > reviewed_until:
            stale_marks_newly_reviewed.append(m)

    if not newly_reviewed_threads and not stale_marks_newly_reviewed:
        print("Нічого нового понад попередню мітку перегляду немає — все одно посуваю мітку.")
    else:
        print(f"Тепер вважаються переглянутими: {len(newly_reviewed_threads)} тредів на місцях, "
              f"{len(stale_marks_newly_reviewed)} застарілих міток:")
        for t in sorted(newly_reviewed_threads, key=lambda x: x["label"]):
            print(f"   💬 [{t['origin']}] {t['label']}  ({t['place_id']})")
        for m in stale_marks_newly_reviewed:
            cs = comments_of(m)
            first = cs[0].get("text", "") if cs else ""
            print(f"   ⚠️ {m.get('title') or first[:60] or '(без назви)'}  ({m['id']})")

    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    write_reviewed_until(now)
    print(f"\n→ trip.json: threads_reviewed_until = {now}")
    return 0


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fresh", action="store_true", help="спершу перечитати карту (python3 scripts/pull_map.py)")
    ap.add_argument("--mark-reviewed", action="store_true",
                     help="поставити мітку перегляду тредів на «зараз» (trip.json.threads_reviewed_until)")
    args, _unknown = ap.parse_known_args()

    if args.fresh:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import pull_map  # noqa: E402
        from cloud_push import get_access_token, get_worker, load_env_file  # noqa: E402

        map_id = CTX.map_id
        if not map_id:
            sys.exit("Немає map_id: активний профіль без map_id.")
        env_vars = load_env_file()
        worker = get_worker(env_vars)
        token = get_access_token(env_vars, worker)
        m, route, alt, marks_raw, places, list_alts_result, alt_briefs = pull_map.pull(worker, token, map_id)
        mirror = pull_map.build_mirror(map_id, m, route, alt, marks_raw, places, list_alts_result, alt_briefs)
        CTX.write_json(CTX.live_mirror, mirror)
        print(f"↻ дзеркало оновлено ({CTX.live_mirror})\n")
    else:
        mirror = CTX.read_json(CTX.live_mirror)

    reviewed_until, had_field = resolve_reviewed_until()

    if args.mark_reviewed:
        return cmd_mark_reviewed(mirror, reviewed_until, had_field)

    owner_email = resolve_owner_email()

    if not had_field:
        print("⚠️  trip.json.threads_reviewed_until не задано — вважаю, що НІЧОГО ще не "
              "переглянуто (усе нижче — «нове»). Постав мітку: "
              "python3 scripts/check_threads.py --mark-reviewed\n")
    else:
        print(f"мітка перегляду тредів: {CTX.trip.get('threads_reviewed_until')}\n")

    print("=" * 70)
    print("МІТКИ")
    print("=" * 70)
    open_ids, stale_new, stale_reviewed_count = report_marks(mirror.get("marks") or [], reviewed_until)

    print("\n" + "=" * 70)
    print("ТРЕДИ НА МІСЦЯХ")
    print("=" * 70)
    threads = collect_place_threads(mirror)
    thread_new_ids, thread_reviewed_count = report_threads(threads, owner_email, reviewed_until)

    n_reviewed = stale_reviewed_count + thread_reviewed_count
    n_new = len(stale_new) + len(thread_new_ids)
    n_open = len(open_ids)

    print("\n" + "=" * 70)
    print("ПІДСУМОК")
    print("=" * 70)
    print(f"переглянутих (не впливають на код виходу): {n_reviewed}  "
          f"(тредів: {thread_reviewed_count}, застарілих міток: {stale_reviewed_count})")
    print(f"нових (потребують уваги):                   {n_new}  "
          f"(тредів: {len(thread_new_ids)}, застарілих міток: {len(stale_new)})")
    print(f"відкритих міток (завжди потребують уваги):  {n_open}")

    if n_new == 0 and n_open == 0:
        print("\n✓ усе, що не переглянуто раніше, — враховано і закрито — можна пушити")
        return 0

    print(f"\n❗ потребує уваги ПЕРЕД пушем: {n_new + n_open}")
    for pid in open_ids:
        print(f"   {pid}  ⏳ відкрита мітка — ще в роботі")
    for pid in stale_new:
        print(f"   {pid}  ⚠️ закрита мітка, але після неї нові коментарі (не переглянуто)")
    for pid in thread_new_ids:
        print(f"   {pid}  💬 тред на місці — не переглянуто")
    print("\nПісля пушу — resolve_mark(status=done|skipped, comment=…) на кожну "
          "мітку, якої зміна стосується; на треди місць (і маршруту, і "
          "альтернатив) відповідай через add_place_comment. Коли розглянув усе "
          "нижче мітки перегляду — python3 scripts/check_threads.py --mark-reviewed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
