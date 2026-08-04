"""
scripts/generate_state.py

Generates <дані>/trips/<профіль>/exports/route_state.json з нової моделі даних:
route.json (порядок, ролі, розклад) + бібліотека places/<id>.md + overlay
trips/<профіль>/overlay/<id>.md (доповнення для конкретної поїздки).

Це ПОРТ generate_state_from_md.py на нову модель входу — уся логіка складання
popup_html / nested_points / extra_info / country_info перенесена звідти
буквально, включно з відомими вадами (див. коментарі нижче). Що змінюється
проти старого генератора — див. заголовок задачі / коміт-повідомлення.

Usage:
    python3 scripts/generate_state.py            # overwrites SoT
    python3 scripts/generate_state.py --dry-run  # writes route_state_generated.json
"""
import json, re, sys
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trip_ctx import paths
CTX = paths()

PLACES       = CTX.places_dir
OVERLAY      = CTX.overlay_dir
STATE        = CTX.route_state
COUNTRY_INFO = CTX.places_dir / "country_info.md"
ROUTE_JSON   = CTX.route_json
TRIP_JSON    = CTX.trip_json
HR = "<hr style='border:0;border-top:1px solid #ddd;margin:6px 0'>"

TRIP = CTX.read_json(TRIP_JSON)
# {"viewpoint": {"icon": "🏰", "section": "Оглядові точки"}, "icecream": {...}, ...}
# Джерело: trip.json → poi_categories (STRUCTURE_PROPOSAL.md §Н3). Категорія
# "poi" (базові "Цікаві POI") туди не входить — вона фіксована для всіх
# поїздок, як і в старому генераторі.
POI_CATEGORIES = TRIP.get('poi_categories', {})

BASE_POI_SECTION = 'Цікаві POI'

# type_name — текст, який бачить користувач у nested_points. trip.json дає
# лише icon + назву MD-секції, НЕ type_name (наприклад, секція "Крафт", але
# type_name лишається "Крафт-паби" — так було в старому генераторі). Тому
# type_name лишається зашитим тут, буквально як у generate_state_from_md.py.
TYPE_NAMES = {
    'poi':       'Цікаві місця',
    'viewpoint': 'Оглядові точки',
    'icecream':  'Морозиво',
    'craft':     'Крафт-паби',
    'local':     'Місцева особливість',
}


# ── Text helpers (ported verbatim from generate_state_from_md.py) ─────────────

def _inline_md(text):
    """Convert **bold** / *italic* inline markdown to HTML."""
    t = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    t = re.sub(r'\*(.+?)\*',     r'<i>\1</i>', t)
    return t


def strip_md(text):
    """Remove all markdown markup, return plain text."""
    t = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    t = re.sub(r'\*(.*?)\*',     r'\1', t)
    t = re.sub(r'`([^`]*)`',     r'\1', t)
    return t.strip()


def block_to_html(text):
    """Convert a markdown section body to inline HTML for extra_info / popup desc.

    Bullet lists (`- item`) are joined with <br>.  Inline bold/italic converted.
    Empty lines between bullet items are dropped; double newlines between prose
    become <br><br>.
    """
    lines = []
    for line in text.strip().splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append('')          # preserve paragraph break
            continue
        if stripped.startswith('- '):
            stripped = stripped[2:]
        lines.append(_inline_md(stripped))

    # Collapse runs of blank lines to a single paragraph marker
    result = []
    prev_blank = False
    for ln in lines:
        if ln == '':
            if not prev_blank:
                result.append('<br><br>')
            prev_blank = True
        else:
            result.append(ln)
            prev_blank = False

    # Join non-break items with <br>
    out = []
    for item in result:
        if item == '<br><br>':
            out.append(item)
        else:
            if out and out[-1] not in ('<br><br>',):
                out.append('<br>')
            out.append(item)

    joined = ''.join(out)
    while joined.startswith('<br>'):
        joined = joined[4:]
    while joined.endswith('<br>'):
        joined = joined[:-4]
    return joined


