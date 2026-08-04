#!/usr/bin/env python3
"""
scripts/migrate_cards.py

Разовий міграційний скрипт (Ф3, STRUCTURE_PROPOSAL.md §5.1–5.2).

Перекладає картки місць зі старого репозиторію travel202609 у нову бібліотеку
travel-data/places/ і будує travel-data/trips/<профіль>/route.json.

Джерело (travel202609) ТІЛЬКИ ЧИТАЄТЬСЯ — жодного запису.

Обсяг цього запуску: секції slugmap `route` (31 файл) і `candidates_2026_09`
(7 файлів) → 35 карток бібліотеки (38 файлів мінус 3 злиття, оголошені в
slugmap.merge). Секція `candidates_2026_05` (14 файлів 8xxx) — це Ф6, не
чіпається.

Що робить:
  1. Читає slugmap.json (₴ГОЛОВНЕ джерело ідентичності — slug звідти, не з
     заголовків карток).
  2. Для кожного цільового slug — переносить картку (§1 нижче), при потребі
     зливаючи 2 файли в одну (§2), і виносить трип-специфічну прозу з
     ## Опис/## Нотатки в overlay/<slug>.md (§3).
  3. Копіює places/country_info.md і TRAVEL_PRACTICAL_INFO.md без змін.
  4. Будує route.json: порядок днів — з INDEX.md, метадані точок (kind/date/
     nights/arrive_at/stay_minutes) — з exports/route_state.json СЛОВО В
     СЛОВО (нічого не перераховується).
  5. Друкує звіт міграції.

Ідемпотентність: скрипт завжди перебудовує вихід заново з джерела +
slugmap.json (+ опційно --live-mirror), без інкрементального стану — тому
повторний запуск з тими самими аргументами дає той самий результат.

Usage:
    python3 scripts/migrate_cards.py --source ~/GitHub/travel202609
    python3 scripts/migrate_cards.py --source ~/GitHub/travel202609 --live-mirror <шлях>
    python3 scripts/migrate_cards.py --source ~/GitHub/travel202609 --dry-run
"""
import argparse
import json
import re
import sys
from datetime import date as _date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trip_ctx import paths  # noqa: E402

CTX = paths()


# ── метадані: що видаляємо з картки бібліотеки (§1 постановки) ──────────────

METADATA_DROP_EXACT = {"Тип", "Тайминг", "Тайминг (старт)", "Тайминг (фініш)", "Бронь", "Час перебування"}


def is_dropped_metadata(label):
    if label in METADATA_DROP_EXACT:
        return True
    if label.startswith("Дата"):  # "Дата (2026)", "Дата" — будь-який варіант
        return True
    return False


# ── розпізнавання трип-специфічної прози (§3 постановки) ────────────────────

_D_MARKER_RE = re.compile(r"\bD\d+\b")
_DAY_WORD_RE = re.compile(r"\bДень\s*\d+\b")
_DATE_RE = re.compile(r"\b(\d{2})\.(\d{2})\b")
_WEEKDAY_RE = re.compile(r"\b(пн|вт|ср|чт|пт|сб|нд)\.")
_ARRIVE_DEPART_RE = re.compile(r"(приїзд|виїзд|старт)[^.\n]{0,25}\d{1,2}:\d{2}")
_TRIP_PHRASES = ["цього разу", "тут ми стоїмо", "дві ночі", "наступного дня", "пів дня в запасі"]
# \b межі слів — щоб "дві ночі" не спрацювало підрядком у "обидві ночі".
_TRIP_PHRASE_RES = [re.compile(r"\b" + re.escape(p) + r"\b") for p in _TRIP_PHRASES]

# Дати цієї поїздки — 13–29.09.2026. ДД.ММ поза цим вікном (напр. "01.07–31.10"
# — сезон роботи тунелю Трансфагараш, "30.06.2026" — дата завершення ремонту
# мосту) — це ПОСТІЙНИЙ факт з числом у форматі дати, а не день конкретної
# поїздки, і трип-специфічним НЕ рахується (вичитка 2: "ознака — прив'язка до
# дати/дня цієї поїздки, а не наявність числа").
_TRIP_DATE_START = _date(2026, 9, 13)
_TRIP_DATE_END = _date(2026, 9, 29)


def _has_trip_date(text):
    for m in _DATE_RE.finditer(text):
        dd, mm = m.groups()
        try:
            d = _date(2026, int(mm), int(dd))
        except ValueError:
            continue
        if _TRIP_DATE_START <= d <= _TRIP_DATE_END:
            return True
    return False


def is_trip_specific(text):
    """Речення/пункт трип-специфічний, якщо містить D-маркер, дату ДД.ММ у
    межах вікна цієї поїздки (13–29.09.2026), коротку назву дня тижня, час
    прибуття/виїзду або одну з ключових фраз (§3 постановки). У сумнівних
    випадках — False (лишити в бібліотеці)."""
    if _D_MARKER_RE.search(text):
        return True
    if _DAY_WORD_RE.search(text):
        return True
    if _has_trip_date(text):
        return True
    if _WEEKDAY_RE.search(text):
        return True
    if _ARRIVE_DEPART_RE.search(text):
        return True
    low = text.lower()
    if any(r.search(low) for r in _TRIP_PHRASE_RES):
        return True
    return False


