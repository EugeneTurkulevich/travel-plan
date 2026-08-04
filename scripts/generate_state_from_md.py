"""
scripts/generate_state_from_md.py

Generates places/exports/route_state.json from MD source files.
MD files are the source of truth (SoT); this produces the lightweight map export.

Route order, dates, and overnight/visit classification come from INDEX.md.
country_info is read from places/country_info.md.

Usage:
    python3 scripts/generate_state_from_md.py            # overwrites SoT
    python3 scripts/generate_state_from_md.py --dry-run  # writes route_state_generated.json
"""
import json, re, sys
from datetime import date, timedelta
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trip_ctx import paths
CTX = paths()

INDEX        = CTX.trip_dir / "INDEX.md"   # Ф4: заміниться на route.json
PLACES       = CTX.places_dir
STATE        = CTX.route_state
COUNTRY_INFO = CTX.places_dir / "country_info.md"
HR     = "<hr style='border:0;border-top:1px solid #ddd;margin:6px 0'>"

# Deprecated place files – excluded even if found in table
DEPRECATED = set()

# Border-crossing place files — get kind='border' instead of 'visit'
BORDER_FILES = {
    "0250.md",   # UA→RO  Порубне / Сірет
    "0800.md",   # RO→BG  Калафат / Відін
    "1050.md",   # BG→GR  Кулата / Промахонас
    "1550.md",   # GR→BG  Промахонас / Кулата (повернення)
    "1750.md",   # BG→RO  Русе / Джурджу
    "1850.md",   # RO→UA  Сірет / Порубне (повернення)
}


# ── Text helpers ──────────────────────────────────────────────────────────────

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

def get_section(md_text, header):
    """Return raw text content of ## <header> section (up to next --- or EOF)."""
    escaped = re.escape(header)
    m = re.search(rf'## {escaped}\n(.*?)(?:\n---|\Z)', md_text, re.DOTALL)
    return m.group(1).strip() if m else ''


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


def parse_booking(md_text):
    """Read **Бронь** value from Метадані table.  Returns trimmed value or ''."""
    m = re.search(r'\*\*Бронь\*\*\s*\|\s*([^|]+)', md_text)
    return m.group(1).strip() if m else ''


def parse_timing(md_text, variant='normal'):
    """Read **Тайминг** value from the Метадані table.

    variant='normal'       → looks up `**Тайминг**`
    variant='home_start'   → `**Тайминг (старт)**` then falls back to `**Тайминг**`
    variant='home_finish'  → `**Тайминг (фініш)**` then falls back to `**Тайминг**`

    Returns the trimmed value or '' if not present.
    """
    candidates = {
        'home_start':  [r'Тайминг\s*\(старт\)',  r'Тайминг'],
        'home_finish': [r'Тайминг\s*\(фініш\)', r'Тайминг'],
        'normal':      [r'Тайминг'],
    }[variant]
    for key in candidates:
        m = re.search(rf'\*\*{key}\*\*\s*\|\s*([^|]+)', md_text)
        if m:
            return m.group(1).strip()
    return ''


# Ukrainian short weekday → ISO weekday index (Monday=0)
_WEEKDAY_UA = {'пн': 0, 'вт': 1, 'ср': 2, 'чт': 3, 'пт': 4, 'сб': 5, 'нд': 6}


# ── Timezones ─────────────────────────────────────────────────────────────────
# Вересень 2026 — DST діє до останньої неділі жовтня. Усі країни маршруту
# (UA, RO, BG, GR) — у зоні EET/EEST, тобто **+03:00 без винятків**.
# Якщо в маршрут колись додасться країна CET/CEST (PL, HU, RS, MK, AL) —
# додати її мітки у CET_LABELS.
CET_LABELS: set[str] = set()


def tz_offset_for(label: str) -> str:
    return "+02:00" if label in CET_LABELS else "+03:00"


