"""
scripts/sync_from_map.py

Напрямок КАРТА → ЛОКАЛЬНІ ДАНІ. Приводить `route.json` активного профілю й,
за потреби, бібліотеку `places/` до стану, зафіксованого в `live_mirror.json`
(джерело — `pull_map.py`). Ніколи не пише в карту: лише читає дзеркало й
пише локальні файли (route.json, нові картки). Це узгоджується з
CLAUDE.md §3 — заморожений напрямок push, а не pull.

НАВІЩО. Робота з картою зараз веде інший агент, і локальний стан застаріває
щодня: за три дні rev 13 → 16 зʼявилась нова точка маршруту, нові
альтернативи, нові звʼязки. Перед тим, як `plan_push.py --dry-run` зможе
колись дати порожній план (критерій переходу на новий канал), локальні дані
треба один раз привести до стану карти.

⚠️ Дефолт — ПОКАЗ, не запис. Дані вичитані руками, тому мовчазне
застосування неприпустиме:

    python3 scripts/sync_from_map.py            # показати, що зробив би
    python3 scripts/sync_from_map.py --apply    # застосувати

Що робить (докладніше — у коментарях коду):
  (а) route.json: порядок точок, kind/date/nights/arrive_at/stay_minutes/
      map_point_id, склад alternates — приводяться до стану карти. Власні
      поля (booking_note, перекриття label/lat/lon) переносяться незмінними,
      бо карта про них не знає.
  (б) для точок і альтернатив без картки в бібліотеці — генеруються нові
      картки глибини «базовий» (крім тих, що впізнані як травневі кандидати
      Ф6 — вони поза обсягом цієї синхронізації, див. _MAY_CANDIDATE_MARKER).
      POI картки розкладаються по секціях за `type` — так само, як їх потім
      читає generate_state.py (`poi_categories` профілю), а не звалюються в
      одну «Цікаві POI»: інакше публікація тихцем звела б чужу
      категоризацію (viewpoint/icecream/craft/local) до загальної зірочки —
      знайдено 07.08.2026 через план публікації на 58 зайвих update_poi.
  (в) КАРТКИ, ВИЧИТАНІ ЛЮДИНОЮ, НІКОЛИ НЕ ПЕРЕПИСУЮТЬСЯ. Розріз — позначка
      `<!-- Імпортовано з карти … -->` (STRUCTURE_PROPOSAL.md §5.2): є вона —
      картка машинна, і сама синхронізація має право перегенерувати її з
      свіжого стану карти (саме так лагодяться картки, зіпсовані до
      виправлення пункту вище); немає — картку вичитано, вона недоторканна.
      Якщо ВИЧИТАНА картка стверджує роль, яка розійшлась зі станом карти
      (лишилась «альтернативою», хоч тепер у маршруті) — це йде окремим
      блоком звіту, а не мовчки виправляється.

⚠️ ПРИПУЩЕННЯ, яке варто знати, коли скрипт колись дасть неочікуване число:
альтернативи без збігу в бібліотеці ще й звіряються з
`_migration/slugmap.json` (секція `candidates_2026_05` — травневі кандидати
Ф6, «не чіпається» за CLAUDE.md). Прямого звʼязку координат там нема (слаги
складені руками з заголовків старого репо, якого тут немає), тому ознака —
текстовий маркер у першому коментарі точки: «з травневої карти». Це
припущення самоперевіряється: кількість точок з маркером звіряється з
кількістю записів у `candidates_2026_05`, і розбіжність друкується як
попередження, а не мовчки ковтається.
"""
import argparse
import datetime
import html
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trip_ctx import paths  # noqa: E402

CTX = paths()

# ── країни: грубі bbox для визначення коду за координатами (§ постановки: "приблизно") ──
COUNTRY_BBOXES = {
    "UA": (44.0, 52.5, 22.0, 40.5, "Україна", "🇺🇦"),
    "RO": (43.5, 48.5, 20.0, 29.8, "Румунія", "🇷🇴"),
    "BG": (41.0, 44.3, 22.0, 28.7, "Болгарія", "🇧🇬"),
    "GR": (34.5, 41.9, 19.0, 28.3, "Греція", "🇬🇷"),
}

# Маркер травневих кандидатів (Ф6) — коментарі точок 03.08 позначені саме так.
_MAY_CANDIDATE_MARKER = re.compile(r"з травневої карти", re.IGNORECASE)

# Картка стверджує роль «альтернатива, не в маршруті» — якщо після синку
# точка з таким текстом опиняється в route.json як точка маршруту, це
# розбіжність картки й карти (правило (в): показати, не виправляти).
_ALT_ROLE_MARKER = re.compile(r"Альтернативна точка|не в маршруті", re.IGNORECASE)

# Координатний поріг «це та сама точка» — ~5 км, з запасом на неточність
# геокодування; звичайна похибка ручного геокодування (соті частки градуса)
# впевнено проходить.
_COORD_THRESHOLD = 0.05

# Картка машинного походження, яку МОЖНА перегенерувати (§ в постановки,
# STRUCTURE_PROPOSAL.md §5.2). Той самий рядок, який build_new_card_md сама
# й пише — round-trip: перегенерована картка лишається перегенеровуваною,
# аж поки людина не вичитає її й не забере позначку (тоді картка стає
# недоторканною, як і решта бібліотеки).
_IMPORTED_MARKER_RE = re.compile(r"<!--\s*Імпортовано з карти")

# Базова секція POI, завжди присутня незалежно від poi_categories профілю
# (PLACE_SPEC.md §3: «базова POI-таблиця, категорія poi»). Тип "poi" і БУДЬ-ЯКИЙ
# тип, якого немає серед poi_categories цієї поїздки, ідуть сюди — так само,
# як розпізнає generate_state.py.
BASE_POI_SECTION = "Цікаві POI"