# ── розбиття тексту на речення (обережно з абревіатурами "ст.", "р.") ───────

_ABBR = {"ст", "рр", "р", "вул", "км", "обл", "кв", "тис", "млн", "див", "напр", "прим", "ім", "гр", "св"}


def split_sentences(text):
    """Розбиває абзац на речення по '.', '!', '?' — але НЕ розриває після
    відомих скорочень (ст., р., вул. тощо), бо в корпусі вони трапляються
    мідречення ('XII ст. на пагорбі', '1580 р. на пагорбі')."""
    sentences = []
    start = 0
    for m in re.finditer(r"[.!?]+(\s+|$)", text):
        punct_start = m.start()
        end = m.end()
        wm = re.search(r"([A-Za-zА-Яа-яІЇЄҐіїєґ']+)\s*$", text[:punct_start])
        word = wm.group(1).lower() if wm else ""
        if word in _ABBR:
            continue  # не межа речення — абревіатура
        sentences.append(text[start:end].strip())
        start = end
    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    return [s for s in sentences if s]


def split_opis(text):
    """## Опис → (текст для бібліотеки, [винесені речення]).

    Абзаци (розділені порожнім рядком) не рвуться між собою; усередині
    кожного абзацу трип-специфічні речення виносяться, решта склеюється
    назад пробілом."""
    paragraphs = re.split(r"\n\s*\n", text.strip())
    kept_paragraphs = []
    overlay_sentences = []
    for para in paragraphs:
        if not para.strip():
            continue
        kept = []
        for s in split_sentences(para):
            if is_trip_specific(s):
                overlay_sentences.append(s)
            else:
                kept.append(s)
        if kept:
            kept_paragraphs.append(" ".join(kept))
    return "\n\n".join(kept_paragraphs), overlay_sentences


def _split_bullet_block(block):
    """Ріже блок (без порожніх рядків усередині) на одиниці по маркеру '- '
    у нульовому відступі. Вкладені/продовжувальні рядки (відступ > 0 або
    текст без маркера) приєднуються до одиниці, що будується; текст ПЕРЕД
    першим маркером — до першої одиниці. Якщо '- ' у нульовому відступі
    немає взагалі — блок лишається одним цілим (непорушним)."""
    lines = block.splitlines()
    if not any(re.match(r"^- ", ln) for ln in lines):
        return [block]
    units = []
    current = []
    current_has_bullet = False
    for line in lines:
        if re.match(r"^- ", line):
            if current_has_bullet:
                units.append("\n".join(current))
                current = [line]
            else:
                current.append(line)
            current_has_bullet = True
        else:
            current.append(line)
    if current:
        units.append("\n".join(current))
    return units


def split_units(text):
    """Загальний розкол секції зі списками/змішаним вмістом (## Нотатки,
    ## Практичне, ## Де зупинитись) → (текст для бібліотеки, [винесені
    одиниці]).

    Правило безпеки з постановки: спершу склеюємо ЛОГІЧНІ ОДИНИЦІ —
    пункт списку ('- …' разом з усіма наступними рядками, що не починаються
    з нового '- '/'#'/порожнім рядком) або цілий абзац (блок до порожнього
    рядка, якщо в ньому взагалі нема пунктів '- ' у нульовому відступі) —
    і лише ПОТІМ вирішуємо, трип-специфічна вона чи ні. Переноситься ЦІЛА
    одиниця або жодна — рядок-хвіст сам по собі ніколи не рухається.

    Блоки (абзаци/під-списки), розділені порожнім рядком у джерелі,
    лишаються розділеними порожнім рядком і в тому, що повертається для
    бібліотеки — інакше сусідні, але змістовно різні шматки (напр. вступний
    абзац і подальший '### Підзаголовок') злипаються в один суцільний текст
    без явної межі."""
    blocks = re.split(r"\n\s*\n", text.strip())
    kept_blocks = []
    overlay_units = []
    for block in blocks:
        if not block.strip():
            continue
        block_kept_units = []
        for unit in _split_bullet_block(block):
            if is_trip_specific(unit):
                overlay_units.append(unit.rstrip())
            else:
                if unit.strip():
                    block_kept_units.append(unit.rstrip())
        if block_kept_units:
            kept_blocks.append("\n".join(block_kept_units))
    # Пункти одного вихідного блоку зшиваємо назад по рядку (як у джерелі,
    # без порожнього рядка між ними); різні блоки розділяємо порожнім
    # рядком, щоб не зліплювати структурно різні частини секції.
    return "\n\n".join(kept_blocks), overlay_units


# ── допоміжне: прибрати markdown-розмітку для note у alternates ─────────────