def parse_schedule_value(timing: str, date_iso: str):
    """Parse a **Тайминг** value into (arrive_at, stay_minutes) for set_schedule.

    Recognized formats
      - visit:          "HH:MM, ~M:MM"        e.g. "09:30, ~1:30"
      - overnight:      "X.HH:MM-Y.HH:MM"     e.g. "пт.18:30-сб.08:30"
      - home start:     "X.HH:MM виїзд"       e.g. "чт.09:00 виїзд"
      - home finish:    "X.~HH:MM повернення" e.g. "нд.~17:30 повернення"

    Returns (arrive_at_iso 'YYYY-MM-DDTHH:MM', stay_minutes int) or (None, None).
    Tz offset is appended later by build_point (per-point, depends on label).
    """
    if not timing:
        return None, None
    v = timing.strip()

    # Overnight  "X.HH:MM-Y.HH:MM"
    m = re.match(r'^([а-яіїєґ]{2})\.(\d{1,2}):(\d{2})-([а-яіїєґ]{2})\.(\d{1,2}):(\d{2})$', v)
    if m:
        wd1, hh1, mm1, wd2, hh2, mm2 = m.groups()
        if wd1 not in _WEEKDAY_UA or wd2 not in _WEEKDAY_UA:
            return None, None
        days_diff = (_WEEKDAY_UA[wd2] - _WEEKDAY_UA[wd1]) % 7
        if days_diff == 0:
            days_diff = 7  # rare, but defend against bug
        stay = days_diff * 24 * 60 + (int(hh2)*60 + int(mm2)) - (int(hh1)*60 + int(mm1))
        arrive = f"{date_iso}T{int(hh1):02d}:{mm1}"
        return arrive, stay

    # Visit  "HH:MM, ~M:MM"
    m = re.match(r'^(\d{1,2}):(\d{2})\s*,\s*~?(\d{1,2}):(\d{2})$', v)
    if m:
        hh, mm, dh, dm = m.groups()
        arrive = f"{date_iso}T{int(hh):02d}:{mm}"
        stay = int(dh)*60 + int(dm)
        return arrive, stay

    # Home start  "X.HH:MM виїзд"
    m = re.match(r'^([а-яіїєґ]{2})\.~?(\d{1,2}):(\d{2})\s*виїзд', v)
    if m:
        _, hh, mm = m.groups()
        return f"{date_iso}T{int(hh):02d}:{mm}", 0

    # Home finish  "X.~HH:MM повернення"
    m = re.match(r'^([а-яіїєґ]{2})\.~?(\d{1,2}):(\d{2})\s*повернення', v)
    if m:
        _, hh, mm = m.groups()
        return f"{date_iso}T{int(hh):02d}:{mm}", 0

    return None, None


def parse_main_coords(md_text):
    """Extract lat, lon from the Метадані table."""
    m = re.search(r'\*\*Координати\*\*\s*\|\s*([^|]+)', md_text)
    if m:
        return parse_coords(m.group(1))
    return None


# ── Popup HTML assembly ───────────────────────────────────────────────────────

def _vp_block(vp_table):
    """Viewpoints are kept as nested_points only; not rendered in popup anymore."""
    return ''


def _local_block(local_table):
    if not local_table:
        return ''
    lines = []
    for row in local_table:
        if len(row) < 2:
            continue
        name = strip_md(row[0])
        desc = row[1].strip()
        if name:
            lines.append(f'<b>{name}</b> — {desc}')
    if not lines:
        return ''
    return f"{HR}🛍️ <b>Місцева особливість</b><br>" + '<br>'.join(lines)


def _gelato_block(gelato_table):
    if not gelato_table:
        return ''
    lines = []
    for row in gelato_table:
        if len(row) < 2:
            continue
        name = strip_md(row[0])
        desc = row[1].strip()
        if name:
            lines.append(f'<b>{name}</b> — {desc}')
    if not lines:
        return ''
    return f"{HR}🍦 <b>Морозиво</b><br>" + '<br>'.join(lines)


def _craft_block(craft_table):
    if not craft_table:
        return ''
    lines = []
    for row in craft_table:
        if len(row) < 2:
            continue
        name = strip_md(row[0])
        desc = row[1].strip()
        if name:
            lines.append(f'<b>{name}</b> — {desc}')
    if not lines:
        return ''
    return f"{HR}🍺 <b>Крафт</b><br>" + '<br>'.join(lines)


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