def parse_coords(raw):
    """Parse 'lat, lon' string (with optional backticks) → (float, float) or None."""
    clean = raw.strip().strip('`').strip()
    m = re.match(r'([-\d.]+)\s*,\s*([-\d.]+)', clean)
    return (float(m.group(1)), float(m.group(2))) if m else None


# ── MD section parsing ────────────────────────────────────────────────────────

def get_section(md_text, title):
    """Return raw text content of a '## ...title' section.

    `title` may be the whole heading text ('Опис', 'Опис (доповнення)') or the
    part after a leading emoji/token ('Морозиво' matches '## 🍦 Морозиво') —
    trip.json's poi_categories carries section names without the emoji prefix
    that the MD files actually use.

    Stops at the next '## ' heading OR a '---' rule, whichever comes first.
    Library cards separate every section with '---'; trip overlay files do
    NOT (only consecutive '## ' headings) — stopping only at '---' like the
    old get_section did would swallow the next overlay section whole.
    """
    lines = md_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if not line.startswith('## '):
            continue
        heading = line[3:].strip()
        if heading == title:
            start = i + 1
            break
        stripped = re.sub(r'^\S+\s+', '', heading)  # drop leading emoji/token
        if stripped == title:
            start = i + 1
            break
    if start is None:
        return ''
    body = []
    for line in lines[start:]:
        if line.strip() == '---' or line.startswith('## '):
            break
        body.append(line)
    return '\n'.join(body).strip()


def parse_table(section_text):
    """Parse a markdown table → list of data-row cell-lists (header+separator skipped)."""
    rows = []
    for line in section_text.splitlines():
        line = line.strip()
        if not line.startswith('|'):
            continue
        # Skip separator rows (| :--- | --- |)
        if re.match(r'\|[\s:|\-]+\|', line):
            continue
        cells = [c.strip() for c in line.split('|')[1:-1]]
        if cells:
            rows.append(cells)
    # First row is the header → skip it
    return rows[1:] if len(rows) > 1 else []


# ── MD file parsing ───────────────────────────────────────────────────────────

def parse_label(md_text):
    """Extract Ukrainian label from H1, stripping ' / English name' suffix."""
    m = re.match(r'^# (.+)$', md_text, re.MULTILINE)
    if not m:
        return 'Unknown'
    h1 = m.group(1).strip()
    if ' / ' in h1:
        h1 = h1[:h1.index(' / ')].strip()
    return h1


def parse_main_coords(md_text):
    """Extract lat, lon from the Метадані table."""
    m = re.search(r'\*\*Координати\*\*\s*\|\s*([^|]+)', md_text)
    if m:
        return parse_coords(m.group(1))
    return None


# ── Popup HTML assembly ───────────────────────────────────────────────────────

def _table_block(table, icon, title):
    """Local/gelato/craft popup block — ported verbatim from _local_block /
    _gelato_block / _craft_block. КАЖНА з трьох мала однакову ваду: `desc`
    береться сирим (row[1].strip()), без прогону через _inline_md — лише
    назва (перша колонка) чиститься strip_md. Це той самий дефект, який
    задача явно назвала на прикладі _local_block (🛍️); тут він відтворений
    буквально і для 🍦/🍺 теж — не виправлено навмисно."""
    if not table:
        return ''
    lines = []
    for row in table:
        if len(row) < 2:
            continue
        name = strip_md(row[0])
        desc = row[1].strip()
        if name:
            lines.append(f'<b>{name}</b> — {desc}')
    if not lines:
        return ''
    return f"{HR}{icon} <b>{title}</b><br>" + '<br>'.join(lines)


def _photos_div(poi_table):
    urls = []
    for row in poi_table:
        if len(row) < 4:
            continue
        url = row[3].strip().strip('`')
        if url.startswith('http'):
            urls.append(url)
    if not urls:
        return ''
    imgs = ''.join(
        f'<img src="{u}" style="width:100%;height:auto;display:block;margin:4px 0;object-fit:cover">'
        for u in urls
    )
    return f'<div>{imgs}</div>'


