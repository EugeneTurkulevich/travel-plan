"""
scripts/migration_diff.py

Приймальний гейт міграції: звіряє щойно згенерований route_state.json з
еталонним файлом зі старого репозиторію (../travel202609). Поки джерело
істини — старий репо (CLAUDE.md §3), цей скрипт — єдиний спосіб довести, що
новий генератор (generate_state.py) не загубив і не спотворив дані.

Дві незалежні перевірки, обидві обовʼязкові:

(а) СТРУКТУРНА. Точка за точкою, по порядку (без прив'язки по id — в
    еталоні id немає): label, lat, lon, kind, date, arrive_at, stay_minutes,
    nights, кожне поле кожного nested_points. Плюс country_info і
    booking_persons.

    ⚠️ nights нормалізується перед порівнянням: відсутнє поле і `nights: 1`
    — те саме значення (сервер завжди віддає 1, локально поле є лише в
    багатонічних точках).

    ⚠️ id очікується лише в новому файлі — не розбіжність, а факт, який
    рахуємо і показуємо в зведенні.

(б) ЗБЕРЕЖЕННЯ ТЕКСТУ. popup_html не зобовʼязаний збігатися дослівно (ручна
    вичитка могла переставити речення між карткою бібліотеки й overlay), але
    жодне речення еталона не має зникнути безслідно: воно має знайтись або в
    popup_html нової точки, або у overlay/<id>.md цієї ж точки (шлях — через
    trip_ctx, id береться з НОВОГО файлу за тим самим індексом).

Usage:
    python3 scripts/migration_diff.py <еталон.json> [--state <новий.json>]

Без --state новий файл береться з CTX.route_state.

Вихід: зведення + код виходу 0, якщо немає ні структурних розбіжностей, ні
втрачених речень, інакше 1.
"""
import argparse
import html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trip_ctx import paths  # noqa: E402

CTX = paths()

# Поля точки верхнього рівня, які звіряємо по порядку.
POINT_FIELDS = (
    "label", "lat", "lon", "kind", "date", "arrive_at", "stay_minutes", "nights",
)
# Поля одного nested_point.
NESTED_FIELDS = (
    "name", "lat", "lon", "description", "type", "type_name", "type_icon",
    "icon", "photo_url",
)

# Речення коротші за це (після схлопування пробілів) не перевіряємо — шум.
MIN_SENTENCE_LEN = 25

# Скорочення, після яких крапка НЕ означає кінець речення (див. migrate_cards.py).
_ABBR = {"ст", "рр", "р", "вул", "км", "обл", "кв", "тис", "млн", "див",
         "напр", "прим", "ім", "гр", "св"}


# ── нормалізація тексту ──────────────────────────────────────────────────────

def normalize_nights(value):
    """Відсутнє поле і nights: 1 — одне й те саме значення."""
    return 1 if value is None else value


def collapse_ws(text):
    return re.sub(r"\s+", " ", text or "").strip()


# HTML-теги, після яких у розмітці попапів завжди йде змістовий розрив
# (заголовок/абзац/список), а не просто інлайн-емфаза всередині речення.
# «<b>Заголовок</b><br>Опис» — саме цей патерн: без розриву тут заголовок без
# крапки приклеюється до першого речення опису і збіг ламається.
_BREAK_TAG_NAMES = {
    "br", "hr", "div", "p", "b", "li", "ul", "ol", "tr", "table",
    "h1", "h2", "h3", "h4", "h5", "h6",
}


def strip_markup(text):
    """Знімає HTML-теги і базове markdown-форматування. HTML з popup_html і
    markdown з overlay/*.md мають порівнюватись на рівних умовах — без цього
    один непереставлений `**` ламає збіг цілого речення.

    Блокові теги (br/div/p/hr/b/li/…) перетворюються на перенос рядка, а не
    просто на пробіл — інакше «<b>Заголовок</b><br>Опис» стає одним реченням
    без роздільника, бо в заголовку немає крапки."""
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)         # markdown-зображення
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)      # markdown-посилання → текст

    def _tag_sub(m):
        name = m.group(1).lower()
        return "\n" if name in _BREAK_TAG_NAMES else " "

    text = re.sub(r"</?([a-zA-Z0-9]+)(?:\s[^>]*)?/?>", _tag_sub, text)  # HTML-теги
    text = re.sub(r"[*_`#>]+", "", text)                      # markdown емфаза/заголовки/цитати
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)       # маркери списків
    return text