def build_popup_html(label, desc, vp_table, poi_table, gelato_table,
                     craft_table, local_table, is_overnight, booking=''):
    local  = _local_block(local_table)
    photos = _photos_div(poi_table)
    gelato = _gelato_block(gelato_table)
    craft  = _craft_block(craft_table) if is_overnight else ''

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

def build_nested_points(poi_table, vp_table, gelato_table, craft_table, local_table):
    nested = []

    for row in poi_table:
        if len(row) < 3:
            continue
        name   = strip_md(row[0])
        desc   = row[1].strip()
        coords = parse_coords(row[2])
        photo  = row[3].strip().strip('`') if len(row) > 3 else ''
        if not name or not coords:
            continue
        np = {
            'name': name, 'lat': coords[0], 'lon': coords[1],
            'description': desc,
            'type': 'poi', 'type_name': 'Цікаві місця',
            'type_icon': '⭐', 'icon': '⭐',
        }
        if photo and photo.startswith('http'):
            np['photo_url'] = photo
        nested.append(np)

    for row in vp_table:
        if len(row) < 3:
            continue
        name   = strip_md(row[0])
        desc   = row[1].strip()
        coords = parse_coords(row[2])
        if not name or not coords:
            continue
        nested.append({
            'name': name, 'lat': coords[0], 'lon': coords[1],
            'description': desc,
            'type': 'viewpoint', 'type_name': 'Оглядові точки',
            'type_icon': '🏰', 'icon': '🏰',
        })

    for row in gelato_table:
        if len(row) < 3:
            continue
        name   = strip_md(row[0])
        desc   = row[1].strip()
        coords = parse_coords(row[2])
        if not name or not coords:
            continue
        nested.append({
            'name': name, 'lat': coords[0], 'lon': coords[1],
            'description': desc,
            'type': 'icecream', 'type_name': 'Морозиво',
            'type_icon': '🍦', 'icon': '🍦',
        })

    for row in craft_table:
        if len(row) < 3:
            continue
        name   = strip_md(row[0])
        desc   = row[1].strip()
        coords = parse_coords(row[2])
        if not name or not coords:
            continue
        nested.append({
            'name': name, 'lat': coords[0], 'lon': coords[1],
            'description': desc,
            'type': 'craft', 'type_name': 'Крафт-паби',
            'type_icon': '🍺', 'icon': '🍺',
        })

    for row in local_table:
        if len(row) < 3:
            continue
        name   = strip_md(row[0])
        desc   = row[1].strip()
        coords = parse_coords(row[2])
        photo  = row[3].strip().strip('`') if len(row) > 3 else ''
        if not name or not coords:
            continue
        np = {
            'name': name, 'lat': coords[0], 'lon': coords[1],
            'description': desc,
            'type': 'local', 'type_name': 'Місцева особливість',
            'type_icon': '🛍️', 'icon': '🛍️',
        }
        if photo and photo.startswith('http'):
            np['photo_url'] = photo
        nested.append(np)

    return nested or None


# ── Build a single point ──────────────────────────────────────────────────────

def build_point(fname, kind, date_iso, timing_variant='normal'):
    md_path = PLACES / fname
    if not md_path.exists():
        print(f"  [WARN] {fname} not found — skipped")
        return None

    md = md_path.read_text(encoding='utf-8')

    label  = parse_label(md)
    coords = parse_main_coords(md)
    if not coords:
        print(f"  [WARN] {fname}: no coordinates found — skipped")
        return None
    lat, lon = coords

    timing  = parse_timing(md, variant=timing_variant)
    booking = parse_booking(md)

    desc = get_section(md, 'Опис')

    poi_sec    = get_section(md, 'Цікаві POI')
    vp_sec     = get_section(md, 'Оглядові точки')
    craft_sec  = get_section(md, '🍺 Крафт')
    gelato_sec = get_section(md, '🍦 Морозиво')
    local_sec  = get_section(md, '🛍️ Місцева особливість')

    poi_table    = parse_table(poi_sec)
    vp_table     = parse_table(vp_sec)
    craft_table  = parse_table(craft_sec)
    gelato_table = parse_table(gelato_sec)
    local_table  = parse_table(local_sec)

    is_overnight = kind == 'overnight'

    popup = build_popup_html(
        label, desc, vp_table, poi_table, gelato_table, craft_table, local_table,
        is_overnight, booking=booking,
    )

    entry   = get_section(md, "В'їзд на авто")
    parking = get_section(md, 'Паркування')
    stay    = get_section(md, 'Де зупинитись')
    extra   = build_extra_info(entry, parking, stay)

    nested = build_nested_points(poi_table, vp_table, gelato_table, craft_table, local_table)

    point = {
        'lat': lat, 'lon': lon,
        'kind': kind,
        'label': label,
        'popup_html': popup,
        'date': date_iso,
    }
    arrive_at, stay_minutes = parse_schedule_value(timing, date_iso)
    if arrive_at is not None:
        point['arrive_at']    = arrive_at + tz_offset_for(label)
        point['stay_minutes'] = stay_minutes
    if extra:
        point['extra_info'] = extra
    if nested:
        point['nested_points'] = nested

    return point


