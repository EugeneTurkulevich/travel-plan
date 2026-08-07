"""
scripts/plan_push.py

Планувальник публікації (STRUCTURE_PROPOSAL.md §2.3, наслідок Н1). НІЧОГО не
пише на карту й ніколи не буде — жодного мережевого виклику, жодного режиму
виконання. Порівнює наш намір (exports/route_state.json) з дзеркалом карти
(live_mirror.json, пише pull_map.py) і складає план з ПРАВИЛЬНИХ тулів
воркера — виконує план інший скрипт (cloud_push.py, Ф5).

НАВІЩО. Досі публікація була одним set_route з усім маршрутом. Це небезпечно
з двох незалежних причин:

  1. Точка, що зникла з нашого списку, зникає з route-шару — і НЕ стає
     альтернативою. Методичка воркера (trip-map://guide, розділ «Переїзд між
     шарами») забороняє ручний переїзд (remove_point+add_alt_point) саме
     тому, що так гине наповнення: разом із точкою з показу зникають
     рейтинги учасників і тред обговорення, бо вони живуть під id точки.
     Наш повний пуш робив рівно це, тільки оптом.
  2. set_route і promote_draft відхиляються ЦІЛКОМ, якщо в payload немає
     заброньованої точки (розділ «Замок на заброньованих точках»), і
     авто-rebase для них НЕ працює (він є лише в точкових тулах).

Тому: diff-based dispatch — дивимось, що саме змінилось, і для кожної зміни
беремо тул, який її не зіпсує (правила — див. build_plan() нижче, і
CLAUDE.md / STRUCTURE_PROPOSAL.md §2.3 для обґрунтування).

ЗІСТАВЛЕННЯ ТОЧОК. Основний ключ — id (route_state.json несе map_point_id з
route.json під ключем "id"). Запасний варіант, якщо id немає або не
знайдений: (label, date), далі округлені координати (round(., 4), як у
cloud_push.carry_ids). Кожен запасний збіг рахується окремо й видно у звіті
("matching") — щоб мовчазне падіння на (label, date) не пройшло непоміченим.

НОРМАЛІЗАЦІЯ ПЕРЕД ПОРІВНЯННЯМ (інакше план потоне в шумі):
  - photo_url і будь-яке необов'язкове текстове поле: відсутнє поле і "" —
    одне й те саме (наш генератор не виводить порожні поля, карта зберігає
    порожній рядок).
  - nights: відсутнє і 1 — одне й те саме значення.

POI (nested_points) НЕ МАЮТЬ стабільного id на сервері (методичка цього не
передбачає) — зіставляємо в межах однієї точки за "name". Це не ідеально
(перейменування POI виглядатиме як видалення+додавання), але дає точний
diff там, де назва не змінилась — і, як показано на живих даних, усуває
фальшиві "розбіжності", які насправді є просто іншим порядком POI у масиві
(сервер не гарантує стабільний порядок).

Usage:
    python3 scripts/plan_push.py              # звіт людині + план у файл
    python3 scripts/plan_push.py --json       # тільки JSON плану у stdout
    python3 scripts/plan_push.py --state <path>   # інший route_state.json
                                                   (тест без зачіпання даних)
    python3 scripts/plan_push.py --mirror <path>  # інше дзеркало (тест)

Пише план у <дані>/trips/<профіль>/push_plan.json незалежно від --json.

⚠️ Немає і не буде прапорця виконання (--apply тощо). Якщо виникне спокуса
його додати — не додавай: виконавець плану це окремий скрипт (cloud_push.py).
"""
import argparse
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trip_ctx import paths  # noqa: E402
import check_limits as CL  # noqa: E402  (перевикористовуємо build_payload/worst/ліміти)

CTX = paths()

# Поля точки верхнього рівня, які порівнюємо напряму (без нормалізації).
POINT_SIMPLE_FIELDS = ("label", "lat", "lon", "kind", "date", "arrive_at", "stay_minutes")
# Поля одного POI (nested_points), які порівнюємо напряму; photo_url — окремо
# (нормалізація "" == відсутнє).
NESTED_COMPARE_FIELDS = ("lat", "lon", "description", "type", "type_name", "type_icon", "icon")

MIRROR_STALE_AFTER = datetime.timedelta(hours=1)


# ── нормалізація ──────────────────────────────────────────────────────────

def nt(v):
    """Порожній рядок і відсутність поля — одне й те саме для необов'язкового
    тексту (photo_url і подібні)."""
    return v or ""


def nnights(v):
    """Відсутнє поле і nights: 1 — одне й те саме значення."""
    return 1 if v is None else v