# Порядок секцій у згенерованій картці — той самий, що в
# generate_state.py:build_nested_points (poi → viewpoint → icecream → craft →
# local), суто для читомості; на розпізнавання (get_section за заголовком)
# порядок не впливає.
_CATEGORY_ORDER = ("poi", "viewpoint", "icecream", "craft", "local")


def is_machine_imported(card_text):
    return bool(_IMPORTED_MARKER_RE.search(card_text))


# ── дрібні текстові помічники ────────────────────────────────────────────────

def iso_to_ukr_date(iso_date):
    """'2026-08-07' → '07.08.2026'."""
    try:
        y, m, d = iso_date.split("-")
        return f"{d}.{m}.{y}"
    except ValueError:
        return iso_date


_BLOCK_BREAK_RE = re.compile(r"</(div|p|li|tr)\s*>|<br\s*/?>|<hr[^>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_B_TAG_RE = re.compile(r"<b>(.*?)</b>", re.IGNORECASE | re.DOTALL)
_I_TAG_RE = re.compile(r"<i>(.*?)</i>", re.IGNORECASE | re.DOTALL)


def strip_html(raw):
    """popup_html → чиста проза: `<b>`/`<i>` переходять у `**bold**`/`*italic*`
    (а не просто зникають) — так само, як generate_state.py:_inline_md() читає
    їх НАЗАД у `<b>`/`<i>` при наступній збірці попапу. Без цього round-trip
    непомітно губить виділення (і далі показує це як «змінився popup_html» —
    шум того самого сорту, що й категорії POI). Блокові теги стають
    переносами рядка, решта тегів (у т.ч. `<img>`) прибирається без сліду,
    HTML-сутності розкодовано. Не намагається відновити структуру абзаців
    джерела — це «базовий» рівень, позначений як не вичитаний."""
    if not raw:
        return ""
    text = _B_TAG_RE.sub(r"**\1**", raw)
    text = _I_TAG_RE.sub(r"*\1*", text)
    text = _BLOCK_BREAK_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n\n".join(lines)


_HR_SPLIT_RE = re.compile(r"<hr[^>]*>", re.IGNORECASE)
_BOOKING_DIV_RE = re.compile(r"<div style='background:#fff7e0.*?</div>", re.IGNORECASE | re.DOTALL)
_LEADING_TITLE_RE = re.compile(r"^\s*<b>[^<]*</b>\s*<br\s*/?>\s*", re.IGNORECASE)


def extract_core_description(popup_html):
    """`## Опис` картки — лише вступна проза popup_html, БЕЗ категорійних
    блоків категорій і без букінг-рядка.

    Причина: карта збирає popup_html точно так само, як generate_state.py
    (`build_popup_html`) — вступ, потім по одному `<hr>`-блоку на кожну
    категорію POI. Якщо взяти popup_html цілком (як strip_html), увесь
    категорійний текст ляже в Опис ЯК ПРОЗА — а після виправлення POI-секцій
    generate_state.py відновить ті самі блоки ЗАНОВО з properly-типізованих
    секцій картки. Результат — подвоєний вміст у майбутньому попапі, і
    вочевидь інший popup_html за той, що зараз на карті (нові діфи в плані
    публікації рівно там, де щойно прибрали старі). Букінг-рядок
    (`🛏️ ...`) — властивість появи точки в конкретному маршруті
    (route.json booking_note, PLACE_SPEC.md §4), не самого місця, тому в
    картку бібліотеки не йде."""
    if not popup_html:
        return ""
    core = _HR_SPLIT_RE.split(popup_html, maxsplit=1)[0]
    core = _BOOKING_DIV_RE.sub("", core)
    core = _LEADING_TITLE_RE.sub("", core, count=1)
    return strip_html(core)


# Мапа отримує extra_info як список HTML-рядків '<b>{emoji} {Назва}</b><br>{текст}'
# — точнісінько те, що видає build_extra_info() у generate_state.py. Розбір тут
# дзеркальний до нього: без нього картка мовчки лишається без "В'їзд на авто" /
# "Паркування" / "Де зупинитись", і той самий round-trip, який щойно
# полагодили для POI, публікація зводить ці секції до порожнечі — знайдено
# тим самим прогоном plan_push.py, яким перевіряли POI-фікс.
_EXTRA_INFO_LABELS = ("В'їзд на авто", "Паркування", "Де зупинитись")
_EXTRA_INFO_PREFIX_RE = re.compile(r"^<b>[^<]*</b>\s*<br\s*/?>\s*", re.IGNORECASE)


def split_extra_info_sections(extra_info):
    """[html_рядок, ...] → {'В'їзд на авто': текст, 'Паркування': текст,
    'Де зупинитись': текст} — лише мітки, які реально знайшлись."""
    result = {}
    for entry in (extra_info or []):
        label = next((l for l in _EXTRA_INFO_LABELS if l in entry[:80]), None)
        if not label:
            continue
        body = _EXTRA_INFO_PREFIX_RE.sub("", entry, count=1)
        result[label] = strip_html(body)
    return result


def first_sentence(text):
    if not text:
        return ""
    m = re.search(r"^.{1,240}?[.!?](?:\s|$)", text)
    return (m.group(0).strip() if m else text[:200].strip())


def md_escape_cell(text):
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


# ── бібліотека місць: індекс ID + Координати для пошуку «а це не наше місце?» ──

_ID_RE = re.compile(r"\|\s*\*\*ID\*\*\s*\|\s*`([^`]+)`\s*\|")
_COORD_RE = re.compile(r"\|\s*\*\*Координати\*\*\s*\|\s*([\d.\-]+)\s*,\s*([\d.\-]+)\s*\|")
_TITLE_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)