def build_popup_html(label, desc, poi_table, gelato_table, gelato_cfg,
                      craft_table, craft_cfg, local_table, local_cfg,
                      is_overnight, booking=''):
    # Оглядові точки (viewpoint) у попап не рендеряться — лишаються тільки як
    # nested_points, так само як у generate_state_from_md.py (_vp_block).
    local  = _table_block(local_table, local_cfg.get('icon', '🛍️'),
                           local_cfg.get('section', 'Місцева особливість'))
    photos = _photos_div(poi_table)
    gelato = _table_block(gelato_table, gelato_cfg.get('icon', '🍦'),
                           gelato_cfg.get('section', 'Морозиво'))
    craft  = (_table_block(craft_table, craft_cfg.get('icon', '🍺'),
                            craft_cfg.get('section', 'Крафт'))
              if is_overnight else '')

    # Час прибуття/перебування рендерить map-server з arrive_at/stay_minutes.
    title = f'<b>{label}</b>'
    booking_line = (
        f"<div style='background:#fff7e0;border-left:3px solid #e0a800;"
        f"padding:4px 6px;margin:4px 0;font-size:0.9em'>{booking}</div>"
        if booking else ''
    )
    parts = [f'{title}<br>{booking_line}{block_to_html(desc)}']

    if local:
        parts.append(local)

    if photos:
        if not local:
            parts.append('<br><br>')
        parts.append(photos)

    if gelato:
        parts.append(gelato)

    if craft:
        parts.append(craft)

    return ''.join(parts)


# ── extra_info ────────────────────────────────────────────────────────────────

def build_extra_info(entry_text, parking_text, stay_text):
    result = []
    if entry_text:
        result.append(f"<b>🚗 В'їзд на авто</b><br>{block_to_html(entry_text)}")
    if parking_text:
        result.append(f"<b>🅿️ Паркування</b><br>{block_to_html(parking_text)}")
    if stay_text:
        result.append(f"<b>🛏️ Де зупинитись</b><br>{block_to_html(stay_text)}")
    return result or None


# ── nested_points ─────────────────────────────────────────────────────────────

def _nested_from_table(table, type_key, icon, with_photo):
    out = []
    for row in table:
        if len(row) < 3:
            continue
        name   = strip_md(row[0])
        desc   = row[1].strip()
        coords = parse_coords(row[2])
        if not name or not coords:
            continue
        np = {
            'name': name, 'lat': coords[0], 'lon': coords[1],
            'description': desc,
            'type': type_key, 'type_name': TYPE_NAMES[type_key],
            'type_icon': icon, 'icon': icon,
        }
        if with_photo:
            photo = row[3].strip().strip('`') if len(row) > 3 else ''
            if photo.startswith('http'):
                np['photo_url'] = photo
        out.append(np)
    return out


def build_nested_points(poi_table, vp_table, vp_cfg,
                         gelato_table, gelato_cfg,
                         craft_table, craft_cfg,
                         local_table, local_cfg):
    # Порядок як у generate_state_from_md.py: poi → viewpoint → icecream →
    # craft → local. Фото є лише у poi й local (як у старому генераторі) —
    # craft тут НЕ залежить від is_overnight (на відміну від попап-блоку) —
    # так само, як у старому build_nested_points.
    nested = []
    nested += _nested_from_table(poi_table, 'poi', '⭐', with_photo=True)
    nested += _nested_from_table(vp_table, 'viewpoint', vp_cfg.get('icon', '🏰'), with_photo=False)
    nested += _nested_from_table(gelato_table, 'icecream', gelato_cfg.get('icon', '🍦'), with_photo=False)
    nested += _nested_from_table(craft_table, 'craft', craft_cfg.get('icon', '🍺'), with_photo=False)
    nested += _nested_from_table(local_table, 'local', local_cfg.get('icon', '🛍️'), with_photo=True)
    return nested or None