def round_coord(v):
    return round(float(v), 4) if v is not None else None


# ── зіставлення route-точок (намір ↔ дзеркало route-шару) ──────────────────

def index_mirror_points(points):
    by_id, by_ld, by_xy = {}, {}, {}
    for p in points:
        pid = p.get("id")
        if pid:
            by_id[pid] = p
        by_ld.setdefault((p.get("label"), p.get("date")), []).append(p)
        xy = (round_coord(p.get("lat")), round_coord(p.get("lon")))
        by_xy.setdefault(xy, []).append(p)
    return by_id, by_ld, by_xy


def match_route_point(intent_p, by_id, by_ld, by_xy):
    """(mirror_point|None, matched_by): 'id' | 'label_date' | 'coords' | None."""
    pid = intent_p.get("id")
    if pid and pid in by_id:
        return by_id[pid], "id"
    cands = by_ld.get((intent_p.get("label"), intent_p.get("date"))) or []
    if len(cands) == 1:
        return cands[0], "label_date"
    xy = (round_coord(intent_p.get("lat")), round_coord(intent_p.get("lon")))
    cands = by_xy.get(xy) or []
    if len(cands) == 1:
        return cands[0], "coords"
    return None, None


# ── зіставлення з альтернативами (намір ↔ live.alt_points) ─────────────────

def index_alt_points(points):
    by_id, by_name, by_xy = {}, {}, {}
    for p in points:
        pid = p.get("id")
        if pid:
            by_id[pid] = p
        by_name.setdefault(p.get("name"), []).append(p)
        xy = (round_coord(p.get("lat")), round_coord(p.get("lon")))
        by_xy.setdefault(xy, []).append(p)
    return by_id, by_name, by_xy


def match_alt_point(intent_p, by_id, by_name, by_xy):
    pid = intent_p.get("id")
    if pid and pid in by_id:
        return by_id[pid], "id"
    # альтернативи не мають "label", а "name" — інтент несе "label"
    cands = by_name.get(intent_p.get("label")) or []
    if len(cands) == 1:
        return cands[0], "name"
    xy = (round_coord(intent_p.get("lat")), round_coord(intent_p.get("lon")))
    cands = by_xy.get(xy) or []
    if len(cands) == 1:
        return cands[0], "coords"
    return None, None


# ── diff однієї точки (намір ↔ вже зіставлена мирор-точка) ─────────────────

def diff_point_fields(intent_p, ref_p):
    """Нормалізована різниця по полях верхнього рівня точки (без
    nested_points — той diff окремо, diff_nested_points). Повертає
    {поле: нове_значення} — прямо придатне як update_point.fields."""
    changed = {}
    for f in POINT_SIMPLE_FIELDS:
        if intent_p.get(f) != ref_p.get(f):
            changed[f] = intent_p.get(f)
    if nnights(intent_p.get("nights")) != nnights(ref_p.get("nights")):
        changed["nights"] = intent_p.get("nights")
    if nt(intent_p.get("popup_html")) != nt(ref_p.get("popup_html")):
        changed["popup_html"] = intent_p.get("popup_html") or ""
    if (intent_p.get("extra_info") or []) != (ref_p.get("extra_info") or []):
        changed["extra_info"] = intent_p.get("extra_info") or []
    return changed


def diff_nested_points(intent_p, ref_p):
    """Зіставляє POI за `name` у межах ОДНІЄЇ точки (nested_points не мають
    стабільного id — методичка цього не передбачає). Повертає (adds, removes,
    updates):
      adds    — [(intent_index, poi_dict)]        → add_poi
      removes — [(mirror_index, name)]             → remove_poi, ВІД
                                                      БІЛЬШОГО індексу до
                                                      меншого (інакшеindices
                                                      «попливуть» після
                                                      кожного видалення)
      updates — [(mirror_index, name, fields)]     → update_poi
    """
    inested = intent_p.get("nested_points") or []
    mnested = ref_p.get("nested_points") or []
    iby_name = {n.get("name"): (idx, n) for idx, n in enumerate(inested)}
    mby_name = {n.get("name"): (idx, n) for idx, n in enumerate(mnested)}

    adds = [(idx, n) for name, (idx, n) in iby_name.items() if name not in mby_name]
    removes = [(idx, name) for name, (idx, n) in mby_name.items() if name not in iby_name]
    updates = []
    for name in sorted(set(iby_name) & set(mby_name)):
        i_idx, i_n = iby_name[name]
        m_idx, m_n = mby_name[name]
        fields = {}
        for f in NESTED_COMPARE_FIELDS:
            if i_n.get(f) != m_n.get(f):
                fields[f] = i_n.get(f)
        if nt(i_n.get("photo_url")) != nt(m_n.get("photo_url")):
            fields["photo_url"] = i_n.get("photo_url") or ""
        if fields:
            updates.append((m_idx, name, fields))

    removes.sort(key=lambda r: -r[0])  # видаляти від більшого індексу
    adds.sort(key=lambda a: a[0])
    updates.sort(key=lambda u: u[0])
    return adds, removes, updates