def load_library_index(places_dir):
    """[{slug, title, lat, lon, path, text}] — лише картки місць (є рядок ID)."""
    out = []
    if not places_dir.exists():
        return out
    for path in sorted(places_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        id_m = _ID_RE.search(text)
        if not id_m:
            continue  # CATALOG.md / country_info.md / practical_info.md
        coord_m = _COORD_RE.search(text)
        title_m = _TITLE_RE.search(text)
        out.append({
            "slug": id_m.group(1).strip(),
            "title": title_m.group(1).strip() if title_m else path.stem,
            "lat": float(coord_m.group(1)) if coord_m else None,
            "lon": float(coord_m.group(2)) if coord_m else None,
            "path": path,
            "text": text,
        })
    return out


def find_library_match(lat, lon, name, library_index, report):
    """Шукає картку бібліотеки за координатами (поріг _COORD_THRESHOLD),
    з іменем як допоміжним, ненав'язливим сигналом (лише попередження, якщо
    розходиться). Повертає slug або None. Кілька кандидатів у порозі —
    береться найближчий, різниця фіксується як неоднозначність у звіті."""
    candidates = []
    for card in library_index:
        if card["lat"] is None or card["lon"] is None:
            continue
        dlat, dlon = abs(card["lat"] - lat), abs(card["lon"] - lon)
        if dlat <= _COORD_THRESHOLD and dlon <= _COORD_THRESHOLD:
            candidates.append((dlat + dlon, card))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    best = candidates[0][1]
    if len(candidates) > 1:
        report.ambiguous_matches.append(
            f"  ⚠️ «{name}» ({lat}, {lon}): у порозі кілька карток "
            f"({', '.join(c[1]['slug'] for c in candidates)}) — узято найближчу {best['slug']}"
        )
    return best["slug"]


def card_claims_alternate(text):
    return bool(_ALT_ROLE_MARKER.search(text))


# ── визначення країни за координатами ────────────────────────────────────────

def guess_country(lat, lon):
    """(код, назва, прапор) або ('?', '?', '') якщо 0 або >1 bbox збіглись —
    постановка дозволяє «приблизно», але не вгадувати навмання."""
    hits = [
        (code, name, flag)
        for code, (lat0, lat1, lon0, lon1, name, flag) in COUNTRY_BBOXES.items()
        if lat0 <= lat <= lat1 and lon0 <= lon <= lon1
    ]
    if len(hits) == 1:
        return hits[0]
    return ("?", "?", "")


def unique_slug(base_slug, taken):
    if base_slug not in taken:
        return base_slug
    i = 2
    while f"{base_slug}-{i}" in taken:
        i += 1
    return f"{base_slug}-{i}"


# ── slug для нових місць: map_slugs → транслітерація, ніколи — сурогат ──────
#
# Реальний випадок: нові точки маршруту мали назву ТІЛЬКИ кирилицею. Без нічого,
# крім re.sub(r"[^a-z0-9]+", ...) на кириличному рядку, лишається порожній
# рядок → slug виду `gr-`, а далі уніфікація дає `gr--2`, `gr--3`… Це
# рівно те, чого не можна допускати: сурогат ВИГЛЯДАЄ як робоча ідентичність
# (id у route.json, посилання, історія використань), а насправді ним не є —
# саме тому таблицю ідентичності для вересня довелось колись складати
# руками. Тому: (1) ручна таблиця map_point_id → slug у
# _migration/slugmap.json (`map_slugs`) — пріоритетна; (2) транслітерація —
# лише запасний варіант; (3) вироджений результат (порожньо/самі дефіси) —
# ПОМИЛКА, скрипт зупиняється, а не вигадує.

_UK_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e", "є": "ie",
    "ж": "zh", "з": "z", "и": "y", "і": "i", "ї": "i", "й": "i", "к": "k", "л": "l",
    "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ю": "iu",
    "я": "ia", "ь": "", "ъ": "", "ы": "y", "э": "e", "ё": "e",
}


def transliterate_uk(text):
    """Кирилиця (укр. алфавіт + типові рос. залишки ы/э/ъ/ё) → латиниця,
    посимвольно. НЕ державний стандарт — лише детермінований запасний
    варіант, який потім проганяється крізь перевірку на виродженість.
    Латинські символи й цифри проходять без змін."""
    return "".join(_UK_TRANSLIT.get(ch, ch) for ch in text.lower())


def build_slug_base(name):
    """Назва → безпечна для slug частина (після коду країни). Порожній
    рядок, якщо в назві не лишилось жодного [a-z0-9] після транслітерації —
    це і є ознака виродженості, яку перевіряє resolve_new_place_slug."""
    ascii_name = transliterate_uk(name or "")
    return re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")


def resolve_new_place_slug(mp_id, name, lat, lon, code, map_slugs, report, context_label):
    """Slug для місця без картки в бібліотеці. Порядок: `map_slugs` (ручна
    таблиця _migration/slugmap.json, ключ — map_point_id, бо назву власник
    може перейменувати будь-коли) → транслітерація назви. Повертає None
    (і записує причину в report.unresolved_slugs), якщо ОБИДВА шляхи дають
    порожняк, — виклик має пропустити цю точку, а не підставити сурогат."""
    manual = map_slugs.get(mp_id)
    if manual:
        return manual

    base = build_slug_base(name)
    if not base:
        report.unresolved_slugs.append(
            f"  ❌ {mp_id}  «{name or '(без назви)'}»  ({lat}, {lon}) — {context_label}: "
            f"не вдалось вивести slug (назва без латиниці, а транслітерація дає "
            f"порожняк). Впиши slug у _migration/slugmap.json → map_slugs[\"{mp_id}\"]."
        )
        return None
    code_for_slug = code.lower() if code != "?" else "xx"
    return f"{code_for_slug}-{base}"


