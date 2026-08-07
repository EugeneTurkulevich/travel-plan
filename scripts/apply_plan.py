"""
scripts/apply_plan.py

Виконавець push_plan.json (складеного plan_push.py) — застосовує кроки плану
на карту ПО ЧЕРЗІ, точними тулами воркера, ланцюжком base_rev. Не планує
нічого сам (це робить plan_push.py) і не вигадує тули — лише виконує те, що
вже записано в plan["steps"].

⚠️ НАЙВАЖЛИВІШЕ ПРАВИЛО. Спільну карту зараз редагує інший агент. Цей скрипт
фізично не може писати в неї без явного дозволу:

  - дефолт — dry-run: друкує виклики, жодного мережевого виклику не робить
    (навіть авторизації не питає);
  - реальне виконання проти карти профілю вимагає ОБОХ прапорців разом:
    --map <id> (має збігтися з map_id активного профілю) і
    --yes-i-mean-the-shared-map. Без обох, чи з чужим id — відмова;
  - --rehearse створює ВЛАСНУ приватну чернетку (create_map, draft: true) і
    виконує план на ній. Чернетка не спільна, приєднання закрите — спільної
    карти цей режим не торкається. promote_draft тут НЕ викликається НІКОЛИ
    (і взагалі відсутній у цьому файлі).

Перевикористовує call_tool/get_access_token/get_worker/load_env_file з
cloud_push.py — не дублює авторизацію й обробку помилок JSON-RPC.

Перед першим викликом (лише для --rehearse / реального виконання, у dry-run
нічого з цього не відбувається):
  1. Гейти плану: gates.booked_ok і gates.limits_ok мають бути true.
  2. Свіжість (тільки для реального виконання проти карти профілю): rev
     карти має збігатися з base_rev плану — інакше карту хтось змінив після
     планування, і план застарів.
  3. План не порожній.

Виконання — по кроках, у порядку плану. Після кожного успішного кроку новий
rev стає base_rev наступного. rebased: true — не помилка (сервер сам
переніс операцію на новішу ревізію). booking_warning виводиться дослівно.
Помилка кроку — стоп, решта плану НЕ виконується.

Журнал кожного запуску (реального чи репетиції) — у
<дані>/trips/<профіль>/apply_log.json (репетиція пишеться в
apply_log.rehearse.json, щоб не затерти журнал реального виконання).

Usage:
    python3 scripts/apply_plan.py                      # dry-run (дефолт)
    python3 scripts/apply_plan.py --dry-run             # те саме, явно
    python3 scripts/apply_plan.py --rehearse            # на власній чернетці
    python3 scripts/apply_plan.py --map <id> --yes-i-mean-the-shared-map
    python3 scripts/apply_plan.py --plan <path>          # інший push_plan.json
"""
import argparse
import datetime
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cloud_push import (  # noqa: E402
    CTX, call_tool, get_access_token, get_worker, load_env_file,
)

# Мітки лежать десь на маршруті (наприклад «Трансфагараш — треба» біля
# гірського перевалу за 50 км від найближчої точки маршруту, якої стосується
# зміна) — це не картка одного місця, а порада по ділянці. Тому радіус
# route-масштабу, не «сусідній будинок».
MARK_PROXIMITY_KM = 30.0


# ── план: завантаження й гейти ──────────────────────────────────────────────

def load_plan(path):
    if not path.exists():
        sys.exit(
            f"Немає плану: {path}\n"
            "Спершу склади його: python3 scripts/plan_push.py"
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"{path}: не вдалось розпарсити JSON: {e}")


def check_gates(plan):
    """Гейти — це стоп, не попередження: сервер однаково відхилить, а
    частково застосований план гірший за незастосований."""
    gates = plan.get("gates") or {}
    problems = []
    if gates.get("booked_ok") is False:
        problems.append(
            "gates.booked_ok = false — план виносить заброньовану точку з "
            "маршруту; сервер відхилить операцію цілком (замок на "
            "заброньованих точках), а частково застосований план — гірше за "
            "незастосований."
        )
    if gates.get("limits_ok") is False:
        problems.append(
            "gates.limits_ok = false — план перевищує ліміти воркера "
            "(payload/POI/popup_html/photo_url тощо); скороти дані й "
            "перегенеруй план (plan_push.py)."
        )
    if problems:
        print("✗ ПЛАН НЕПРИДАТНИЙ до виконання:", file=sys.stderr)
        for p in problems:
            print(f"  · {p}", file=sys.stderr)
        for n in gates.get("notes") or []:
            print(f"  · нотатка плану: {n}", file=sys.stderr)
        sys.exit(1)