# ── swap-виявлення: 1 demote + 1 promote того самого kind на тому ж місці ──

def _neighbors(seq_ids, target_id):
    try:
        idx = seq_ids.index(target_id)
    except ValueError:
        return (None, None)
    prev_id = seq_ids[idx - 1] if idx > 0 else None
    next_id = seq_ids[idx + 1] if idx + 1 < len(seq_ids) else None
    return prev_id, next_id


def detect_swap(demote_candidates, promote_candidates, mirror_ids_seq, intent_ids_seq):
    """Якщо є РІВНО одна demote- і РІВНО одна promote-кандидатка того самого
    kind, і в обох послідовностях у них ОДНАКОВІ сусіди (тобто вони стоять
    на тому самому місці) — це проста заміна 1:1: swap_route_alt. Інакше
    (неоднозначність, різні kind, різне місце) НЕ вгадуємо — лишаємо
    demote_points + promote_alt_points окремо, це безпечніший дефолт.

    Повертає (swap_pair|None): (demote_mirror_point, promote_intent_point,
    promote_alt_point) або None.
    """
    if len(demote_candidates) != 1 or len(promote_candidates) != 1:
        return None
    d = demote_candidates[0]
    ip, ap, how = promote_candidates[0]
    if d.get("kind") != ap.get("kind"):
        return None
    d_neighbors = _neighbors(mirror_ids_seq, d.get("id"))
    # intent_ids_seq несе РОЗВ'ЯЗАНІ id — для promote-кандидатки це id
    # альтернативи (ap), а не обов'язково ip.get("id") (могло бути порожнім
    # при зіставленні за запасним ключем).
    p_neighbors = _neighbors(intent_ids_seq, ap.get("id"))
    if d_neighbors != p_neighbors:
        return None
    return (d, ip, ap)


# ── групування promote-кандидатів за суцільними відрізками вставки ─────────

def group_insertions(target_order, stable_ids, insert_ids):
    """Розбиває послідовні "нові" id (яких ще немає на маршруті) на групи
    для окремих вставних викликів: один суцільний відрізок = один виклик,
    прив'язаний до найближчого ПОПЕРЕДНЬОГО стабільного (вже на маршруті,
    нікуди не рухається цим планом) id. Повертає [(after_id|None, [ids])]."""
    groups = []
    current = []
    anchor = None
    for pid in target_order:
        if pid in insert_ids:
            current.append(pid)
        else:
            if current:
                groups.append((anchor, current))
                current = []
            if pid in stable_ids:
                anchor = pid
    if current:
        groups.append((anchor, current))
    return groups


# ── ліміти (перевикористовуємо check_limits.py, не копіюємо) ──────────────

def _limit_row(name, limit, value, where=None, over=None):
    return {
        "name": name, "limit": limit, "value": value, "where": where,
        "over": over, "exceeded": value > limit,
    }