# ── генерація нової картки («базовий» рівень, § б постановки) ───────────────

def render_poi_table(nested_points):
    if not nested_points:
        return None
    lines = ["| Назва | Опис | lat, lon | Фото |", "| :------ | :----- | :--------- | :----- |"]
    for n in nested_points:
        name = md_escape_cell(n.get("name") or "?")
        desc = md_escape_cell(n.get("description") or "")
        lat, lon = n.get("lat"), n.get("lon")
        coord = f"`{lat}, {lon}`" if lat is not None and lon is not None else "`?`"
        photo = f"`{n['photo_url']}`" if n.get("photo_url") else "?"
        lines.append(f"| **{name}** | {desc} | {coord} | {photo} |")
    return "\n".join(lines)


def group_nested_by_category(nested_points, poi_categories):
    """{'poi': [...], '<ключ категорії>': [...], ...} — POI groups за `type`.

    `type == 'poi'` і БУДЬ-ЯКИЙ тип, якого нема серед poi_categories цієї
    поїздки (нова категорія на карті, про яку профіль ще не знає) — в базову
    секцію `poi`. Це те саме правило, з якого 07.08.2026 з'явився дефект:
    картка з усіма POI в одній секції означає, що generate_state.py бачить
    для них усіх лише `type: poi`, а карта під ними могла мати
    viewpoint/icecream/craft/local — і план публікації тоді хоче тихцем
    звести чужу категоризацію до загальної зірочки."""
    buckets = {}
    for n in (nested_points or []):
        t = n.get("type")
        key = t if (t and t != "poi" and t in poi_categories) else "poi"
        buckets.setdefault(key, []).append(n)
    return buckets


def render_category_section(type_key, items, poi_categories):
    """(заголовок без '## ', таблиця) для однієї секції, або None якщо
    порожньо. Заголовок — `<icon> <section>` з profile poi_categories
    (PLACE_SPEC.md §3: «Картка додає секцію ## <icon> <section> з тим самим
    текстом»), НІКОЛИ не зашитий тут; для базової 'poi' — фіксована
    BASE_POI_SECTION, як і в generate_state.py."""
    table = render_poi_table(items)
    if not table:
        return None
    if type_key == "poi":
        return BASE_POI_SECTION, table
    cfg = poi_categories.get(type_key) or {}
    section = cfg.get("section") or type_key
    icon = cfg.get("icon") or ""
    heading = f"{icon} {section}".strip()
    return heading, table


def build_new_card_md(slug, title, lat, lon, code, country_name, flag, popup_html,
                       nested_points, extra_info, import_date_ukr, poi_categories):
    desc = extract_core_description(popup_html)
    if not desc:
        desc = "_(На карті ще немає опису — лише назва і координати.)_"

    lines = [f"# {title}", "", "## Метадані", "", "| | |", "|:--|:--|",
              f"| **ID** | `{slug}` |", "| **Глибина** | базовий |"]
    if code != "?":
        lines.append(f"| **Країна** | {country_name} {flag} |")
        lines.append(f"| **Код** | {code} |")
    else:
        lines.append("| **Країна** | `?` — визначити руками |")
    lines.append(f"| **Координати** | {lat}, {lon} |")
    lines.append(f"| **Google Maps** | [відкрити](https://www.google.com/maps?q={lat},{lon}) |")
    lines.append("")
    lines.append(f"<!-- Імпортовано з карти {import_date_ukr}. Проза не розколота, картку не вичитано. -->")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Опис")
    lines.append("")
    lines.append(desc)

    buckets = group_nested_by_category(nested_points, poi_categories)
    order = list(_CATEGORY_ORDER) + [t for t in buckets if t not in _CATEGORY_ORDER]
    for type_key in order:
        items = buckets.get(type_key)
        if not items:
            continue
        section = render_category_section(type_key, items, poi_categories)
        if not section:
            continue
        heading, table = section
        lines += ["", "---", "", f"## {heading}", "", table]

    # В'їзд на авто / Паркування / Де зупинитись — ті самі секції, які
    # generate_state.py:build_extra_info() читає назад (get_section за
    # точною назвою). Без них картка не втратить лише категорію POI, а й
    # усе практичне: план публікації тоді хоче стерти чужий extra_info
    # порожнім масивом (той самий клас втрати, що й POI, знайдено тим самим
    # прогоном plan_push.py).
    for label, text in split_extra_info_sections(extra_info).items():
        if text:
            lines += ["", "---", "", f"## {label}", "", text]

    return "\n".join(lines) + "\n"


def synthesize_alt_note(name, popup_html, import_date_ukr):
    desc = extract_core_description(popup_html)
    if desc:
        gist = first_sentence(desc)
        return f"⚙️ Імпортовано з карти {import_date_ukr}: {gist} Проза не розколота, перевірити руками."
    return (f"⚙️ Імпортовано з карти {import_date_ukr}: маркер «{name}» без опису "
            f"(лише назва і координати). Перевірити руками.")


# ── звіт ─────────────────────────────────────────────────────────────────────

class Report:
    def __init__(self):
        self.added_points = []
        self.removed_points = []
        self.moved_points = []       # (slug, old_day, new_day)
        self.changed_fields = []     # (slug, day, {field: (old, new)})
        self.new_cards = []          # (slug, title, lat, lon, code, source)
        self.ambiguous_matches = []
        self.deferred_may = []       # (name, lat, lon)
        self.alt_added = []
        self.alt_removed = []
        self.alt_kept = []
        self.divergences = []
        self.unresolved_slugs = []   # рядки-помилки: не вдалось вивести slug — ЗУПИНКА
        self.regenerated_cards = []  # (slug, title, source) — машинні картки, перегенеровані наново