# ── dry-run: лише друк, жодного call_tool ───────────────────────────────────

def print_dry_run(plan, plan_path):
    steps = plan.get("steps") or []
    print(f"[DRY RUN] план: {plan_path}")
    print(
        f"[DRY RUN] map_id={plan.get('map_id')} base_rev={plan.get('base_rev')} "
        f"кроків={len(steps)}"
    )
    print("[DRY RUN] мережевих викликів не буде — нижче показано, що було б "
          "надіслано")
    gates = plan.get("gates") or {}
    if gates.get("booked_ok") is False or gates.get("limits_ok") is False:
        failed = [k for k in ("booked_ok", "limits_ok") if gates.get(k) is False]
        print(f"[DRY RUN] ⚠ гейти НЕ пройдені ({', '.join(failed)}) — реальне "
              "виконання (--rehearse чи --map+--yes-i-mean-the-shared-map) "
              "буде відмовлено ще ДО першого виклику (див. нотатки плану нижче)")
        for n in gates.get("notes") or []:
            print(f"    · {n}")
    print()
    for i, step in enumerate(steps, 1):
        call_args = dict(step.get("args") or {})
        call_args["map_id"] = plan.get("map_id")
        call_args["base_rev"] = (
            plan.get("base_rev") if i == 1 else f"<rev після кроку {i - 1}>"
        )
        affects = ", ".join(a for a in (step.get("affects") or []) if a)
        print(f" {i}. {step['op']}({json.dumps(call_args, ensure_ascii=False)})"
              + (f"  [{affects}]" if affects else ""))
        print(f"    {step.get('why', '')}")
    print(f"\n[DRY RUN] нічого не відправлено ({len(steps)} кроків показано вище)")


# ── маршрут виконання: dry-run / rehearse / real ────────────────────────────

def resolve_mode(args):
    if args.dry_run and (args.rehearse or args.map or args.confirm_shared):
        sys.exit(
            "--dry-run взаємовиключний з --rehearse/--map/"
            "--yes-i-mean-the-shared-map — обери один режим."
        )
    if args.rehearse and (args.map or args.confirm_shared):
        sys.exit(
            "--rehearse взаємовиключний з --map/--yes-i-mean-the-shared-map: "
            "репетиція завжди йде на ВЛАСНІЙ новоствореній чернетці, а не на "
            "карту, яку ти вказав."
        )
    if args.rehearse:
        return "rehearse"
    if args.map or args.confirm_shared:
        if not (args.map and args.confirm_shared):
            sys.exit(
                "Реальне виконання проти спільної карти вимагає ОБОХ прапорців "
                "разом: --map <id> і --yes-i-mean-the-shared-map. "
                "Одного недостатньо — відмова."
            )
        if args.map != CTX.map_id:
            sys.exit(
                f"--map {args.map!r} НЕ збігається з map_id активного профілю "
                f"({CTX.map_id!r}) — відмова. Спільну карту з чужим id "
                "не займаю; якщо це справді потрібна карта, перевір активний "
                "профіль (trip.py) чи --trip."
            )
        return "real"
    return "dry-run"


# ── допоміжне: відстань (для нагадування про мітки) ─────────────────────────

def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def touched_point_ids(steps):
    ids = set()
    for step in steps:
        a = step.get("args") or {}
        if a.get("point_id"):
            ids.add(a["point_id"])
        for pid in a.get("point_ids") or []:
            ids.add(pid)
        if a.get("alt_id"):
            ids.add(a["alt_id"])
        for aid in a.get("alt_ids") or []:
            ids.add(aid)
    return ids