def compute_limits(state):
    """Повертає (ok, rows, payload_bytes) — та сама логіка, що
    check_limits.py, але як функція над довільним state (для --state) і з
    підрахунком КІЛЬКОСТІ порушень для photo_url/description (не лише
    найгіршого значення — інакше в звіті не побачити "3 photo_url >500")."""
    points = state.get("points") or []
    payload = CL.build_payload(state)
    payload_bytes = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    all_nested = [(p.get("label"), n) for p in points for n in (p.get("nested_points") or [])]

    def count_over(items, extractor, limit):
        return sum(1 for it in items if extractor(it) > limit)

    rows = []
    rows.append(_limit_row("точок у set_route", CL.LIMIT_POINTS, len(points)))

    v, w = CL.worst(points, lambda p: len(p.get("nested_points") or []), lambda p: p.get("label"))
    rows.append(_limit_row("nested_points на точку", CL.LIMIT_NESTED_PER_POINT, v, w))

    v, w = CL.worst(points, lambda p: len(p.get("popup_html") or ""), lambda p: p.get("label"))
    rows.append(_limit_row("popup_html (символів)", CL.LIMIT_POPUP_HTML, v, w))

    v, w = CL.worst(all_nested, lambda pn: len(pn[1].get("description") or ""),
                     lambda pn: f"{pn[0]} / {pn[1].get('name')}")
    over = count_over(all_nested, lambda pn: len(pn[1].get("description") or ""), CL.LIMIT_POI_DESCRIPTION)
    rows.append(_limit_row("description POI (символів)", CL.LIMIT_POI_DESCRIPTION, v, w, over))

    v, w = CL.worst(all_nested, lambda pn: len(pn[1].get("photo_url") or ""),
                     lambda pn: f"{pn[0]} / {pn[1].get('name')}")
    over = count_over(all_nested, lambda pn: len(pn[1].get("photo_url") or ""), CL.LIMIT_PHOTO_URL)
    rows.append(_limit_row("photo_url (символів)", CL.LIMIT_PHOTO_URL, v, w, over))

    v, w = CL.worst(points, lambda p: len(p.get("label") or ""), lambda p: p.get("label"))
    rows.append(_limit_row("label точки (символів)", CL.LIMIT_LABEL, v, w))

    v, w = CL.worst(all_nested, lambda pn: len(pn[1].get("name") or ""),
                     lambda pn: f"{pn[0]} / {pn[1].get('name')}")
    rows.append(_limit_row("name POI (символів)", CL.LIMIT_POI_NAME, v, w))

    rows.append(_limit_row("payload сумарно (байт)", CL.LIMIT_PAYLOAD_BYTES, payload_bytes))

    ok = not any(r["exceeded"] for r in rows)
    return ok, rows, payload_bytes


def limits_note(rows):
    """Компактний рядок для гейта: "3 photo_url >500" тощо."""
    parts = []
    for r in rows:
        if not r["exceeded"]:
            continue
        n = r["over"] if r["over"] else 1
        parts.append(f"{n} {r['name']} >{r['limit']} (найгірше: {r['where']}, факт {r['value']})")
    return "; ".join(parts)


# ── основна побудова плану ──────────────────────────────────────────────────