def strip_md_inline(text):
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    t = re.sub(r"\*(.+?)\*", r"\1", t)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# ── переписування посилань на старі 4-значні імена файлів ───────────────────

_OLD_LINK_RE = re.compile(r"\]\((\d{4}\.md)\)")


def build_fname_slug_index(slugmap):
    """Повне зіставлення fname → slug з УСІХ іменованих секцій slugmap.json
    (route, candidates_2026_09, candidates_2026_05 — якщо є), а не лише тих,
    що мігруються в цьому запуску. Так посилання на ще не перенесені травневі
    картки (Ф6) стають 'коректно неробочими' на цільовий slug — загояться
    самі, коли ці картки з'являться — а не лишаються навіки на стару
    нумерацію травневої карти, якої в цьому репо взагалі нема."""
    idx = {}
    for key in ("route", "candidates_2026_09", "candidates_2026_05"):
        idx.update(slugmap.get(key, {}))
    return idx


def rewrite_links(text, fname_slug_index, target_template, context_label, report):
    """Замінює '](XXXX.md)' на '](<ціль за target_template>)' скрізь у
    тексті. Текст посилання (у квадратних дужках) НЕ чіпається. Ціль без
    запису в slugmap.json — не вгадуємо, лишаємо як є й фіксуємо у звіті."""

    def repl(m):
        fname = m.group(1)
        target_slug = fname_slug_index.get(fname)
        if target_slug is None:
            report.unresolved_links.append(
                f"  ⚠️ {context_label}: посилання на {fname} — цілі нема в slugmap.json, лишено як є"
            )
            return m.group(0)
        new_target = target_template.format(slug=target_slug)
        report.rewritten_links.append(f"  {context_label}: {fname} → {new_target}")
        return f"]({new_target})"

    return _OLD_LINK_RE.sub(repl, text)


# ── парсинг картки джерела ───────────────────────────────────────────────────

def parse_card(text):
    """Розбирає MD-картку джерела: (title, metadata_rows, sections).

    metadata_rows — [(label, value), ...] у порядку появи в таблиці.
    sections — {header: body, ...} у порядку появи, БЕЗ 'Метадані'; body —
    без обрамляючих '---'-роздільників."""
    title_m = re.match(r"^# (.+)$", text, re.MULTILINE)
    title = title_m.group(1).strip() if title_m else ""

    header_re = re.compile(r"^## (.+)$", re.MULTILINE)
    headers = list(header_re.finditer(text))
    all_sections = {}
    for idx, hm in enumerate(headers):
        header = hm.group(1).strip()
        body_start = hm.end()
        body_end = headers[idx + 1].start() if idx + 1 < len(headers) else len(text)
        body = text[body_start:body_end]
        body = re.sub(r"^\s*\n", "", body)
        body = re.sub(r"\n-{3,}\s*$", "", body)
        all_sections[header] = body.strip("\n")

    metadata_rows = []
    if "Метадані" in all_sections:
        for line in all_sections["Метадані"].splitlines():
            line = line.strip()
            rm = re.match(r"^\|\s*\*\*(.+?)\*\*\s*\|\s*(.*?)\s*\|\s*$", line)
            if rm:
                metadata_rows.append((rm.group(1).strip(), rm.group(2).strip()))

    sections = {h: b for h, b in all_sections.items() if h not in ("Метадані", "Суміжні місця")}
    return title, metadata_rows, sections


# ── рендер картки бібліотеки ─────────────────────────────────────────────────

def render_metadata_table(rows):
    lines = ["| | |", "|:--|:--|"]
    for label, value in rows:
        lines.append(f"| **{label}** | {value} |")
    return "\n".join(lines)


def render_card(title, metadata_rows, section_items):
    parts = [f"# {title}", "", "## Метадані", "", render_metadata_table(metadata_rows)]
    for header, body in section_items:
        parts.append("")
        parts.append("---")
        parts.append("")
        parts.append(f"## {header}")
        parts.append("")
        parts.append(body.strip("\n"))
    return "\n".join(parts) + "\n"


_OVERLAY_HEADER_ORDER = ["Опис", "Нотатки", "Практичне", "Де зупинитись"]


def render_overlay_sections(overlay_by_header):
    """overlay_by_header: {'Опис': [...], 'Нотатки': [...], ...} → overlay MD
    або None, якщо винести не було чого. Порядок секцій фіксований
    (_OVERLAY_HEADER_ORDER), а не порядок появи в джерелі — щоб overlay-файли
    були передбачувані для читання."""
    if not any(overlay_by_header.values()):
        return None
    parts = ["<!-- Автоматичний перший зріз міграції. Перевірити руками. -->"]
    for header in _OVERLAY_HEADER_ORDER:
        units = overlay_by_header.get(header)
        if units:
            parts.append(OVERLAY_HEADING[header])
            parts.append("\n".join(units))
    return "\n\n".join(parts) + "\n"


