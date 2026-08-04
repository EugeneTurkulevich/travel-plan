"""
scripts/patch_route_bookings.py

Одноразовий патч-скрипт, що закриває дірку в специфікації Ф3: migrate_cards.py
видалив рядок `**Бронь**` із карток бібліотеки, не переклавши його в
route.json, а обидва напрямки кожного двобічного кордону звели в одну
нейтральну картку, втративши напрямково-специфічні label/координати.

Бронювання і напрямок кордону — властивості КОНКРЕТНОЇ ПОЯВИ точки в
маршруті, а не самого місця (Чернівці з'являються двічі з різним текстом
Бронь; кожен кордон — двічі, у різні боки). Тому обидва патчаться в
route.json на рівні точки, а не в картці бібліотеки.

Джерела — ТІЛЬКИ ЧИТАННЯ:
  - /Users/eugenet/GitHub/travel202609/places/*.md   (архівні картки старої
    моделі — звідти читається рядок `| **Бронь** | … |`)
  - travel-data/_migration/slugmap.json              (file → slug, злиття)

Пишеться:
  - route.json АКТИВНОГО профілю (через trip_ctx) — додається `booking_note`
    на кожну появу, де джерело мало Бронь, і `label`/`lat`/`lon` — на 4 появи
    кордонів (значення взяті дослівно з еталона
    travel202609/places/exports/route_state.json, за вказівкою). Решта
    route.json не чіпається — файл читається, точково допатчується, пишеться
    назад, а не збирається з нуля.

Ідемпотентний: другий прогін дає той самий route.json.

Usage:
    python3 scripts/patch_route_bookings.py            # патчить route.json
    python3 scripts/patch_route_bookings.py --dry-run  # тільки друкує план
"""
import json, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trip_ctx import paths
CTX = paths()

OLD_PLACES = Path('/Users/eugenet/GitHub/travel202609/places')   # тільки читати
SLUGMAP    = CTX.data_root / '_migration' / 'slugmap.json'
ROUTE_JSON = CTX.route_json

# Появи кордонів, що потребують напрямкового перекриття label/lat/lon.
# Ключ — (slug, індекс появи в route.json, рахуючи з 0). Значення — дослівно
# з еталона travel202609/places/exports/route_state.json (задача явно дала ці
# числа — не виводяться, не вгадуються).
BORDER_OVERRIDES = {
    ('border-ro-ua-siret-porubne', 0):      {'label': 'Кордон UA→RO: Порубне',    'lat': 47.9877, 'lon': 26.0615},
    ('border-bg-gr-kulata-promachonas', 0): {'label': 'Кордон BG→GR: Кулата',     'lat': 41.3417, 'lon': 23.37},
    ('border-bg-gr-kulata-promachonas', 1): {'label': 'Кордон GR→BG: Промахонас', 'lat': 41.335,  'lon': 23.3689},
    ('border-ro-ua-siret-porubne', 1):      {'label': 'Кордон RO→UA: Сірет',      'lat': 47.9877, 'lon': 26.0615},
}


def parse_booking(md_text):
    """Той самий регекс, що й generate_state_from_md.py::parse_booking."""
    m = re.search(r'\*\*Бронь\*\*\s*\|\s*([^|]+)', md_text)
    return m.group(1).strip() if m else ''


def load_bookings_by_slug(slugmap):
    """slug → [текст Бронь, ...] у дорожньому порядку появ.

    Порядок файлів для злитих карток береться зі slugmap['merge'] (там він
    явно дорожній — «0200.md», «1900.md»). Для карток без злиття — єдиний
    файл зі slugmap['route']. Slug без жодного тексту Бронь у джерелах у
    результат не потрапляє (кордони, home-точки тощо).
    """
    route_map = slugmap['route']          # {"0200.md": "ua-chernivtsi", ...}
    merge     = slugmap.get('merge', {})  # {"ua-chernivtsi": ["0200.md","1900.md"], ...}

    slug_files = {}
    for fname, slug in route_map.items():
        slug_files.setdefault(slug, []).append(fname)
    for slug, files in merge.items():
        slug_files[slug] = list(files)    # дорожній порядок — авторитетний

    result = {}
    for slug, files in slug_files.items():
        texts = []
        for fname in files:
            path = OLD_PLACES / fname
            if not path.exists():
                continue
            text = parse_booking(path.read_text(encoding='utf-8'))
            if text:
                texts.append(text)
        if texts:
            result[slug] = texts
    return result


def main():
    dry_run = '--dry-run' in sys.argv

    slugmap = json.loads(SLUGMAP.read_text(encoding='utf-8'))
    bookings_by_slug = load_bookings_by_slug(slugmap)

    route = CTX.read_json(ROUTE_JSON)

    occurrence_seen = {}  # slug -> скільки появ уже пройдено
    n_booking = 0
    n_override = 0

    for day in route.get('days', []):
        for pt in day.get('points', []):
            slug = pt['id']
            idx = occurrence_seen.get(slug, 0)
            occurrence_seen[slug] = idx + 1

            texts = bookings_by_slug.get(slug)
            if texts:
                # Якщо появ більше за тексти в групі — останній текст на решту
                # («той самий текст на обидві», якщо в групі один файл).
                text = texts[idx] if idx < len(texts) else texts[-1]
                pt['booking_note'] = text
                n_booking += 1
                print(f"  [booking ] {slug} #{idx}: {text!r}")

            override = BORDER_OVERRIDES.get((slug, idx))
            if override:
                pt.update(override)
                n_override += 1
                print(f"  [override] {slug} #{idx}: {override}")

    print(f"\nbooking_note: {n_booking} появ")
    print(f"label/lat/lon overrides: {n_override} появ")

    if dry_run:
        print("[DRY RUN] route.json не записано")
        return

    CTX.write_json(ROUTE_JSON, route)
    print(f"Записано → {ROUTE_JSON}")


if __name__ == '__main__':
    main()
