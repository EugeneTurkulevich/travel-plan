"""
scripts/patch_route_bookings.py

Одноразовий патч-скрипт, що закриває дірку в специфікації Ф3: migrate_cards.py
видалив рядок `**Бронь**` із карток бібліотеки, не переклавши його в
route.json, а обидва напрямки кожного двобічного кордону звели в одну
нейтральну картку, втративши напрямково-специфічні label/координати.

Бронювання і напрямок кордону — властивості КОНКРЕТНОЇ ПОЯВИ точки в
маршруті, а не самого місця (місце проміжної ночівлі при заїзді й виїзді
може з'являтись двічі з різним текстом Бронь; кожен кордон — двічі, у різні
боки). Тому обидва патчаться в route.json на рівні точки, а не в картці
бібліотеки.

Джерела — ТІЛЬКИ ЧИТАННЯ:
  - <--source>/places/*.md                           (архівні картки старої
    моделі — звідти читається рядок `| **Бронь** | … |`)
  - travel-data/_migration/slugmap.json              (file → slug, злиття,
    напрямкові перекриття label/lat/lon для кордонів — `border_overrides`)

Пишеться:
  - route.json АКТИВНОГО профілю (через trip_ctx) — додається `booking_note`
    на кожну появу, де джерело мало Бронь, і `label`/`lat`/`lon` — на появи
    кордонів за `slugmap.json → border_overrides` (значення там узяті
    дослівно з еталона старого репо, за вказівкою власника даних). Решта
    route.json не чіпається — файл читається, точково допатчується, пишеться
    назад, а не збирається з нуля.

Ідемпотентний: другий прогін дає той самий route.json.

Usage:
    python3 scripts/patch_route_bookings.py --source ~/GitHub/travel202609
    python3 scripts/patch_route_bookings.py --source ~/GitHub/travel202609 --dry-run
"""
import argparse
import json, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trip_ctx import paths
CTX = paths()

SLUGMAP    = CTX.data_root / '_migration' / 'slugmap.json'
ROUTE_JSON = CTX.route_json


def load_border_overrides(slugmap):
    """slugmap.json → border_overrides: [{"slug","occurrence","label","lat",
    "lon"}, ...] → {(slug, occurrence): {"label","lat","lon"}}.

    Появи кордонів, що потребують напрямкового перекриття label/lat/lon —
    рішення КОНКРЕТНОЇ міграції (координати взяті дослівно з еталона
    старого репозиторію), тому в даних (slugmap.json), а не літералом тут
    (STRUCTURE_PROPOSAL §7). occurrence — індекс появи в route.json,
    рахуючи з 0."""
    out = {}
    for row in slugmap.get('border_overrides', []):
        out[(row['slug'], row['occurrence'])] = {
            'label': row['label'], 'lat': row['lat'], 'lon': row['lon'],
        }
    return out


def parse_booking(md_text):
    """Той самий регекс, що й generate_state.py::parse_booking (портовано з
    первісного MD-генератора, з тих пір видаленого — Ф4)."""
    m = re.search(r'\*\*Бронь\*\*\s*\|\s*([^|]+)', md_text)
    return m.group(1).strip() if m else ''


def load_bookings_by_slug(slugmap, old_places):
    """slug → [текст Бронь, ...] у дорожньому порядку появ.

    Порядок файлів для злитих карток береться зі slugmap['merge'] (там він
    явно дорожній — «0200.md», «1900.md»). Для карток без злиття — єдиний
    файл зі slugmap['route']. Slug без жодного тексту Бронь у джерелах у
    результат не потрапляє (кордони, home-точки тощо).
    """
    route_map = slugmap['route']          # {"0200.md": "cc-example", ...}
    merge     = slugmap.get('merge', {})  # {"cc-example": ["0200.md","1900.md"], ...}

    slug_files = {}
    for fname, slug in route_map.items():
        slug_files.setdefault(slug, []).append(fname)
    for slug, files in merge.items():
        slug_files[slug] = list(files)    # дорожній порядок — авторитетний

    result = {}
    for slug, files in slug_files.items():
        texts = []
        for fname in files:
            path = old_places / fname
            if not path.exists():
                continue
            text = parse_booking(path.read_text(encoding='utf-8'))
            if text:
                texts.append(text)
        if texts:
            result[slug] = texts
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--source', required=True, help='корінь travel202609 (тільки читання)')
    ap.add_argument('--dry-run', action='store_true', help='нічого не писати, лише показати план')
    args = ap.parse_args()

    old_places = Path(args.source).expanduser().resolve() / 'places'
    if not old_places.exists():
        raise SystemExit(f'❌ Джерела немає: {old_places}')

    slugmap = json.loads(SLUGMAP.read_text(encoding='utf-8'))
    bookings_by_slug = load_bookings_by_slug(slugmap, old_places)
    border_overrides = load_border_overrides(slugmap)

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

            override = border_overrides.get((slug, idx))
            if override:
                pt.update(override)
                n_override += 1
                print(f"  [override] {slug} #{idx}: {override}")

    print(f"\nbooking_note: {n_booking} появ")
    print(f"label/lat/lon overrides: {n_override} появ")

    if args.dry_run:
        print("[DRY RUN] route.json не записано")
        return

    CTX.write_json(ROUTE_JSON, route)
    print(f"Записано → {ROUTE_JSON}")


if __name__ == '__main__':
    main()