# ── обробка одного файлу (без злиття) ───────────────────────────────────────

# Секції, з яких взагалі виносимо трип-специфічну прозу (§3 постановки +
# розширення після вичитки: ## Практичне і ## Де зупинитись теж містять
# прив'язані до дат/днів пункти — "заправитись перед D10", "e-вінєтка з
# 18.09"). ## Опис карток кордонів свідомо НЕ чіпаємо — там навмисно
# перемішані постійні факти й планові рішення цієї поїздки, розводити
# автоматично небезпечно (§ "Чого НЕ роби").
SPLIT_HEADERS_SENTENCE = {"Опис"}          # розбиття по реченнях
SPLIT_HEADERS_UNIT = {"Нотатки", "Практичне", "Де зупинитись"}  # по одиницях
OVERLAY_HEADING = {
    "Опис": "## Опис (доповнення)",
    "Нотатки": "## Нотатки дня",
    "Практичне": "## Практичне (доповнення)",
    "Де зупинитись": "## Де зупинитись (доповнення)",
}


def split_section(header, body, slug):
    """(бібліотечний текст, [винесені одиниці]) для однієї секції картки.

    Опис карток кордонів (slug.startswith('border-')) НЕ розколюємо —
    повертаємо body як є, нічого не винесено."""
    if header == "Опис":
        if slug.startswith("border-"):
            return body, []
        return split_opis(body)
    if header in SPLIT_HEADERS_UNIT:
        return split_units(body)
    return body, []


def process_single(fname, source_dir, slug, report):
    text = (source_dir / "places" / fname).read_text(encoding="utf-8")
    title, metadata_rows, sections = parse_card(text)

    kept_rows = [(l, v) for l, v in metadata_rows if not is_dropped_metadata(l)]
    final_rows = [("ID", f"`{slug}`"), ("Глибина", "повний")] + kept_rows

    overlay_by_header = {}
    section_items = []
    for header, body in sections.items():
        if header in SPLIT_HEADERS_SENTENCE or header in SPLIT_HEADERS_UNIT:
            lib, ov = split_section(header, body, slug)
            if ov:
                overlay_by_header.setdefault(header, []).extend(ov)
            if lib.strip():
                section_items.append((header, lib))
            elif ov:
                report.record_empty(slug, header)
            # інакше секція спорожніла після виносу — заголовок теж не пишемо
        else:
            section_items.append((header, body))

    card_md = render_card(title, final_rows, section_items)
    overlay_md = render_overlay_sections(overlay_by_header)

    report.record_overlay(slug, overlay_by_header)
    return card_md, overlay_md


# ── обробка групи злиття (2 файли → 1 картка) ───────────────────────────────

TITLE_OVERRIDES = {
    # Обидва напрямки того самого кордону мають різні заголовки в джерелі —
    # жодне з них саме по собі не годиться для двосторонньої картки, тому
    # заголовок побудовано вручну (порядок міст — як у slug: border-cc1-cc2).
    "border-ro-ua-siret-porubne": "Кордон RO↔UA: Сірет / Порубне (Siret / Porubne)",
    "border-bg-gr-kulata-promachonas": "Кордон BG↔GR: Кулата / Промахонас (Kulata / Promachonas)",
}


def combine_metadata_rows(rowsA, rowsB, slug, fnameA, fnameB, report):
    dictA, dictB = dict(rowsA), dict(rowsB)
    order = []
    for l, _ in rowsA + rowsB:
        if l not in order:
            order.append(l)
    merged = []
    for label in order:
        va, vb = dictA.get(label), dictB.get(label)
        if va is not None and vb is not None and va != vb:
            if label in ("Код", "Країна") and slug.startswith("border-"):
                merged.append((label, f"{va} / {vb}"))
                report.merge_notes.append(
                    f"  {slug}: Метадані/{label} — обидва напрямки зафіксовано: «{va} / {vb}»"
                )
            else:
                merged.append((label, va))
                report.merge_notes.append(
                    f"  ⚠️ {slug}: Метадані/{label} різниться "
                    f"({fnameA}={va!r} vs {fnameB}={vb!r}) — узято значення з {fnameA}, перевір вручну"
                )
        elif va is not None:
            merged.append((label, va))
        else:
            merged.append((label, vb))
    return merged


def merge_section_bodies(bodyA, bodyB, fnameA, fnameB, header, slug, report):
    a, b = bodyA.strip(), bodyB.strip()
    if a == b:
        return a
    marker = f"<!-- РОЗБІЖНІСТЬ: {fnameA} vs {fnameB}, перевір вручну -->"
    report.divergences.append(f"  {slug}: секція «{header}» — {fnameA} vs {fnameB}")
    return f"{a}\n\n{marker}\n\n{b}"