# ── Build a single point (картка бібліотеки + overlay поїздки) ────────────────

def build_point(place_id, kind, date_iso, arrive_at=None, stay_minutes=None, nights=None,
                 label_override=None, lat_override=None, lon_override=None, booking_note=None):
    card_path = PLACES / f"{place_id}.md"
    if not card_path.exists():
        print(f"  [WARN] {place_id}: картка {card_path.name} не знайдена — пропущено")
        return None
    card_text = card_path.read_text(encoding='utf-8')

    overlay_path = OVERLAY / f"{place_id}.md"
    overlay_text = overlay_path.read_text(encoding='utf-8') if overlay_path.exists() else ''

    # label/lat/lon — властивість появи точки в маршруті, а не самого місця
    # (напрямок кордону, обране ім'я перетину тощо). route.json може
    # перекрити будь-яке з них на рівні конкретної точки; коли перекриття
    # немає — беруться з картки бібліотеки, як і раніше.
    label  = label_override if label_override is not None else parse_label(card_text)
    coords = parse_main_coords(card_text)
    if lat_override is not None and lon_override is not None:
        lat, lon = lat_override, lon_override
    elif coords:
        lat, lon = coords
    else:
        print(f"  [WARN] {place_id}: координат не знайдено — пропущено")
        return None

    # ## Опис картки + ## Опис (доповнення) overlay, через пробіл, у цьому
    # порядку (див. задачу).
    card_desc    = get_section(card_text, 'Опис')
    overlay_desc = get_section(overlay_text, 'Опис (доповнення)') if overlay_text else ''
    desc = ' '.join(p for p in (card_desc, overlay_desc) if p)

    vp_cfg     = POI_CATEGORIES.get('viewpoint', {})
    gelato_cfg = POI_CATEGORIES.get('icecream', {})
    craft_cfg  = POI_CATEGORIES.get('craft', {})
    local_cfg  = POI_CATEGORIES.get('local', {})

    poi_table    = parse_table(get_section(card_text, BASE_POI_SECTION))
    vp_table     = parse_table(get_section(card_text, vp_cfg.get('section', 'Оглядові точки')))
    gelato_table = parse_table(get_section(card_text, gelato_cfg.get('section', 'Морозиво')))
    craft_table  = parse_table(get_section(card_text, craft_cfg.get('section', 'Крафт')))
    local_table  = parse_table(get_section(card_text, local_cfg.get('section', 'Місцева особливість')))

    is_overnight = kind == 'overnight'

    popup = build_popup_html(
        label, desc, poi_table, gelato_table, gelato_cfg,
        craft_table, craft_cfg, local_table, local_cfg,
        is_overnight, booking=booking_note or '',
    )

    # Решта секцій — лише з картки (overlay їх не чіпає, за умовою задачі).
    entry   = get_section(card_text, "В'їзд на авто")
    parking = get_section(card_text, 'Паркування')
    stay    = get_section(card_text, 'Де зупинитись')
    extra   = build_extra_info(entry, parking, stay)

    nested = build_nested_points(
        poi_table, vp_table, vp_cfg,
        gelato_table, gelato_cfg,
        craft_table, craft_cfg,
        local_table, local_cfg,
    )

    point = {
        'lat': lat, 'lon': lon,
        'kind': kind,
        'label': label,
        'popup_html': popup,
        'date': date_iso,
    }
    if arrive_at is not None:
        point['arrive_at'] = arrive_at
    if stay_minutes is not None:
        point['stay_minutes'] = stay_minutes
    if extra:
        point['extra_info'] = extra
    if nested:
        point['nested_points'] = nested
    if nights is not None:
        point['nights'] = nights

    return point


# ── route.json → flat, ordered list of point specs ────────────────────────────