def marks_near_touched_points(plan):
    """Нагадування про мітки, повʼязані з планом. Мітки НЕ мають прямого
    посилання на point_id (методичка цього не передбачає), тож точного
    зіставлення не існує — беремо два наближення, обидва консервативні
    (краще зайва мітка в списку, ніж пропущена):

      - "proximity": мітка лежить за ≤MARK_PROXIMITY_KM (route-масштаб, не
        сусідній будинок — мітка типу «Трансфагараш — треба» може стояти
        за 30-50 км від найближчої точки маршруту, якої стосується порада);
      - "open": УСІ ще не закриті мітки карти — незалежно від відстані,
        бо не закрита мітка так чи інакше потребує рішення людини, а
        пропустити її через невдалий збіг координат гірше, ніж показати
        зайву.

    Джерело — live_mirror.json (локальний файл, уже витягнутий pull_map.py;
    без мережевого виклику)."""
    mirror_path = CTX.live_mirror
    if not mirror_path.exists():
        return []
    try:
        mirror = json.loads(mirror_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    route_points = {p.get("id"): p for p in (mirror.get("route") or {}).get("points", []) if p.get("id")}
    alt_points = {p.get("id"): p for p in (mirror.get("alt") or {}).get("points", []) if p.get("id")}
    marks = mirror.get("marks") or []

    coords = []
    for pid in touched_point_ids(plan.get("steps") or []):
        p = route_points.get(pid) or alt_points.get(pid)
        if p and p.get("lat") is not None and p.get("lon") is not None:
            label = p.get("label") or p.get("name") or pid
            coords.append((label, p["lat"], p["lon"]))

    by_id = {}
    for m in marks:
        mlat, mlon = m.get("lat"), m.get("lon")
        label = None
        if mlat is not None and mlon is not None:
            for cand_label, lat, lon in coords:
                if haversine_km(mlat, mlon, lat, lon) <= MARK_PROXIMITY_KM:
                    label = cand_label
                    break
        if label is not None:
            by_id[m.get("id")] = (m, label, "proximity")
        elif m.get("status") == "open" and m.get("id") not in by_id:
            by_id[m.get("id")] = (m, None, "open")
    return list(by_id.values())


# ── виконання кроків ─────────────────────────────────────────────────────────

def compact_result(result):
    r = dict(result or {})
    pts = r.get("points")
    if isinstance(pts, list):
        r["points"] = f"<omitted, {len(pts)} points>"
    return r


def run_steps(worker, token, target_map_id, initial_rev, plan, log_path, mode):
    steps = plan.get("steps") or []
    log = {
        "started_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": mode,
        "map_id": target_map_id,
        "plan_path": plan.get("route_state_path"),
        "plan_generated_at": plan.get("generated_at"),
        "plan_base_rev": plan.get("base_rev"),
        "initial_rev": initial_rev,
        "steps": [],
    }

    base_rev = initial_rev
    booking_warnings = []
    rebased_steps = []
    applied = 0

    for i, step in enumerate(steps, 1):
        call_args = dict(step.get("args") or {})
        call_args["map_id"] = target_map_id
        call_args["base_rev"] = base_rev

        print(f" {i}/{len(steps)}. {step['op']}"
              + (f"  [{', '.join(a for a in (step.get('affects') or []) if a)}]"
                 if step.get("affects") else "")
              + f"  (base_rev={base_rev})")

        try:
            result = call_tool(worker, token, step["op"], call_args)
        except SystemExit as e:
            code = e.code
            if isinstance(code, str):
                error_msg, exit_code = code, 1
            elif code == 2:
                error_msg, exit_code = "Конфлікт ревізій (див. повідомлення вище)", 2
            else:
                error_msg = f"exit {code}"
                exit_code = code if isinstance(code, int) else 1

            log["steps"].append({
                "index": i, "op": step["op"], "affects": step.get("affects"),
                "args": call_args, "rev_before": base_rev, "status": "failed",
                "rev_after": None, "rebased": None, "rebased_from": None,
                "booking_warning": None, "error": error_msg,
            })
            log["finished_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
            log["summary"] = {
                "total_steps": len(steps), "applied": applied, "failed_at": i,
                "booking_warnings_total": len(booking_warnings),
                "rebased_steps": rebased_steps,
            }
            CTX.write_json(log_path, log)

            print(f"\n✗ Зупинено на кроці {i}/{len(steps)} ({step['op']}): {error_msg}")
            print(f"✓ Застосовано до зупинки: {applied} крок(и/ів) із {len(steps)}.")
            if applied:
                for done in log["steps"][:-1]:
                    print(f"   ✓ {done['index']}. {done['op']}  rev {done['rev_before']}→{done['rev_after']}")
            print(f"   ✗ {i}. {step['op']}  — впав тут")
            print(f"\n→ журнал: {log_path}")
            print("Далі: перечитай get_route (побач актуальний rev), за потреби "
                  "diff_route_revisions, узгодь з людиною — повторити операцію "
                  "на новій базі чи скасувати. Частково застосований план — "
                  "нормальний стан, головне, що межа відома.")
            sys.exit(exit_code)

        new_rev = result.get("rev")
        rebased = bool(result.get("rebased"))
        warnings = result.get("booking_warning") or []
        if warnings:
            booking_warnings.extend(warnings)
        if rebased:
            rebased_steps.append(i)
            print(f"    ↻ rebased: true (rebased_from={result.get('rebased_from')}) — "
                  "сервер сам переніс операцію на новішу ревізію, це не помилка")
        for w in warnings:
            print(f"    ⚠ booking_warning: {w}")
        if result.get("already_removed"):
            print("    (already_removed: true — точку вже прибрав хтось інший, ідемпотентно)")

        log["steps"].append({
            "index": i, "op": step["op"], "affects": step.get("affects"),
            "args": call_args, "rev_before": base_rev, "status": "ok",
            "rev_after": new_rev, "rebased": rebased,
            "rebased_from": result.get("rebased_from"),
            "booking_warning": warnings or None,
            "already_removed": result.get("already_removed"),
            "result": compact_result(result),
            "error": None,
        })
        applied += 1

        if new_rev is None:
            print(f"    ⚠ відповідь без rev — не можу продовжити ланцюжок base_rev: {result}")
            log["finished_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
            log["summary"] = {
                "total_steps": len(steps), "applied": applied, "failed_at": i,
                "booking_warnings_total": len(booking_warnings),
                "rebased_steps": rebased_steps,
            }
            CTX.write_json(log_path, log)
            print(f"→ журнал: {log_path}")
            sys.exit(1)
        base_rev = new_rev
        print(f"    ✓ rev → {new_rev}")

    log["finished_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    log["summary"] = {
        "total_steps": len(steps), "applied": applied, "failed_at": None,
        "booking_warnings_total": len(booking_warnings), "rebased_steps": rebased_steps,
        "final_rev": base_rev,
    }
    CTX.write_json(log_path, log)
    return log, base_rev


def print_final_report(plan, log, final_rev, mode, log_path):
    s = log["summary"]
    print(f"\n✓ Застосовано всі {s['total_steps']} крок(и/ів). Фінальний rev: {final_rev}.")
    if s["rebased_steps"]:
        print(f"↻ rebased сервером на {len(s['rebased_steps'])} крок(ах): "
              f"{s['rebased_steps']} — нормально, не помилка.")
    if s["booking_warnings_total"]:
        print(f"⚠ booking_warning усього: {s['booking_warnings_total']} (див. дослівно вище).")
    print(f"→ журнал: {log_path}")

    hits = marks_near_touched_points(plan)
    if hits:
        print("\n⚠ Мітки, які стосуються цього плану (за близькістю координат "
              f"≤{MARK_PROXIMITY_KM:.0f} км ДО ЗМІНЕНИХ точок, плюс УСІ ще не "
              "закриті мітки карти) — перевір і закрий resolve_mark(status, "
              "comment) вручну (рішення про зміст, скрипт його не приймає):")
        for m, label, reason in hits:
            where = f"— біля «{label}»" if reason == "proximity" else "— відкрита, звʼязку з планом не встановлено"
            print(f"   · мітка {m.get('id')} [{m.get('status')}] «{m.get('title') or ''}» {where}")
    else:
        print("\n(Немає ні відкритих міток, ні міток поблизу змінених точок за "
              "координатами дзеркала — усе одно варто перевірити list_marks очима, "
              "це наближення, не гарантія.)")

    if mode == "rehearse":
        print(f"\n[REHEARSE] чернетка {log['map_id']} лишилась на сервері — "
              "приберіть її вручну (delete_map), якщо вона більше не потрібна. "
              "Цей скрипт promote_draft не викликає ніколи.")


# ── CLI ───────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--plan", help="шлях до push_plan.json (дефолт: <дані>/trips/<профіль>/push_plan.json)")
    p.add_argument("--dry-run", action="store_true", help="явно (це й так дефолт без --rehearse чи --map+--yes-i-mean-the-shared-map)")
    p.add_argument("--rehearse", action="store_true", help="прогнати план на ВЛАСНІЙ новій чернетці (create_map draft:true); спільної карти не торкається")
    p.add_argument("--map", metavar="MAP_ID", help="ID карти для РЕАЛЬНОГО виконання — має збігтися з map_id активного профілю")
    p.add_argument("--yes-i-mean-the-shared-map", dest="confirm_shared", action="store_true",
                    help="явне підтвердження, що ти свідомо пишеш у СПІЛЬНУ карту (обовʼязково разом з --map)")
    return p.parse_args()


def main():
    args = parse_args()

    plan_path = Path(args.plan).expanduser() if args.plan else CTX.trip_dir / "push_plan.json"
    plan = load_plan(plan_path)

    if not plan.get("steps"):
        print(f"План {plan_path} ПОРОЖНІЙ — намір і карта тотожні, виконувати нічого.")
        return 0

    mode = resolve_mode(args)

    if mode == "dry-run":
        # Dry-run нічого не виконує (жодного call_tool), тож гейти тут не
        # блокують показ — навпаки, це основний спосіб побачити, ЩО саме
        # завадить реальному виконанню (check_gates нижче спрацює для
        # --rehearse і реального запуску, перед першим call_tool).
        print_dry_run(plan, plan_path)
        return 0

    # ── перевірки ПЕРЕД першим мережевим викликом (rehearse / real) ────────
    check_gates(plan)

    env_vars = load_env_file()
    worker = get_worker(env_vars)
    token = get_access_token(env_vars, worker)

    if mode == "rehearse":
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        name = f"rehearsal {CTX.trip_id} {ts}"
        created = call_tool(worker, token, "create_map", {"name": name, "draft": True})
        target_map_id = created.get("map_id")
        if not target_map_id:
            sys.exit(f"create_map не повернув map_id: {created}")
        print(f"✓ create_map (чернетка для репетиції): {target_map_id} «{name}» — {created.get('url', '')}")
        print("  (приватна, приєднання закрите — спільної карти не торкається; "
              "promote_draft НЕ буде викликано)")
        log_path = CTX.trip_dir / "apply_log.rehearse.json"
    else:
        target_map_id = args.map
        log_path = CTX.trip_dir / "apply_log.json"

    route = call_tool(worker, token, "get_route", {"map_id": target_map_id})
    current_rev = route.get("rev")

    if mode == "real":
        if current_rev != plan.get("base_rev"):
            sys.exit(
                f"Карту змінили після планування: rev карти зараз {current_rev}, "
                f"план складено на базі rev {plan.get('base_rev')}. "
                "Не підлаштовуюсь — перескладіть план: python3 scripts/plan_push.py"
            )
        print(f"✓ Свіжість підтверджено: rev карти {current_rev} = base_rev плану.")
    else:
        print(f"[REHEARSE] rev нової чернетки: {current_rev} "
              f"(base_rev плану {plan.get('base_rev')} стосується ІНШОЇ, спільної карти — "
              "не звіряю, це різні карти).")

    print(f"\nВиконую {len(plan['steps'])} крок(и/ів) на map={target_map_id}...\n")
    log, final_rev = run_steps(worker, token, target_map_id, current_rev, plan, log_path, mode)
    print_final_report(plan, log, final_rev, mode, log_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