def process_merge(fnameA, fnameB, source_dir, slug, report):
    textA = (source_dir / "places" / fnameA).read_text(encoding="utf-8")
    textB = (source_dir / "places" / fnameB).read_text(encoding="utf-8")
    titleA, rowsA_raw, sectionsA = parse_card(textA)
    titleB, rowsB_raw, sectionsB = parse_card(textB)

    kept_rowsA = [(l, v) for l, v in rowsA_raw if not is_dropped_metadata(l)]
    kept_rowsB = [(l, v) for l, v in rowsB_raw if not is_dropped_metadata(l)]
    combined_rows = combine_metadata_rows(kept_rowsA, kept_rowsB, slug, fnameA, fnameB, report)
    final_rows = [("ID", f"`{slug}`"), ("Глибина", "повний")] + combined_rows

    if slug in TITLE_OVERRIDES:
        title = TITLE_OVERRIDES[slug]
        report.merge_notes.append(
            f"  {slug}: заголовки джерел різняться ({titleA!r} vs {titleB!r}) — "
            f"побудовано двосторонній заголовок вручну: {title!r}"
        )
    elif titleA == titleB:
        title = titleA
    else:
        title = titleA
        report.merge_notes.append(
            f"  ⚠️ {slug}: заголовки джерел різняться ({titleA!r} vs {titleB!r}) — узято {fnameA}"
        )

    header_order = list(sectionsA.keys())
    for h in sectionsB.keys():
        if h not in header_order:
            header_order.append(h)
    if list(sectionsA.keys()) != list(sectionsB.keys()):
        report.merge_notes.append(
            f"  ⚠️ {slug}: набір секцій різниться між {fnameA} і {fnameB} — обʼєднано union"
        )

    overlay_by_header = {}
    section_items = []
    for header in header_order:
        bodyA = sectionsA.get(header)
        bodyB = sectionsB.get(header)
        if bodyA is None:
            section_items.append((header, bodyB))
            continue
        if bodyB is None:
            section_items.append((header, bodyA))
            continue

        ovA, ovB = [], []
        if header in SPLIT_HEADERS_SENTENCE or header in SPLIT_HEADERS_UNIT:
            libA, ovA = split_section(header, bodyA, slug)
            libB, ovB = split_section(header, bodyB, slug)
            if ovA or ovB:
                overlay_by_header.setdefault(header, []).extend(ovA)
                overlay_by_header[header].extend(ovB)
            merged_body = merge_section_bodies(libA, libB, fnameA, fnameB, header, slug, report)
        else:
            merged_body = merge_section_bodies(bodyA, bodyB, fnameA, fnameB, header, slug, report)

        if merged_body.strip():
            section_items.append((header, merged_body))
        elif ovA or ovB:
            report.record_empty(slug, header)

    card_md = render_card(title, final_rows, section_items)
    overlay_md = render_overlay_sections(overlay_by_header)

    report.record_overlay(slug, overlay_by_header)
    return card_md, overlay_md


# ── route.json ───────────────────────────────────────────────────────────────

BORDER_FILES = {"0250.md", "0800.md", "1050.md", "1550.md", "1750.md", "1850.md"}


def parse_index_route(index_path):
    """[(день:int, дата_iso, [fname, ...]), ...] у порядку рядків INDEX.md."""
    text = index_path.read_text(encoding="utf-8")
    rows = []
    for m in re.finditer(
        r"^\|\s*(\d+)\s*\|\s*(\d{2}\.\d{2})[^|]*\|[^|]+\|([^|]+)\|",
        text, re.MULTILINE,
    ):
        day_num = int(m.group(1))
        dd, mm = m.group(2).split(".")
        date_iso = f"2026-{mm}-{dd}"
        fnames = re.findall(r"\((\d{4}\.md)\)", m.group(3))
        if fnames:
            rows.append((day_num, date_iso, fnames))
    return rows


def walk_points(route_rows):
    """Повторює алгоритм generate_state_from_md.py (day1: усі зупинки;
    подальші дні: без першої — це вчорашня ночівля; та сама точка наступної
    ночі підряд — не новий запис, а +1 до nights попередньої). Повертає
    список {day, date, fname, kind} — по одному на точку route_state.json,
    у тому самому порядку. Використовується ЛИШЕ для визначення дня й slug
    точки; kind/nights/arrive_at/stay_minutes у виході беруться з
    route_state.json, а не з цього обчислення."""
    out = []
    for i, (day_num, date_iso, fnames) in enumerate(route_rows):
        to_add = fnames if i == 0 else fnames[1:]
        for j, fname in enumerate(to_add):
            is_last = j == len(to_add) - 1
            if fname == "0100.md":
                kind = "home"
            elif fname in BORDER_FILES:
                kind = "border"
            elif is_last:
                kind = "overnight"
            else:
                kind = "visit"

            if kind == "overnight":
                prev_on = next((p for p in reversed(out) if p["kind"] == "overnight"), None)
                if prev_on is not None and prev_on["fname"] == fname:
                    prev_date = _date.fromisoformat(prev_on["date"])
                    nights = prev_on.get("nights", 1)
                    if _date.fromisoformat(date_iso) == prev_date + timedelta(days=nights):
                        prev_on["nights"] = nights + 1
                        continue

            out.append({"day": day_num, "date": date_iso, "fname": fname, "kind": kind})
    return out