# ── основна логіка (а): route.json ───────────────────────────────────────────

POINT_TRACKED_FIELDS = ("kind", "date", "nights", "arrive_at", "stay_minutes", "map_point_id")
# Власні поля, яких на карті немає — переносяться незмінними (правило §, "не чіпай").
OWN_FIELDS = ("booking_note", "label", "lat", "lon")


def maybe_queue_regeneration(slug, lib_text_by_slug, queued_regen_slugs, title, lat, lon,
                              popup_html, nested_points, extra_info, source_label,
                              cards_to_regenerate, report):
    """Якщо картка `slug` — машинного походження (маркер «Імпортовано з
    карти», is_machine_imported), перегенеровуємо її зі СВІЖОГО стану точки
    на карті (§ в постановки). Картку, вичитану людиною (маркера нема), НЕ
    чіпаємо, навіть якщо на карті щось змінилось відтоді — check_role_divergence
    про це попереджає окремо, а не тут."""
    if slug in queued_regen_slugs:
        return
    text = lib_text_by_slug.get(slug)
    if text is None or not is_machine_imported(text):
        return
    queued_regen_slugs.add(slug)
    code, country_name, flag = guess_country(lat, lon)
    cards_to_regenerate.append({
        "slug": slug, "title": title, "lat": lat, "lon": lon,
        "code": code, "country_name": country_name, "flag": flag,
        "popup_html": popup_html, "nested_points": nested_points or [],
        "extra_info": extra_info or [],
        "source": source_label,
    })
    report.regenerated_cards.append((slug, title, source_label))


def index_old_points(old_route_json):
    """map_point_id → (slug, day_number, point_dict) для кожної точки старого route.json."""
    idx = {}
    for day in old_route_json["days"]:
        for p in day["points"]:
            mpid = p.get("map_point_id")
            if mpid:
                idx[mpid] = (p["id"], day["day"], p)
    return idx


def resolve_point_slug(mp, old_by_mpid, library_index, taken_slugs, map_slugs, lib_text_by_slug,
                        queued_regen_slugs, report, new_cards_pending, cards_to_regenerate):
    """Slug для точки карти mp. Порядок (за постановкою): наявна картка за
    map_point_id → картка за назвою й координатами → map_slugs →
    автоматичний вивід (транслітерація). Повертає (None, None, "unresolved"),
    якщо навіть map_slugs і транслітерація не дають нічого — виклик має
    ПРОПУСТИТИ точку, помилка вже записана в report.unresolved_slugs."""
    mpid = mp["id"]
    if mpid in old_by_mpid:
        old_slug, _, old_point = old_by_mpid[mpid]
        maybe_queue_regeneration(
            old_slug, lib_text_by_slug, queued_regen_slugs, mp.get("label") or old_slug,
            mp.get("lat"), mp.get("lon"), mp.get("popup_html"), mp.get("nested_points"),
            mp.get("extra_info"),
            f"точка маршруту {mp.get('date')} (перегенеровано)", cards_to_regenerate, report,
        )
        return old_slug, old_point, "existing"

    lib_slug = find_library_match(mp.get("lat"), mp.get("lon"), mp.get("label") or "", library_index, report)
    if lib_slug:
        maybe_queue_regeneration(
            lib_slug, lib_text_by_slug, queued_regen_slugs, mp.get("label") or lib_slug,
            mp.get("lat"), mp.get("lon"), mp.get("popup_html"), mp.get("nested_points"),
            mp.get("extra_info"),
            f"точка маршруту {mp.get('date')} (перегенеровано)", cards_to_regenerate, report,
        )
        return lib_slug, None, "library"

    # Нове місце без картки — генеруємо (§ б).
    code, country_name, flag = guess_country(mp.get("lat"), mp.get("lon"))
    base_slug = resolve_new_place_slug(
        mpid, mp.get("label"), mp.get("lat"), mp.get("lon"), code, map_slugs, report,
        f"точка маршруту {mp.get('date')}",
    )
    if base_slug is None:
        return None, None, "unresolved"
    slug = unique_slug(base_slug, taken_slugs)
    taken_slugs.add(slug)
    new_cards_pending.append({
        "slug": slug, "title": mp.get("label") or mpid,
        "lat": mp.get("lat"), "lon": mp.get("lon"),
        "code": code, "country_name": country_name, "flag": flag,
        "popup_html": mp.get("popup_html"), "nested_points": mp.get("nested_points") or [],
        "extra_info": mp.get("extra_info") or [],
        "source": f"точка маршруту «{mp.get('label')}»",
    })
    report.new_cards.append((slug, mp.get("label") or mpid, mp.get("lat"), mp.get("lon"), code,
                              f"точка маршруту {mp.get('date')}"))
    return slug, None, "new"