# ── Parse INDEX.md route table ────────────────────────────────────────────────

def parse_index_route():
    """Return list of (date_iso, [fname, ...]) per table row, in order."""
    text = INDEX.read_text(encoding='utf-8')
    route = []
    # Match: | <day#> | DD.MM <weekday> | <icon> | <stops> | <notes> |
    for m in re.finditer(
        r'^\|\s*\d+\s*\|\s*(\d{2}\.\d{2})[^|]*\|[^|]+\|([^|]+)\|',
        text, re.MULTILINE
    ):
        dd_mm   = m.group(1)          # "14.05"
        stops_s = m.group(2)          # "[Київ](0100.md) → ..."

        dd, mm  = dd_mm.split('.')
        date_iso = f"2026-{mm}-{dd}"

        fnames = re.findall(r'\((\d{4}\.md)\)', stops_s)
        fnames = [f for f in fnames if f not in DEPRECATED]

        if fnames:
            route.append((date_iso, fnames))

    return route


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

    route = parse_index_route()
    print(f"INDEX: {len(route)} days parsed\n")

    points = []
    home_seen = 0  # 0 → next home is start, 1 → finish

    for i, (date_iso, fnames) in enumerate(route):
        # Day 1: add all stops; subsequent days: skip first (it was previous overnight)
        to_add = fnames if i == 0 else fnames[1:]

        for j, fname in enumerate(to_add):
            is_last = (j == len(to_add) - 1)

            if fname == '0100.md':
                kind = 'home'
            elif fname in BORDER_FILES:
                kind = 'border'
            elif is_last:
                kind = 'overnight'
            else:
                kind = 'visit'

            # Multi-night: та сама ночівля, що й попередньої ночі → nights += 1.
            # Шукаємо саме останню *overnight*-точку, а не points[-1]: між двома
            # ночами може стояти денна visit-точка (радіальна вилазка з бази:
            # база → обʼєкт → база, і база НЕ дублюється).
            if kind == 'overnight':
                prev_on = next((p for p in reversed(points)
                                if p['kind'] == 'overnight'), None)
                if prev_on is not None and prev_on.get('_fname') == fname:
                    prev_date = date.fromisoformat(prev_on['date'])
                    nights = prev_on.get('nights', 1)
                    # Зливаємо тільки якщо ночі дійсно підряд — щоб повторний
                    # візит у те саме місце через тиждень лишився окремою точкою.
                    if date.fromisoformat(date_iso) == prev_date + timedelta(days=nights):
                        prev_on['nights'] = nights + 1
                        print(f"  [+night   ]  {date_iso}  {fname}  nights={prev_on['nights']}")
                        continue

            timing_variant = 'normal'
            if kind == 'home':
                timing_variant = 'home_start' if home_seen == 0 else 'home_finish'
                home_seen += 1

            pt = build_point(fname, kind, date_iso, timing_variant=timing_variant)
            if pt:
                pt['_fname'] = fname
                points.append(pt)
                print(f"  [{kind:9s}]  {date_iso}  {fname}  {pt['label']}")

    # Remove internal tracking key
    for pt in points:
        pt.pop('_fname', None)

    state = {
        'version': 1,
        'points':  points,
        'country_info': country_info,
    }

    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    tag = '[DRY RUN] ' if dry_run else ''
    print(f"\n{tag}Generated {len(points)} points → {out_path}")


if __name__ == '__main__':
    main()