def build_plan(state, mirror, state_path):
    mirror_route = mirror.get("route") or {}
    mirror_points = mirror_route.get("points") or []
    alt_points = (mirror.get("alt") or {}).get("points") or []
    intent_points = state.get("points") or []

    by_id, by_ld, by_xy = index_mirror_points(mirror_points)
    alt_by_id, alt_by_name, alt_by_xy = index_alt_points(alt_points)

    matches = {}            # mirror point id -> (intent_p, mirror_p, matched_by)
    intent_unmatched = []
    match_stats = {"id": 0, "label_date": 0, "coords": 0}

    for ip in intent_points:
        mp, how = match_route_point(ip, by_id, by_ld, by_xy)
        if mp is not None:
            matches[mp["id"]] = (ip, mp, how)
            match_stats[how] += 1
        else:
            intent_unmatched.append(ip)

    mirror_matched_ids = set(matches)
    mirror_unmatched = [mp for mp in mirror_points if mp.get("id") not in mirror_matched_ids]

    # інтент-точки без пари на route-шарі — чи є вони серед альтернатив?
    promote_candidates = []   # [(intent_p, alt_p, how)]
    brand_new = []             # [intent_p] — немає НІДЕ на карті
    alt_match_stats = {"id": 0, "name": 0, "coords": 0}
    for ip in intent_unmatched:
        ap, how = match_alt_point(ip, alt_by_id, alt_by_name, alt_by_xy)
        if ap is not None:
            promote_candidates.append((ip, ap, how))
            alt_match_stats[how] += 1
        else:
            brand_new.append(ip)

    # мирор-точки без пари в намірі: demote-кандидати. home/border не бувають
    # альтернативами (методичка: "відпадають самі, такого kind серед
    # альтернатив нема") — demote_points їх відхилить, тому лишаємо окремо.
    demote_candidates = [mp for mp in mirror_unmatched if mp.get("kind") not in ("home", "border")]
    undemotable = [mp for mp in mirror_unmatched if mp.get("kind") in ("home", "border")]

    # Розв'язана позиційна ідентичність кожної точки наміру — для звірки
    # порядку й прив'язки вставок. НЕ intent_p.get("id") напряму: коли
    # зіставлення впало на запасний ключ (label_date/coords), інтент-точка
    # могла взагалі не нести id (map_point_id), а якийсь id для порівняння
    # з мирор-послідовністю все одно потрібен.
    resolved_id = {}
    for mid, (ip, mp, how) in matches.items():
        resolved_id[id(ip)] = mid
    for ip, ap, how in promote_candidates:
        resolved_id[id(ip)] = ap.get("id")
    for i, ip in enumerate(brand_new):
        resolved_id[id(ip)] = f"__new_{i}__"

    mirror_ids_seq = [p.get("id") for p in mirror_points]
    intent_ids_seq = [resolved_id[id(ip)] for ip in intent_points]

    # 1:1 заміна того самого kind на тому самому місці → swap_route_alt.
    swap = detect_swap(demote_candidates, promote_candidates, mirror_ids_seq, intent_ids_seq)
    swap_demote_id, swap_promote_id = None, None
    if swap:
        d, ip, ap = swap
        swap_demote_id, swap_promote_id = d.get("id"), ap.get("id")
        demote_candidates = [x for x in demote_candidates if x.get("id") != swap_demote_id]
        promote_candidates = [x for x in promote_candidates if x[1].get("id") != swap_promote_id]

    steps = []
    demote_ids = [mp.get("id") for mp in demote_candidates]
    demote_affects = [mp.get("label") for mp in demote_candidates]

    if demote_ids:
        steps.append({
            "op": "demote_points",
            "args": {"point_ids": demote_ids},
            "why": ("точка є на маршруті карти, немає в нашому намірі "
                    "(exports/route_state.json) — переносимо в альтернативи, "
                    "а не видаляємо: наповнення, рейтинги й тред зберігаються "
                    "під тим самим id (trip-map://guide, «Переїзд між шарами»)"),
            "affects": demote_affects,
        })

    if swap:
        d, ip, ap = swap
        steps.append({
            "op": "swap_route_alt",
            "args": {"point_id": d.get("id"), "alt_id": ap.get("id")},
            "why": (f"«{ap.get('name')}» замінює «{d.get('label')}» — та сама позиція, "
                    f"той самий kind={d.get('kind')}: атомарна заміна 1:1. ⚠️ ТАЙМИНГ-СЛОТ "
                    "успадковується від знесеної точки, а не з наміру — перевір "
                    "timing_stale після виконання і онови arrive_at/stay_minutes, якщо "
                    "намір хоче інший час"),
            "affects": [d.get("label"), ap.get("name")],
        })

    # promote_alt_points: групуємо суцільні відрізки вставки за позицією в
    # цільовому порядку намір (intent_ids_seq), щоб не втратити порядок
    # точок у маршруті без set_route.
    promote_ids = {ap.get("id") for _, ap, _ in promote_candidates}
    stable_ids = set(mirror_ids_seq) - set(demote_ids) - ({swap_demote_id} if swap_demote_id else set())
    groups = group_insertions(intent_ids_seq, stable_ids, promote_ids)
    promote_affects = []
    for anchor, ids in groups:
        args = {"alt_ids": ids}
        if anchor is not None:
            args["after_id"] = anchor
        else:
            args["at_index"] = 0
        names = [ap.get("name") for _, ap, _ in promote_candidates if ap.get("id") in ids]
        promote_affects.extend(names)
        steps.append({
            "op": "promote_alt_points",
            "args": args,
            "why": ("точка(и) з наміру мають id, що зараз лежить серед альтернатив "
                    "на карті — наповнення копіюється на сервері, id зберігається "
                    "(рейтинги/тред переїжджають самі). Точки отримають timing_stale:"
                    "true — перевір гейт нижче"),
            "affects": names,
        })

    # add_point — точка є в намірі, але її НІДЕ немає на карті (ні в
    # маршруті, ні в альтернативах). Рідкісний і не зовсім штатний випадок:
    # методичка вимагає створювати кандидатів ОДРАЗУ в карті, а не приносити
    # їх звідкись іззовні. Обробляємо, але позначаємо нотатку в гейтах.
    for ip in brand_new:
        target_pos_id = resolved_id[id(ip)]
        # шукаємо найближчий ПОПЕРЕДНІЙ стабільний id вручну (сусід у
        # intent_ids_seq міг сам бути іншою новою чи промоут-точкою)
        anchor = None
        for pid in intent_ids_seq:
            if pid == target_pos_id:
                break
            if pid in stable_ids:
                anchor = pid
        point_payload = {k: v for k, v in ip.items() if k in CL.KNOWN_POINT_KEYS}
        args = {"point": point_payload}
        if anchor is not None:
            args["after_id"] = anchor
        else:
            args["at_index"] = 0
        steps.append({
            "op": "add_point",
            "args": args,
            "why": ("нова точка в намірі, відсутня і в маршруті, і в альтернативах "
                    "на карті — методичка радить створювати кандидатів одразу в "
                    "карті (add_alt_point), тож перевір, чи це справді бажано, "
                    "перш ніж виконувати"),
            "affects": [ip.get("label")],
        })

    # точкові правки вже наявних (зіставлених, не demoted/swapped) точок.
    update_point_count = 0
    poi_op_count = 0
    unchanged_count = 0
    for mid, (ip, mp, how) in sorted(matches.items()):
        if mid == swap_demote_id:
            continue  # ця точка вже пішла swap-ом вище
        field_diff = diff_point_fields(ip, mp)
        adds, removes, updates = diff_nested_points(ip, mp)

        touched = bool(field_diff)
        if field_diff:
            update_point_count += 1
            note = "" if how == "id" else f" (зіставлено за запасним ключем: {how})"
            steps.append({
                "op": "update_point",
                "args": {"point_id": mid, "fields": field_diff},
                "why": f"змінились поля наявної точки{note}: {sorted(field_diff)}",
                "affects": [ip.get("label") or mp.get("label")],
            })

        for m_idx, name, fields in updates:
            poi_op_count += 1
            touched = True
            steps.append({
                "op": "update_poi",
                "args": {"point_id": mid, "poi_index": m_idx, "fields": fields},
                "why": f"POI «{name}» точки «{mp.get('label')}»: змінились поля {sorted(fields)}",
                "affects": [mp.get("label"), name],
            })
        for m_idx, name in removes:
            poi_op_count += 1
            touched = True
            steps.append({
                "op": "remove_poi",
                "args": {"point_id": mid, "poi_index": m_idx},
                "why": f"POI «{name}» зник з наміру для точки «{mp.get('label')}»",
                "affects": [mp.get("label"), name],
            })
        for i_idx, poi in adds:
            poi_op_count += 1
            touched = True
            steps.append({
                "op": "add_poi",
                "args": {"point_id": mid, "poi": poi, "at_index": i_idx},
                "why": f"новий POI «{poi.get('name')}» у намірі для точки «{mp.get('label')}»",
                "affects": [mp.get("label"), poi.get("name")],
            })
        if not touched:
            unchanged_count += 1

    # ── порядок: чи потрібен set_route? ────────────────────────────────────
    # Стабільна (незмінна) частина порядку — точки, що НЕ demote/promote/swap
    # цим планом. Якщо їхній відносний порядок у намірі відрізняється від
    # порядку на карті — точкові тули це не виправлять (вони не рухають чужі
    # точки), потрібен set_route.
    all_moved = (set(demote_ids) | promote_ids | {swap_demote_id, swap_promote_id}
                 | {resolved_id[id(p)] for p in brand_new})
    all_moved.discard(None)
    stable_target = [pid for pid in intent_ids_seq if pid not in all_moved]
    stable_mirror = [pid for pid in mirror_ids_seq if pid not in all_moved]
    order_ok = stable_target == stable_mirror

    set_route_step = None
    if not order_ok:
        payload_points = [{k: v for k, v in p.items() if k in CL.KNOWN_POINT_KEYS}
                           for p in intent_points]
        set_route_step = {
            "op": "set_route",
            "args": {"points": payload_points},
            "why": ("порядок точок у намірі не відповідає порядку на карті — точкові "
                    "тули не переставляють чужі точки. ⚠️ set_route НЕ робить "
                    "авто-rebase: якщо rev карти зміниться між зчитуванням дзеркала і "
                    "виконанням, запис буде відхилено конфліктом, а не перенесено"),
            "affects": ["увесь маршрут"],
        }
        # За правилом «set_route останнім» — і без демоут/промоут/поінт-кроків:
        # повна переробка сама несе весь бажаний стан.
        steps = [set_route_step]
        update_point_count = 0
        poi_op_count = 0

    # ── гейти ───────────────────────────────────────────────────────────────
    notes = []

    booked = mirror_route.get("booked") or {}
    final_ids = (set(mirror_ids_seq) - set(demote_ids)
                 - ({swap_demote_id} if swap_demote_id else set()) | promote_ids
                 | ({swap_promote_id} if swap_promote_id else set())
                 | {p.get("id") for p in brand_new if p.get("id")})
    if order_ok:
        missing_booked = [bid for bid in booked if bid not in final_ids]
    else:
        # set_route: фінальний набір id — це те, що явно передано в points
        # (id зберігаються лише якщо присутні в payload).
        set_route_ids = {p.get("id") for p in intent_points if p.get("id")}
        missing_booked = [bid for bid in booked if bid not in set_route_ids]
    booked_ok = not missing_booked
    if missing_booked:
        for bid in missing_booked:
            info = booked.get(bid) or {}
            name = info.get("name") or bid
            dates = info.get("dates")
            notes.append(f"✗ booked: точка «{name}» (id={bid}, dates={dates}) зникає з "
                         "маршруту цим планом — сервер відхилить операцію, план "
                         "НЕПРИДАТНИЙ до виконання без ручного втручання")

    limits_ok, limit_rows, payload_bytes = compute_limits(state)
    lim_note = limits_note(limit_rows)
    if lim_note:
        notes.append(f"ліміти: {lim_note}")

    timing_stale = [p.get("label") or p.get("id") for p in mirror_points if p.get("timing_stale")]

    if undemotable:
        for mp in undemotable:
            notes.append(f"⚠️ точка «{mp.get('label')}» (id={mp.get('id')}, kind="
                         f"{mp.get('kind')}) є на маршруті карти, немає в намірі, але "
                         f"kind={mp.get('kind')} не буває альтернативою — demote_points "
                         "її відхилить. Потрібне ручне рішення (методичка: home/border "
                         "«відпадають самі»)")

    if brand_new:
        names = ", ".join(p.get("label") or "?" for p in brand_new)
        notes.append(f"⚠️ {len(brand_new)} нових точок без сліду на карті ({names}) — "
                     "методичка радить створювати кандидатів одразу в карті "
                     "(add_alt_point), а не приносити готовими ззовні; перевір задум "
                     "перед виконанням add_point")

    fallback_matches = match_stats["label_date"] + match_stats["coords"]
    if fallback_matches:
        notes.append(f"зіставлення точок: {match_stats['id']} за id, "
                     f"{match_stats['label_date']} за (label,date), "
                     f"{match_stats['coords']} за координатами — перевір запасні "
                     "збіги вручну, вони ненадійніші за id")
    alt_fallback = alt_match_stats["name"] + alt_match_stats["coords"]
    if alt_fallback:
        notes.append(f"зіставлення з альтернативами: {alt_match_stats['id']} за id, "
                     f"{alt_match_stats['name']} за name, "
                     f"{alt_match_stats['coords']} за координатами")

    if set_route_step is not None:
        notes.append("set_route: rebase НЕ буде — якщо rev карти зміниться до "
                     "виконання, треба перечитати дзеркало (pull_map.py) і "
                     "перегенерувати план заново")

    pulled_at_raw = mirror.get("pulled_at")
    mirror_age_note = None
    if pulled_at_raw:
        try:
            pulled_dt = datetime.datetime.fromisoformat(pulled_at_raw)
            age = datetime.datetime.now().astimezone() - pulled_dt
            if age > MIRROR_STALE_AFTER:
                mirror_age_note = (f"дзеркало старше години ({age}) — перечитай карту: "
                                   "python3 scripts/pull_map.py")
                notes.append(f"⚠️ {mirror_age_note}")
        except ValueError:
            notes.append(f"⚠️ не вдалось розпарсити pulled_at дзеркала: {pulled_at_raw!r}")

    if demote_ids and pulled_at_raw:
        try:
            state_mtime = datetime.datetime.fromtimestamp(
                state_path.stat().st_mtime
            ).astimezone()
            pulled_dt = datetime.datetime.fromisoformat(pulled_at_raw)
            if state_mtime < pulled_dt:
                notes.append(
                    f"⚠️ demote стосується точки(ок), що зникли з наміру — але сам намір "
                    f"({state_path.name}, згенеровано {state_mtime.isoformat(timespec='minutes')}) "
                    f"старіший за дзеркало (знято {pulled_dt.isoformat(timespec='minutes')}): "
                    "це може бути НЕ рішення прибрати точку, а розсинхрон — карту хтось "
                    "змінив після того, як намір згенерували. Перш ніж виконувати demote, "
                    "звір з людиною, чи намір досі актуальний."
                )
        except (ValueError, OSError):
            pass
    notes = [n for n in notes if n]

    gates = {
        "booked_ok": booked_ok,
        "limits_ok": limits_ok,
        "timing_stale": timing_stale,
        "notes": notes,
    }

    summary = {
        "demote": len(demote_ids) + (1 if swap else 0),
        "promote": len(promote_ids) + (1 if swap else 0),
        "swap": 1 if swap else 0,
        "add_point": len(brand_new),
        "update_point": update_point_count,
        "poi_ops": poi_op_count,
        "set_route": set_route_step is not None,
        "unchanged": unchanged_count,
    }

    plan = {
        "_": "План публікації. Складений plan_push.py, виконує інший скрипт. Не редагувати руками.",
        "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "map_id": mirror.get("map_id") or CTX.map_id,
        "base_rev": mirror.get("rev"),
        "mirror_pulled_at": pulled_at_raw,
        "route_state_path": str(state_path),
        "gates": gates,
        "summary": summary,
        "steps": steps,
    }
    return plan