def build_new_days(mirror_points, old_route_json, old_by_mpid, library_index, taken_slugs,
                    map_slugs, lib_text_by_slug, queued_regen_slugs, report, new_cards_pending,
                    cards_to_regenerate):
    """Дні: скелет (номер+дата) береться зі СТАРОГО route.json (щоб не
    загубити дні без жодної НОВОЇ точки — напр. вільний день на Ітаці, який
    ніколи не зʼявляється як окрема точка ні в мапі, ні в route.json), точки
    заново розкладені по датах зі свіжого дзеркала."""
    day_by_date = {d["date"]: {"day": d["day"], "date": d["date"], "points": []}
                   for d in old_route_json["days"]}

    unmatched_dates = []
    for mp in mirror_points:
        date = mp.get("date")
        if date not in day_by_date:
            unmatched_dates.append(mp)
            continue
        slug, old_point, origin = resolve_point_slug(
            mp, old_by_mpid, library_index, taken_slugs, map_slugs, lib_text_by_slug,
            queued_regen_slugs, report, new_cards_pending, cards_to_regenerate,
        )
        if slug is None:
            continue  # unresolved — помилка вже в report.unresolved_slugs, main() зупинить прогін
        point = {"id": slug, "map_point_id": mp["id"], "kind": mp.get("kind")}
        if mp.get("nights") and mp["nights"] > 1:
            point["nights"] = mp["nights"]
        if mp.get("arrive_at") is not None:
            point["arrive_at"] = mp["arrive_at"]
        if mp.get("stay_minutes") is not None:
            point["stay_minutes"] = mp["stay_minutes"]
        if old_point:
            for f in OWN_FIELDS:
                if f in old_point:
                    point[f] = old_point[f]
        day_by_date[date]["points"].append(point)

    if unmatched_dates:
        for mp in unmatched_dates:
            report.added_points.append(
                f"  ⚠️ точка «{mp.get('label')}» ({mp.get('date')}) — дати немає серед днів "
                f"поточного route.json, точку НЕ додано; потрібне ручне розширення днів"
            )

    ordered_days = [day_by_date[d["date"]] for d in old_route_json["days"]]
    return ordered_days


def diff_days(old_days, new_days, report):
    old_by_slug = {}
    for d in old_days:
        for p in d["points"]:
            old_by_slug[p["id"]] = (d["day"], p)
    new_by_slug = {}
    for d in new_days:
        for p in d["points"]:
            new_by_slug[p["id"]] = (d["day"], p)

    for slug, (day, p) in new_by_slug.items():
        if slug not in old_by_slug:
            report.added_points.append(f"  + {slug} → день {day} ({p.get('kind')}, {p.get('map_point_id')})")
    for slug, (day, p) in old_by_slug.items():
        if slug not in new_by_slug:
            report.removed_points.append(f"  − {slug} (був день {day}) — на карті вже немає")

    for slug in sorted(set(old_by_slug) & set(new_by_slug)):
        old_day, old_p = old_by_slug[slug]
        new_day, new_p = new_by_slug[slug]
        if old_day != new_day:
            report.moved_points.append(f"  ↕ {slug}: день {old_day} → {new_day}")
        changed = {}
        for f in POINT_TRACKED_FIELDS:
            if old_p.get(f) != new_p.get(f):
                changed[f] = (old_p.get(f), new_p.get(f))
        if changed:
            report.changed_fields.append((slug, new_day, changed))


# ── основна логіка (б)+(а): alternates ───────────────────────────────────────

def process_alternates(alt_points, old_alternates, library_index, taken_slugs, map_slugs,
                        lib_text_by_slug, queued_regen_slugs, report,
                        new_cards_pending, cards_to_regenerate, slugmap_may_count, import_date_ukr):
    """Повертає новий список alternates route.json: [{id, note}, ...].

    Порядок має значення: старий route.json alternates — ручний, курований
    список (порядок появи в старому файлі), а не машинний dict. Тому
    існуючі id зберігають СВІЙ порядок і свою нотатку незмінними; нові
    id (з нових карток) дописуються в кінець, у порядку зустрічі на карті.
    Інакше застосування тихцем перемішало б курований список під порядок
    `alt.points` (той навмисно сортується за opaque map id у pull_map.py —
    зручно для diff'у дзеркала, шкідливо для читання route.json)."""
    old_note_by_slug = {a["id"]: a["note"] for a in old_alternates}
    old_order = [a["id"] for a in old_alternates]

    resolved_notes = {}   # slug → нотатка, для ВСІХ, хто лишається альтернативою
    new_order = []        # slug-и, яких не було в старому route.json, у порядку зустрічі на карті

    for ap in alt_points:
        name = ap.get("name") or ap["id"]
        lat, lon = ap.get("lat"), ap.get("lon")

        lib_slug = find_library_match(lat, lon, name, library_index, report)
        if lib_slug:
            maybe_queue_regeneration(
                lib_slug, lib_text_by_slug, queued_regen_slugs, name, lat, lon,
                ap.get("popup_html"), ap.get("nested_points"), ap.get("extra_info"),
                f"альтернатива «{name}» (перегенеровано)", cards_to_regenerate, report,
            )
            if lib_slug in old_note_by_slug:
                resolved_notes[lib_slug] = old_note_by_slug[lib_slug]
            else:
                resolved_notes[lib_slug] = synthesize_alt_note(name, ap.get("popup_html"), import_date_ukr)
                new_order.append(lib_slug)
                report.alt_added.append(f"  + {lib_slug} («{name}») — картка вже є, нотатку згенеровано")
            continue

        comments_text = " ".join(c.get("text", "") for c in (ap.get("comments") or []))
        if _MAY_CANDIDATE_MARKER.search(comments_text):
            report.deferred_may.append(f"  ⏭ {name} ({lat}, {lon}) — травневий кандидат (Ф6), не чіпаємо")
            continue

        # Справді нове місце — план створення картки (§ б).
        code, country_name, flag = guess_country(lat, lon)
        base_slug = resolve_new_place_slug(
            ap.get("id"), name, lat, lon, code, map_slugs, report, "альтернатива",
        )
        if base_slug is None:
            continue  # unresolved — помилка вже в report.unresolved_slugs
        slug = unique_slug(base_slug, taken_slugs)
        taken_slugs.add(slug)
        new_cards_pending.append({
            "slug": slug, "title": name, "lat": lat, "lon": lon,
            "code": code, "country_name": country_name, "flag": flag,
            "popup_html": ap.get("popup_html"), "nested_points": ap.get("nested_points") or [],
            "extra_info": ap.get("extra_info") or [],
            "source": f"альтернатива «{name}»",
        })
        report.new_cards.append((slug, name, lat, lon, code, "альтернатива"))
        resolved_notes[slug] = synthesize_alt_note(name, ap.get("popup_html"), import_date_ukr)
        new_order.append(slug)
        report.alt_added.append(f"  + {slug} («{name}») — нова картка")

    new_alternates = []
    for slug in old_order:
        if slug in resolved_notes:
            new_alternates.append({"id": slug, "note": resolved_notes[slug]})
            report.alt_kept.append(f"  = {slug} — нотатку й місце в списку збережено")
        else:
            report.alt_removed.append(f"  − {slug} — на карті серед альтернатив вже немає "
                                       f"(промоутнуто в маршрут або прибрано)")
    for slug in new_order:
        new_alternates.append({"id": slug, "note": resolved_notes[slug]})

    if len(report.deferred_may) != slugmap_may_count:
        report.ambiguous_matches.append(
            f"  ⚠️ травневих кандидатів (маркер «з травневої карти») знайдено "
            f"{len(report.deferred_may)}, а в slugmap.json candidates_2026_05 — {slugmap_may_count}. "
            f"Перевір руками — можливо, зʼявився новий кандидат без маркера "
            f"або старий маркер зник."
        )

    return new_alternates