def _split_sentences_in_block(block):
    """Ріже ОДИН блок (без переносів рядка всередині) на речення по '.', '!',
    '?', не розриваючи після відомих скорочень (ст., р., вул. тощо)."""
    sentences = []
    start = 0
    for m in re.finditer(r"[.!?]+(\s+|$)", block):
        punct_start = m.start()
        end = m.end()
        wm = re.search(r"([A-Za-zА-Яа-яІЇЄҐіїєґ']+)\s*$", block[:punct_start])
        word = wm.group(1).lower() if wm else ""
        if word in _ABBR:
            continue
        sentences.append(block[start:end].strip())
        start = end
    tail = block[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def split_sentences(text):
    """Спершу ріже за блоковими розривами (\\n — від br/div/p/hr/b тощо,
    або справжні переноси рядка в markdown), тоді всередині кожного блоку —
    за розділовими знаками. Заголовок без крапки в своєму блоці не зливається
    з наступним абзацом."""
    sentences = []
    for block in re.split(r"\n+", text):
        block = block.strip()
        if not block:
            continue
        sentences.extend(_split_sentences_in_block(block))
    return [s for s in sentences if s]


# ── (а) структурна перевірка ─────────────────────────────────────────────────

def diff_points(etalon_points, new_points):
    """Повертає (diffs: list[str], id_present, id_total)."""
    diffs = []
    id_present = 0

    if len(etalon_points) != len(new_points):
        diffs.append(
            f"кількість точок не збігається: еталон={len(etalon_points)} "
            f"нове={len(new_points)}"
        )

    for i, e in enumerate(etalon_points):
        if i >= len(new_points):
            diffs.append(f"точка[{i}] «{e.get('label')}»: відсутня в новому файлі")
            continue
        n = new_points[i]
        if "id" in n:
            id_present += 1
        label = e.get("label") or n.get("label") or f"#{i}"

        for field in POINT_FIELDS:
            ev = e.get(field)
            nv = n.get(field)
            if field == "nights":
                ev, nv = normalize_nights(ev), normalize_nights(nv)
            if ev != nv:
                diffs.append(
                    f"точка[{i}] «{label}».{field}: еталон={ev!r} / нове={nv!r}"
                )

        e_nested = e.get("nested_points") or []
        n_nested = n.get("nested_points") or []
        if len(e_nested) != len(n_nested):
            diffs.append(
                f"точка[{i}] «{label}».nested_points: кількість не збігається "
                f"(еталон={len(e_nested)} нове={len(n_nested)})"
            )
        for j in range(min(len(e_nested), len(n_nested))):
            en, nn = e_nested[j], n_nested[j]
            for field in NESTED_FIELDS:
                ev, nv = en.get(field), nn.get(field)
                if ev != nv:
                    diffs.append(
                        f"точка[{i}] «{label}».nested_points[{j}].{field}: "
                        f"еталон={ev!r} / нове={nv!r}"
                    )

    return diffs, id_present, len(new_points)


def diff_top_level(etalon, new):
    diffs = []

    e_ci = etalon.get("country_info") or {}
    n_ci = new.get("country_info") or {}
    for cc in sorted(set(e_ci) | set(n_ci)):
        if e_ci.get(cc) != n_ci.get(cc):
            diffs.append(f"country_info[{cc}]: розбіжність")

    e_bp = etalon.get("booking_persons")
    n_bp = new.get("booking_persons")
    if e_bp != n_bp:
        diffs.append(f"booking_persons: еталон={e_bp!r} / нове={n_bp!r}")

    return diffs


# ── (б) перевірка збереження тексту ──────────────────────────────────────────

def load_route_slugs(route_json_path):
    """route.json.days[].points[].id, по порядку, як точки йдуть у маршруті.

    ⚠️ Це slug бібліотеки (`ua-kyiv`), а НЕ те, що лежить у полі `id` точки
    згенерованого route_state.json — там `id` це map_point_id карти
    (`c7642202`), службовий ідентифікатор для set_route, а не назва картки.
    Slug для overlay/<slug>.md треба брати саме звідси, зіставляючи з новим
    файлом ПО ІНДЕКСУ: route.json обходиться в тому самому порядку, в якому
    генератор кладе точки в route_state.json.

    Повертає [] (без падіння), якщо route.json відсутній — тоді overlay-крок
    просто нічого не знайде і чесно про це прозвітує."""
    if not route_json_path.exists():
        return []
    route = json.loads(route_json_path.read_text(encoding="utf-8"))
    return [
        p.get("id")
        for day in route.get("days") or []
        for p in day.get("points") or []
    ]


def check_text_preserved(etalon_points, new_points, overlay_dir, route_slugs):
    """Повертає (losses: list[(label, sentence)], checked, overlay_candidates,
    overlay_found) — overlay_candidates це точки, для яких вдалось визначити
    slug (є відповідник у route_slugs), overlay_found — скільки з них реально
    мають файл overlay/<slug>.md на диску."""
    losses = []
    checked = 0
    overlay_candidates = 0
    overlay_found = 0

    for i, e in enumerate(etalon_points):
        if i >= len(new_points):
            continue
        n = new_points[i]
        label = e.get("label") or n.get("label") or f"#{i}"

        sentences = split_sentences(strip_markup(e.get("popup_html", "")))
        sentences = [s for s in sentences if len(collapse_ws(s)) >= MIN_SENTENCE_LEN]
        if not sentences:
            continue
        checked += 1

        new_text = collapse_ws(strip_markup(n.get("popup_html", "")))

        overlay_text = ""
        slug = route_slugs[i] if i < len(route_slugs) else None
        if slug:
            overlay_candidates += 1
            overlay_path = overlay_dir / f"{slug}.md"
            if overlay_path.exists():
                overlay_found += 1
                overlay_text = collapse_ws(strip_markup(
                    overlay_path.read_text(encoding="utf-8")
                ))

        for s in sentences:
            norm = collapse_ws(s)
            if norm in new_text or (overlay_text and norm in overlay_text):
                continue
            losses.append((label, s))

    return losses, checked, overlay_candidates, overlay_found


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv):
    parser = argparse.ArgumentParser(
        description="Приймальний гейт міграції route_state.json"
    )
    parser.add_argument("etalon", help="еталонний route_state.json (старий репо)")
    parser.add_argument("--state", help="новий route_state.json (типово CTX.route_state)")
    args = parser.parse_args(argv[1:])

    etalon_path = Path(args.etalon).expanduser()
    new_path = Path(args.state).expanduser() if args.state else CTX.route_state

    if not etalon_path.exists():
        print(f"⚠️  Немає еталонного файлу: {etalon_path}", file=sys.stderr)
        return 1
    if not new_path.exists():
        print(f"⚠️  Немає нового файлу: {new_path}", file=sys.stderr)
        return 1

    etalon = json.loads(etalon_path.read_text(encoding="utf-8"))
    new = json.loads(new_path.read_text(encoding="utf-8"))

    e_points = etalon.get("points") or []
    n_points = new.get("points") or []

    struct_diffs, id_present, id_total = diff_points(e_points, n_points)
    struct_diffs += diff_top_level(etalon, new)

    route_slugs = load_route_slugs(CTX.route_json)
    losses, checked, overlay_candidates, overlay_found = check_text_preserved(
        e_points, n_points, CTX.overlay_dir, route_slugs
    )

    print(f"еталон:  {etalon_path}")
    print(f"нове:    {new_path}")
    print()
    print(f"(а) структура: звірено точок {min(len(e_points), len(n_points))} з "
          f"{len(e_points)} (еталон) / {len(n_points)} (нове)")
    print(f"    id: {id_present}/{id_total} точок нового файлу мають id "
          f"(очікувано — лише в новому файлі)")
    if struct_diffs:
        print(f"    розбіжностей: {len(struct_diffs)}")
        for d in struct_diffs:
            print(f"      · {d}")
    else:
        print("    розбіжностей немає")

    print()
    print(f"(б) текст: перевірено речень {checked} точок; "
          f"slug для overlay визначено у {overlay_candidates}/{len(n_points)} точок "
          f"(route.json: {CTX.route_json})")
    print(f"    overlay-файлів прочитано: {overlay_found} з {overlay_candidates} "
          f"точок з відомим slug")
    if losses:
        print(f"    ВТРАЧЕНО речень: {len(losses)}")
        for label, s in losses:
            print(f"      · [{label}] {s}")
    else:
        print("    втрачених речень немає")

    ok = not struct_diffs and not losses
    print()
    print("✓ гейт пройдено" if ok else "✗ гейт НЕ пройдено")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