# ── людський звіт ────────────────────────────────────────────────────────

def print_report(plan):
    s = plan["summary"]
    g = plan["gates"]

    age = ""
    if plan.get("mirror_pulled_at"):
        try:
            pulled_dt = datetime.datetime.fromisoformat(plan["mirror_pulled_at"])
            mins = int((datetime.datetime.now().astimezone() - pulled_dt).total_seconds() // 60)
            age = f" · дзеркало знято {mins} хв тому"
        except ValueError:
            pass

    mid = plan.get("map_id") or "—"
    short = mid if len(mid) <= 8 else mid[:8] + "…"
    print(f"▶ {CTX.trip_id} · карта rev {plan.get('base_rev')} (map {short}){age}")

    bits = []
    if s["demote"]:
        bits.append(f"{s['demote']} демоут" + ("и" if s["demote"] != 1 else ""))
    if s["swap"]:
        bits.append(f"{s['swap']} заміна(и) 1:1")
    if s["promote"]:
        bits.append(f"{s['promote']} промоут" + ("и" if s["promote"] != 1 else ""))
    if s["add_point"]:
        bits.append(f"{s['add_point']} нові точки")
    if s["update_point"]:
        bits.append(f"{s['update_point']} правки точок")
    if s["poi_ops"]:
        bits.append(f"{s['poi_ops']} правки POI")
    if s["set_route"]:
        bits.append("ПОВНА переробка (set_route)")
    bits.append(f"{s['unchanged']} точок без змін")
    if not any([s["demote"], s["swap"], s["promote"], s["add_point"],
                s["update_point"], s["poi_ops"], s["set_route"]]):
        print("план: ПОРОЖНІЙ — намір і карта тотожні, публікувати нічого")
    else:
        print("план: " + ", ".join(bits))

    booked_mark = "✓" if g["booked_ok"] else "✗"
    limits_mark = "✓" if g["limits_ok"] else "✗"
    booked_n = 0  # порахований нижче з notes лише якщо є розбіжність
    stale = ", ".join(g["timing_stale"]) if g["timing_stale"] else "—"
    print(f"гейти: booked {booked_mark} · ліміти {limits_mark} · timing_stale: {stale}")
    for n in g["notes"]:
        print(f"  · {n}")

    if not g["booked_ok"] or not g["limits_ok"]:
        print("\n✗ ПЛАН НЕПРИДАТНИЙ до виконання без ручного втручання — див. нотатки гейтів вище")

    if plan["steps"]:
        print(f"\nкроки ({len(plan['steps'])}):")
        for i, step in enumerate(plan["steps"], 1):
            affects = ", ".join(a for a in (step.get("affects") or []) if a)
            print(f" {i}. {step['op']}" + (f"  [{affects}]" if affects else ""))
            print(f"    {step['why']}")


# ── CLI ───────────────────────────────────────────────────────────────────

def main(argv):
    parser = argparse.ArgumentParser(
        description="Планувальник публікації: diff намір ↔ дзеркало карти → план тулів."
    )
    parser.add_argument("--json", action="store_true", help="тільки JSON плану у stdout")
    parser.add_argument("--state", help="інший route_state.json (типово CTX.route_state)")
    parser.add_argument("--mirror", help="інше дзеркало (типово CTX.live_mirror)")
    args = parser.parse_args(argv[1:])

    state_path = Path(args.state).expanduser() if args.state else CTX.route_state
    mirror_path = Path(args.mirror).expanduser() if args.mirror else CTX.live_mirror

    if not state_path.exists():
        print(f"⚠️  Немає {state_path} — спершу згенеруй route_state.json.", file=sys.stderr)
        return 1
    if not mirror_path.exists():
        print(f"⚠️  Немає {mirror_path} — спершу python3 scripts/pull_map.py.", file=sys.stderr)
        return 1

    state = json.loads(state_path.read_text(encoding="utf-8"))
    mirror = json.loads(mirror_path.read_text(encoding="utf-8"))

    plan = build_plan(state, mirror, state_path)

    out_path = CTX.trip_dir / "push_plan.json"
    CTX.write_json(out_path, plan)

    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=1))
    else:
        print_report(plan)
        print(f"\n→ {out_path}")

    unusable = not plan["gates"]["booked_ok"] or not plan["gates"]["limits_ok"]
    return 1 if unusable else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