# ── (в): розбіжності картка ↔ карта ──────────────────────────────────────────

def check_role_divergence(new_days, library_index, report):
    """Картки, що стверджують «альтернатива, не в маршруті», але тепер є
    точкою маршруту. Правило (в): показати, не виправляти."""
    route_slugs = {p["id"] for d in new_days for p in d["points"]}
    by_slug = {c["slug"]: c for c in library_index}
    for slug in sorted(route_slugs):
        card = by_slug.get(slug)
        if card and card_claims_alternate(card["text"]):
            report.divergences.append(
                f"  ⚠️ {card['path'].name}: картка досі стверджує «альтернативна точка / "
                f"не в маршруті», але тепер вона в route.json як точка маршруту. "
                f"Картку НЕ чіпав — правка руками."
            )


# ── друк звіту ────────────────────────────────────────────────────────────────

def print_report(report, apply_mode, new_days, old_route_json, new_alternates):
    print("=" * 70)
    print("СИНХРОНІЗАЦІЯ КАРТА → route.json" + ("  [ЗАСТОСОВАНО]" if apply_mode else "  [ПОКАЗ, --apply не передано]"))
    print("=" * 70)

    old_n = sum(len(d["points"]) for d in old_route_json["days"])
    new_n = sum(len(d["points"]) for d in new_days)
    print(f"\nТочок маршруту: було {old_n} → стане {new_n}")

    print("\n-- Додані точки --")
    print("\n".join(report.added_points) if report.added_points else "  (нема)")
    print("\n-- Прибрані точки (на карті вже немає) --")
    print("\n".join(report.removed_points) if report.removed_points else "  (нема)")
    print("\n-- Переміщені між днями --")
    print("\n".join(report.moved_points) if report.moved_points else "  (нема)")
    print("\n-- Змінені поля --")
    if report.changed_fields:
        for slug, day, changed in report.changed_fields:
            bits = ", ".join(f"{f}: {o!r}→{n!r}" for f, (o, n) in changed.items())
            print(f"  {slug} (день {day}): {bits}")
    else:
        print("  (нема)")

    print(f"\n-- Alternates: було {len(old_route_json['alternates'])} → стане {len(new_alternates)} --")
    print("\n-- alternates: додано --")
    print("\n".join(report.alt_added) if report.alt_added else "  (нема)")
    print("\n-- alternates: прибрано --")
    print("\n".join(report.alt_removed) if report.alt_removed else "  (нема)")
    print("\n-- alternates: збережено як є --")
    print("\n".join(report.alt_kept) if report.alt_kept else "  (нема)")

    print(f"\n-- План створення карток: {len(report.new_cards)} --")
    if report.new_cards:
        for slug, title, lat, lon, code, source in report.new_cards:
            print(f"  {slug}  «{title}»  ({lat}, {lon})  країна={code}  ← {source}")
    else:
        print("  (нема)")

    print(f"\n-- Картки на перегенерацію (машинний імпорт, § в): {len(report.regenerated_cards)} --")
    if report.regenerated_cards:
        for slug, title, source in report.regenerated_cards:
            print(f"  {slug}  «{title}»  ← {source}")
    else:
        print("  (нема)")

    print(f"\n-- Травневі кандидати (Ф6) — поза обсягом, картку не чіпаємо: {len(report.deferred_may)} --")
    print("\n".join(report.deferred_may) if report.deferred_may else "  (нема)")

    print("\n-- Неоднозначні збіги координат --")
    print("\n".join(report.ambiguous_matches) if report.ambiguous_matches else "  (нема)")

    print("\n" + "=" * 70)
    print("РОЗБІЖНОСТІ КАРТКА ↔ КАРТА (на розгляд людини, нічого не застосовано)")
    print("=" * 70)
    print("\n".join(report.divergences) if report.divergences else "  (нема)")
    print()


# ── main ──────────────────────────────────────────────────────────────────────

def check_freshness(mirror):
    pulled_at = mirror.get("pulled_at")
    if not pulled_at:
        return
    try:
        ts = datetime.datetime.fromisoformat(pulled_at)
    except ValueError:
        return
    now = datetime.datetime.now(ts.tzinfo) if ts.tzinfo else datetime.datetime.now()
    age = now - ts
    if age > datetime.timedelta(hours=1):
        print(f"⚠️  live_mirror.json старший за годину ({pulled_at}, {age}) — "
              f"онови: python3 scripts/pull_map.py", file=sys.stderr)