def load_live_mirror_points(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    pts = (data.get("route") or {}).get("points")
    if pts is None:
        pts = data.get("points")
    return pts or []


def build_route_json(source_dir, slugmap, trip_id, live_mirror_path, report):
    index_path = source_dir / "places" / "INDEX.md"
    route_rows = parse_index_route(index_path)
    walked = walk_points(route_rows)

    state = json.loads((source_dir / "places" / "exports" / "route_state.json").read_text(encoding="utf-8"))
    rs_points = state["points"]

    if len(rs_points) != len(walked):
        raise SystemExit(
            f"❌ Розбіжність: INDEX.md дає {len(walked)} точок після згортання ночей, "
            f"а route_state.json — {len(rs_points)}. Зупинено — не підганяю."
        )

    for i, (w, rp) in enumerate(zip(walked, rs_points)):
        if w["date"] != rp.get("date"):
            raise SystemExit(
                f"❌ Розбіжність на точці {i}: INDEX.md дає дату {w['date']!r} ({w['fname']}), "
                f"route_state.json — {rp.get('date')!r} ({rp.get('label')}). Зупинено — не підганяю."
            )
        if w["kind"] != rp.get("kind"):
            report.route_notes.append(
                f"  ⚠️ точка {i} ({rp.get('label')}): kind з INDEX-проходу={w['kind']!r}, "
                f"з route_state.json={rp.get('kind')!r} — узято route_state.json (як і наказано)"
            )

    route_map = slugmap["route"]
    missing = [w["fname"] for w in walked if w["fname"] not in route_map]
    if missing:
        raise SystemExit(f"❌ У slugmap.route бракує fname: {sorted(set(missing))}")

    live_points = None
    if live_mirror_path:
        p = Path(live_mirror_path).expanduser()
        if p.exists():
            live_points = load_live_mirror_points(p)
            if len(live_points) != len(rs_points):
                report.route_notes.append(
                    f"  ⚠️ --live-mirror має {len(live_points)} точок, очікувалось {len(rs_points)} "
                    f"— map_point_id НЕ додається"
                )
                live_points = None
        else:
            report.route_notes.append(f"  ⚠️ --live-mirror файл не знайдено: {p} — map_point_id не додається")

    # 17 днів — навіть якщо для якогось дня жодної НОВОЇ точки не з'явилось
    # (напр. вільний день на Ітаці — вона вже записана попереднього дня з
    # nights:2, і повторно тут не зʼявляється).
    days = {d: {"day": d, "date": dt, "points": []} for d, dt, _ in route_rows}

    for i, (w, rp) in enumerate(zip(walked, rs_points)):
        slug = route_map[w["fname"]]
        point = {"id": slug}
        if live_points is not None:
            lp = live_points[i]
            if lp.get("label") != rp.get("label"):
                report.route_notes.append(
                    f"  ⚠️ точка {i}: label з live-mirror={lp.get('label')!r} != "
                    f"route_state.json={rp.get('label')!r} — map_point_id все одно взято по порядку"
                )
            if lp.get("id"):
                point["map_point_id"] = lp["id"]
        point["kind"] = rp.get("kind")
        nights = rp.get("nights")
        if nights and nights > 1:
            point["nights"] = nights
        if rp.get("arrive_at") is not None:
            point["arrive_at"] = rp["arrive_at"]
        if rp.get("stay_minutes") is not None:
            point["stay_minutes"] = rp["stay_minutes"]
        days[w["day"]]["points"].append(point)

    ordered_days = [days[d] for d in sorted(days)]

    alternates = []
    for fname, slug in slugmap["candidates_2026_09"].items():
        if slugmap.get("_ручні_рішення", {}).get(slug, "") and slug == "ro-postavarul":
            continue
        card_path = source_dir / "places" / fname
        text = card_path.read_text(encoding="utf-8")
        _, _, sections = parse_card(text)
        desc = sections.get("Опис", "")
        first_para = re.split(r"\n\s*\n", desc.strip())[0] if desc.strip() else ""
        note = strip_md_inline(first_para)
        alternates.append({"id": slug, "note": note})

    route_json = {
        "_": "Маршрут: порядок, ролі, розклад. Картки місць — у бібліотеці places/, посилання через id.",
        "trip": trip_id,
        "days": ordered_days,
        "alternates": alternates,
    }

    total_points = sum(len(d["points"]) for d in ordered_days)
    from collections import Counter
    kind_counter = Counter(p["kind"] for d in ordered_days for p in d["points"])

    return route_json, total_points, kind_counter


# ── звіт ─────────────────────────────────────────────────────────────────────

class Report:
    def __init__(self):
        self.merge_notes = []
        self.divergences = []
        self.overlay_counts = {}   # slug -> {header: n_units}
        self.route_notes = []
        self.unrecognized = []
        self.empty_sections = []   # "slug: секція «Header» спорожніла — заголовок прибрано"
        self.rewritten_links = []  # "context: XXXX.md → ціль"
        self.unresolved_links = []  # "context: XXXX.md — нема в slugmap"

    def record_overlay(self, slug, overlay_by_header):
        counts = {h: len(v) for h, v in overlay_by_header.items() if v}
        if counts:
            self.overlay_counts[slug] = counts

    def record_empty(self, slug, header):
        self.empty_sections.append(f"  {slug}: секція «{header}» спорожніла після виносу — заголовок прибрано")


# ── основний прохід по картках ──────────────────────────────────────────────

def build_slug_groups(slugmap):
    """slug -> [fname, ...] (1 або 2), зібрано з route+candidates_2026_09,
    звірено з slugmap.merge."""
    groups = {}
    for fname, slug in slugmap["route"].items():
        groups.setdefault(slug, []).append(fname)
    for fname, slug in slugmap["candidates_2026_09"].items():
        groups.setdefault(slug, []).append(fname)

    for slug, pair in slugmap.get("merge", {}).items():
        actual = sorted(groups.get(slug, []))
        if actual != sorted(pair):
            raise SystemExit(
                f"❌ slugmap.merge неузгоджений із route/candidates для {slug}: "
                f"merge каже {pair}, а фактичне групування дає {actual}"
            )
    return groups


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", required=True, help="корінь travel202609 (тільки читання)")
    ap.add_argument("--live-mirror", default=None, help="шлях до live_mirror.json для map_point_id")
    ap.add_argument("--dry-run", action="store_true", help="нічого не писати, лише показати, що зроблено б")
    args, _unknown = ap.parse_known_args()

    source_dir = Path(args.source).expanduser().resolve()
    if not source_dir.exists():
        raise SystemExit(f"❌ Джерела немає: {source_dir}")

    slugmap_path = CTX.data_root / "_migration" / "slugmap.json"
    slugmap = json.loads(slugmap_path.read_text(encoding="utf-8"))

    report = Report()
    groups = build_slug_groups(slugmap)

    files_read = 0
    cards = {}       # slug -> markdown text
    overlays = {}     # slug -> markdown text (or absent, якщо нема overlay)

    for slug in sorted(groups):
        fnames = groups[slug]
        for f in fnames:
            if not (source_dir / "places" / f).exists():
                raise SystemExit(f"❌ Немає файлу джерела: places/{f} (потрібен для {slug})")
        files_read += len(fnames)

        if len(fnames) == 1:
            card_md, overlay_md = process_single(fnames[0], source_dir, slug, report)
        elif len(fnames) == 2:
            fnameA, fnameB = fnames
            card_md, overlay_md = process_merge(fnameA, fnameB, source_dir, slug, report)
        else:
            raise SystemExit(f"❌ {slug}: неочікувано {len(fnames)} файлів у групі: {fnames}")

        cards[slug] = card_md
        if overlay_md:
            overlays[slug] = overlay_md

    # ── переписати посилання на старі 4-значні імена файлів ────────────────
    fname_slug_index = build_fname_slug_index(slugmap)
    for slug in list(cards):
        cards[slug] = rewrite_links(
            cards[slug], fname_slug_index, "{slug}.md", f"places/{slug}.md", report
        )
    for slug in list(overlays):
        overlays[slug] = rewrite_links(
            overlays[slug], fname_slug_index, "../../../places/{slug}.md",
            f"overlay/{slug}.md", report,
        )

    route_json, total_points, kind_counter = build_route_json(
        source_dir, slugmap, CTX.trip_id, args.live_mirror, report
    )

    # ── самоперевірка: кожен id у route.json має відповідну картку ──────────
    route_ids = {p["id"] for d in route_json["days"] for p in d["points"]}
    route_ids |= {a["id"] for a in route_json["alternates"]}
    missing_cards = sorted(route_ids - set(cards))
    if missing_cards:
        raise SystemExit(f"❌ У route.json є id без картки в бібліотеці: {missing_cards}")

    # ── запис ────────────────────────────────────────────────────────────
    places_dir = CTX.places_dir
    overlay_dir = CTX.overlay_dir
    country_info_src = source_dir / "places" / "country_info.md"
    practical_info_src = source_dir / "TRAVEL_PRACTICAL_INFO.md"

    if args.dry_run:
        print("[DRY RUN] нічого не пишеться\n")
    else:
        places_dir.mkdir(parents=True, exist_ok=True)
        overlay_dir.mkdir(parents=True, exist_ok=True)

    for slug, card_md in sorted(cards.items()):
        dest = places_dir / f"{slug}.md"
        if args.dry_run:
            print(f"[DRY RUN] написав би {dest}")
        else:
            dest.write_text(card_md, encoding="utf-8")

    # overlay: пишемо ті, що потрібні; прибираємо застарілі для карток цього
    # запуску, які раніше мали overlay, а тепер — ні (ідемпотентність).
    for slug in sorted(cards):
        dest = overlay_dir / f"{slug}.md"
        if slug in overlays:
            if args.dry_run:
                print(f"[DRY RUN] написав би {dest}")
            else:
                dest.write_text(overlays[slug], encoding="utf-8")
        else:
            if dest.exists():
                if args.dry_run:
                    print(f"[DRY RUN] видалив би застарілий {dest}")
                else:
                    dest.unlink()

    if args.dry_run:
        print(f"[DRY RUN] написав би {places_dir / 'country_info.md'}")
        print(f"[DRY RUN] написав би {places_dir / 'practical_info.md'}")
        print(f"[DRY RUN] написав би {CTX.route_json}")
    else:
        (places_dir / "country_info.md").write_text(
            country_info_src.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (places_dir / "practical_info.md").write_text(
            practical_info_src.read_text(encoding="utf-8"), encoding="utf-8"
        )
        CTX.write_json(CTX.route_json, route_json)

    # ── звіт ─────────────────────────────────────────────────────────────
    print("=" * 70)
    print("ЗВІТ МІГРАЦІЇ")
    print("=" * 70)
    print(f"\nФайлів прочитано: {files_read} → карток створено: {len(cards)}")
    print(f"  (38 файлів очікувалось [31 route + 7 candidates_2026_09], "
          f"35 карток [3 групи по 2 файли злито в 1])")

    print("\n-- Таблиця злиття --")
    for slug, pair in slugmap.get("merge", {}).items():
        print(f"  {slug}: {' + '.join(pair)}")

    print("\n-- Примітки злиття (метадані/заголовки) --")
    if report.merge_notes:
        for n in report.merge_notes:
            print(n)
    else:
        print("  (нема)")

    print("\n-- Секції з РОЗБІЖНОСТЯМИ при злитті (перевір вручну) --")
    if report.divergences:
        for n in report.divergences:
            print(n)
    else:
        print("  (нема)")

    print("\n-- Overlay: винесено одиниць по картках (за секціями) --")
    if report.overlay_counts:
        for slug in sorted(report.overlay_counts):
            counts = report.overlay_counts[slug]
            parts = [f"{h} — {n}" for h, n in counts.items()]
            print(f"  {slug}: " + ", ".join(parts))
    else:
        print("  (нічого не винесено)")

    print("\n-- З них НОВІ (після розширення на ## Практичне / ## Де зупинитись) --")
    new_ones = [
        (slug, counts["Практичне"] if "Практичне" in counts else 0,
         counts["Де зупинитись"] if "Де зупинитись" in counts else 0)
        for slug, counts in sorted(report.overlay_counts.items())
        if "Практичне" in counts or "Де зупинитись" in counts
    ]
    if new_ones:
        for slug, n_pract, n_stay in new_ones:
            bits = []
            if n_pract:
                bits.append(f"Практичне — {n_pract}")
            if n_stay:
                bits.append(f"Де зупинитись — {n_stay}")
            print(f"  {slug}: " + ", ".join(bits))
    else:
        print("  (нічого нового не знайдено)")

    print("\n-- Секції, що спорожніли й були прибрані цілком --")
    if report.empty_sections:
        for n in report.empty_sections:
            print(n)
    else:
        print("  (нема)")

    print(f"\n-- Переписані посилання ](XXXX.md) → ](slug) — {len(report.rewritten_links)} шт. --")
    if report.rewritten_links:
        for n in report.rewritten_links:
            print(n)
    else:
        print("  (нема)")

    print("\n-- Посилання БЕЗ цілі в slugmap.json (лишено як є) --")
    if report.unresolved_links:
        for n in report.unresolved_links:
            print(n)
    else:
        print("  (нема)")

    print("\n-- route.json: примітки --")
    if report.route_notes:
        for n in report.route_notes:
            print(n)
    else:
        print("  (нема)")

    print("\n-- Самоперевірка чисел --")
    n_days = len(route_json["days"])
    n_alt = len(route_json["alternates"])
    print(f"  днів: {n_days} (очікувалось 17) — {'OK' if n_days == 17 else 'МИМО'}")
    print(f"  точок сумарно: {total_points} (очікувалось 32) — {'OK' if total_points == 32 else 'МИМО'}")
    expected_kind = {"home": 2, "overnight": 14, "border": 6, "visit": 10}
    kind_ok = all(kind_counter.get(k) == v for k, v in expected_kind.items())
    print(f"  розподіл kind: {dict(kind_counter)} "
          f"(очікувалось {expected_kind}) — {'OK' if kind_ok else 'МИМО'}")
    print(f"  alternates: {n_alt} (очікувалось 6) — {'OK' if n_alt == 6 else 'МИМО'}")
    print(f"  карток бібліотеки: {len(cards)} (очікувалось 35) — "
          f"{'OK' if len(cards) == 35 else 'МИМО'}")

    print()


if __name__ == "__main__":
    main()