def parse_route():
    """route.json → [{id, map_point_id, kind, date, arrive_at, stay_minutes,
    nights, label, lat, lon, booking_note}, ...] в порядку днів/точок.

    На відміну від старого генератора (INDEX.md + мердж суміжних ночей), тут
    мерджити нічого не треба: route.json уже несе правильний `nights` на
    єдиному представницькому записі точки (порожні days[].points, напр. день
    відпочинку в середині багатоночівельної зупинки, просто пропускаються).
    Місце, відвідане двічі (Київ, Чернівці, обидва кордони), дає два окремі
    записи тут — обидва рази з тієї самої картки бібліотеки, за задумом.

    `label`/`lat`/`lon`/`booking_note` — необов'язкові поля на рівні точки:
    властивість конкретної появи (напрямок кордону, текст бронювання), а не
    самого місця. Коли їх немає — беруться з картки бібліотеки (label/lat/lon)
    або не рендеряться (booking_note). scripts/patch_route_bookings.py — те,
    що їх сюди пише.
    """
    route = CTX.read_json(ROUTE_JSON)
    out = []
    for day in route.get('days', []):
        date_iso = day.get('date')
        for pt in day.get('points', []):
            out.append({
                'id':            pt['id'],
                'map_point_id':  pt.get('map_point_id'),
                'kind':          pt['kind'],
                'date':          date_iso,
                'arrive_at':     pt.get('arrive_at'),
                'stay_minutes':  pt.get('stay_minutes'),
                'nights':        pt.get('nights'),
                'label':         pt.get('label'),
                'lat':           pt.get('lat'),
                'lon':           pt.get('lon'),
                'booking_note':  pt.get('booking_note'),
            })
    return out


# ── country_info from places/country_info.md ─────────────────────────────────

def parse_country_info():
    """Read places/country_info.md → {CC: {description: HTML}}.

    File format: H2 sections `## CC` (ISO 3166-1 alpha-2 code) followed by HTML
    body until the next `##` or `---` separator. Body is taken verbatim (already
    HTML — no markdown conversion).
    """
    if not COUNTRY_INFO.exists():
        print(f"  [WARN] {COUNTRY_INFO} not found — country_info will be empty")
        return {}

    text = COUNTRY_INFO.read_text(encoding='utf-8')
    result = {}
    # Match each "## CC" header followed by body up to the next "## " or EOF.
    for m in re.finditer(r'^## ([A-Z]{2})\s*$\n(.*?)(?=^## [A-Z]{2}\s*$|\Z)',
                          text, re.MULTILINE | re.DOTALL):
        code = m.group(1)
        body = m.group(2).strip()
        # Drop trailing '---' separators and surrounding blank lines
        body = re.sub(r'\n*-{3,}\s*$', '', body).strip()
        if body:
            result[code] = {'description': body}
    return result


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    dry_run  = '--dry-run' in sys.argv
    out_path = STATE.parent / 'route_state_generated.json' if dry_run else STATE

    country_info = parse_country_info()
    print(f"country_info: {len(country_info)} countries from {COUNTRY_INFO.name}\n")

    route_points = parse_route()
    print(f"route.json: {len(route_points)} точок розібрано\n")

    points = []
    for rp in route_points:
        pt = build_point(
            rp['id'], rp['kind'], rp['date'],
            arrive_at=rp['arrive_at'], stay_minutes=rp['stay_minutes'], nights=rp['nights'],
            label_override=rp['label'], lat_override=rp['lat'], lon_override=rp['lon'],
            booking_note=rp['booking_note'],
        )
        if pt is None:
            continue
        pt['id'] = rp['map_point_id']
        points.append(pt)
        print(f"  [{rp['kind']:9s}]  {rp['date']}  {rp['id']:35s}  {pt['label']}")

    state = {
        'version': 1,
        'points':  points,
        'country_info': country_info,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    tag = '[DRY RUN] ' if dry_run else ''
    print(f"\n{tag}Generated {len(points)} points → {out_path}")


if __name__ == '__main__':
    main()