def load_slugmap():
    slugmap_path = CTX.data_root / "_migration" / "slugmap.json"
    if not slugmap_path.exists():
        return {}
    return CTX.read_json(slugmap_path)


def load_slugmap_may_count(slugmap):
    return len(slugmap.get("candidates_2026_05", {}))


def load_map_slugs(slugmap):
    """map_point_id → slug, ручна таблиця ідентичності для точок і
    альтернатив без латинської назви на карті (_migration/slugmap.json,
    секція `map_slugs`). Ключ — id, не назва: власник може перейменувати
    точку на карті будь-коли, id лишається."""
    return slugmap.get("map_slugs", {})


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true", help="записати зміни (дефолт — тільки показати)")
    # --data/--trip розбирає сам trip_ctx.paths() (з повного sys.argv, до parse_args) —
    # parse_known_args лише не дає argparse впасти на них тут.
    args, _unknown = p.parse_known_args()
    return args


def main():
    args = parse_args()

    mirror = CTX.read_json(CTX.live_mirror)
    check_freshness(mirror)
    import_date_ukr = iso_to_ukr_date((mirror.get("pulled_at") or "")[:10])

    old_route_json = CTX.read_json(CTX.route_json)
    library_index = load_library_index(CTX.places_dir)
    taken_slugs = {c["slug"] for c in library_index}
    lib_text_by_slug = {c["slug"]: c["text"] for c in library_index}
    slugmap = load_slugmap()
    map_slugs = load_map_slugs(slugmap)
    # trip.json → poi_categories: те саме джерело, яким потім читає
    # generate_state.py. Секції генерованих карток мають з ним звірятись,
    # інакше POI, які на карті вже мають категорію (viewpoint/icecream/
    # craft/local), при round-trip назад через одну секцію "Цікаві POI"
    # перетворюються на голий "poi" — і план публікації хоче тихцем
    # переписати чужу категоризацію (07.08.2026, 58 зайвих update_poi).
    poi_categories = CTX.trip.get("poi_categories", {})

    report = Report()
    old_by_mpid = index_old_points(old_route_json)
    new_cards_pending = []
    cards_to_regenerate = []
    queued_regen_slugs = set()

    new_days = build_new_days(
        mirror["route"]["points"], old_route_json, old_by_mpid, library_index,
        taken_slugs, map_slugs, lib_text_by_slug, queued_regen_slugs, report,
        new_cards_pending, cards_to_regenerate,
    )
    diff_days(old_route_json["days"], new_days, report)
    check_role_divergence(new_days, library_index, report)

    may_count = load_slugmap_may_count(slugmap)
    new_alternates = process_alternates(
        mirror["alt"]["points"], old_route_json["alternates"], library_index, taken_slugs,
        map_slugs, lib_text_by_slug, queued_regen_slugs, report,
        new_cards_pending, cards_to_regenerate, may_count, import_date_ukr,
    )

    # ЗУПИНКА, не сурогат: якщо хоч для одного нового місця не вдалось
    # вивести slug (нема ні в map_slugs, ні в транслітерації) — далі не
    # рахуємо й нічого не друкуємо з решти звіту. Половинчастий diff
    # (частина нових точок пропущена мовчки) оманливіший за чесну відмову.
    if report.unresolved_slugs:
        print("=" * 70, file=sys.stderr)
        print("ЗУПИНКА: не вдалось вивести slug для нових місць", file=sys.stderr)
        print("=" * 70, file=sys.stderr)
        print(
            "Мовчазна генерація сурогату (gr-, gr--2, …) гірша за відмову — "
            "нічого не пораховано й не записано. Впиши бракуючі id в "
            "_migration/slugmap.json → map_slugs і повтори:\n",
            file=sys.stderr,
        )
        print("\n".join(report.unresolved_slugs), file=sys.stderr)
        sys.exit(1)

    new_route_json = {
        "_": old_route_json.get("_", ""),
        "trip": old_route_json["trip"],
        "days": new_days,
        "alternates": new_alternates,
    }

    print_report(report, args.apply, new_days, old_route_json, new_alternates)

    if not args.apply:
        print("[ПОКАЗ] нічого не записано. Повтори з --apply, щоб застосувати.")
        return

    for spec in new_cards_pending:
        card_md = build_new_card_md(
            spec["slug"], spec["title"], spec["lat"], spec["lon"], spec["code"],
            spec["country_name"], spec["flag"], spec["popup_html"], spec["nested_points"],
            spec["extra_info"], import_date_ukr, poi_categories,
        )
        dest = CTX.places_dir / f"{spec['slug']}.md"
        dest.write_text(card_md, encoding="utf-8")
        print(f"→ записано (нова) {dest}")

    # Перегенерація — лише картки з маркером «Імпортовано з карти»
    # (is_machine_imported), зібрані в cards_to_regenerate за тим самим
    # правилом round-trip: build_new_card_md знову ставить той самий
    # маркер, тому картка лишається придатною до перегенерації, доки
    # людина не вичитає її й не забере позначку (§ в постановки).
    for spec in cards_to_regenerate:
        card_md = build_new_card_md(
            spec["slug"], spec["title"], spec["lat"], spec["lon"], spec["code"],
            spec["country_name"], spec["flag"], spec["popup_html"], spec["nested_points"],
            spec["extra_info"], import_date_ukr, poi_categories,
        )
        dest = CTX.places_dir / f"{spec['slug']}.md"
        dest.write_text(card_md, encoding="utf-8")
        print(f"→ записано (перегенеровано) {dest}")

    CTX.write_json(CTX.route_json, new_route_json)
    print(f"→ записано {CTX.route_json}")


if __name__ == "__main__":
    main()
