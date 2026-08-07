"""
scripts/find_poi_photos.py

Заповнює `?` у колонці «Фото» таблиці «Цікаві POI» та категорій із фото
(trip.json → poi_categories) — через тул `find_photos` віддаленого воркера
(Wikimedia Commons).

ЧОМУ ПЕРЕПИСАНО. Попередня версія ходила у Wikipedia opensearch і брала
pageimage найкращого «схожого» результату. На коротких і транслітерованих
назвах це давало омоніми, бо збіг був ПІДРЯДКОВИЙ, а не по словах:

    Anogi        → Anogenital_distance_female_and_male
    Kioni        → Conic_sections
    Prionia      → Prion_subdomain_colored_sec_structure
    Bâlea Lac    → Lacus_Timoris (кратер на Місяці)
    Golden Gate  → Wpdms_usgs_photo_golden_gate (міст у Сан-Франциско)

З 110 знайдених фото 25 виявились чужими. Тому тепер:

1. Пошук — `find_photos` воркера: шукає САМЕ по Commons і повертає до
   5 кандидатів з готовим `/500px-` thumb_url.
2. **Фільтр релевантності** — головне. Кандидат приймається, лише якщо назва
   файла містить хоча б одне ЦІЛЕ характерне слово з назви POI. Порівняння
   по токенах, не по підрядках: `anogenital` більше не «схоже» на `anogi`.
3. Глобальна дедуплікація: один URL не ставиться двом різним POI.

Запит будується як «латинська назва POI + місто» — на Commons латиниця
працює краще за кирилицю. Місто/країна — з власної картки місця (H1 + поле
**Код**), а НЕ з фіксованої таблиці: файли бібліотеки — slug-картки
(`cc-example-town.md`), не старі `XXXX.md`, тому будь-яка таблиця «файл → місто»
неминуче старіє і належить даним, а не тулкіту.

Для POI, чию назву автозапит не вгадає (суто кириличні POI без латинської
форми в дужках), раніше був QUERY_OVERRIDES — ручний список запитів на
30+ POI старого маршруту. Він видалений: увесь прив'язаний до старих
`XXXX.md` файлів, під сучасну бібліотеку жоден рядок уже не спрацьовує
(глоб нижче їх просто не знаходить), а ручні підказки для НОВИХ POI — це
знання конкретної поїздки, йому місце в даних (напр. окремим полем
у таблиці POI картки), не в коді тулкіта.

Run (потрібен токен у .env):
    PYTHONPATH=scripts python3 scripts/find_poi_photos.py --dry-run
    PYTHONPATH=scripts python3 scripts/find_poi_photos.py
    PYTHONPATH=scripts python3 scripts/find_poi_photos.py --only cc-example-town.md
"""
import json
import re
import sys
import time
import unicodedata
import urllib.request
from pathlib import Path

from cloud_push import get_access_token, get_worker, load_env_file

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trip_ctx import paths
CTX = paths()

PLACES = CTX.places_dir
PLACE_META_SKIP = {"country_info.md", "practical_info.md", "CATALOG.md"}

# Універсальний факт (код країни ISO 3166-1 → англійська назва), не дані
# конкретної поїздки — потрібен лише щоб дати Commons запит англійською,
# на якій пошук працює найкраще.
COUNTRY_EN = {
    "UA": "Ukraine", "RO": "Romania", "BG": "Bulgaria", "GR": "Greece",
    "MK": "North Macedonia", "RS": "Serbia", "HU": "Hungary", "PL": "Poland",
    "AL": "Albania", "MD": "Moldova",
}


def card_city_context(card_text):
    """H1 картки → (коротка латинська назва, повний контекст «назва країна»).

    H1 виду «Назва / Name (уточнення)» — беремо ОСТАННІЙ сегмент після
    ` / ` (латинська форма за конвенцією карток), відкидаємо дужки для
    короткої назви. **Код** дає країну (ISO), звідки — англійська назва
    з фіксованого (не трипового) словника вище.
    """
    m = re.match(r"^#\s+(.+)$", card_text, re.MULTILINE)
    if not m:
        return "", ""
    parts = [p.strip() for p in m.group(1).split(" / ")]
    latin = next((p for p in reversed(parts)
                  if re.search(r"[A-Za-zÀ-ž]", p) and not re.search(r"[а-яА-ЯіІїЇєЄґҐ]", p)),
                 parts[-1])
    short = re.sub(r"\s*\([^)]*\)", "", latin).strip()
    cc_m = re.search(r"\*\*Код\*\*\s*\|\s*([A-Z]{2})", card_text)
    country = COUNTRY_EN.get(cc_m.group(1), "") if cc_m else ""
    ctx = f"{short} {country}".strip()
    return short, ctx

# Слова, які не вважаємо характерними — вони є в половині Commons.
# Родові слова: самі по собі НЕ підтверджують, що це те саме місце.
# «cave» збігається і з печерою Німф, і з картиною «Cave of the Storm».
STOP = {
    "the", "of", "and", "in", "at", "de", "la", "din", "national", "old",
    "new", "great", "big", "city", "town", "street", "square", "church", "saint",
    "monastery", "castle", "fortress", "tower", "park", "bridge", "house",
    "museum", "romania", "bulgaria", "greece", "ukraine", "romanian", "greek",
    "view", "panorama", "centre", "center", "file", "jpg", "jpeg", "png",
    "cave", "lake", "mount", "mountain", "gorge", "waterfall", "falls", "beach",
    "port", "harbour", "harbor", "village", "hill", "dam", "statue", "fountain",
    "market", "quarter", "cathedral", "palace", "gate", "wall", "road", "tunnel",
    "pool", "pools", "bastion", "citadel", "clock", "school", "home", "wine",
    "refuge", "shelter", "cabana", "hut", "lac", "lake",
    "honey", "ice", "cream", "gelato", "main", "upper", "lower", "north", "south",
}


def strip_diacritics(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def tokens(s: str) -> set:
    s = strip_diacritics(s).lower().replace("ș", "s").replace("ț", "t")
    return {t for t in re.split(r"[^a-z0-9]+", s) if len(t) >= 3}


def latin_variants(poi: str) -> list:
    """«Печера Німф (Marmarospilia / Cave of the Nymphs)» →
       ['Marmarospilia', 'Cave of the Nymphs'].

    Саме СПИСОК, а не склейка: Commons не знаходить нічого по «Marmarospilia
    Cave of the Nymphs», але знаходить по кожній частині окремо.
    """
    m = re.search(r"\(([^)]*[A-Za-zÀ-ž][^)]*)\)", poi)
    if not m:
        return []
    out = []
    for part in re.split(r"\s*[/,]\s*", m.group(1)):
        part = re.sub(r"\d+\s*[мm]\b", "", part).strip()
        if part and re.search(r"[A-Za-zÀ-ž]", part):
            out.append(part)
    return out


def queries(poi: str, variants: list, short: str, ctx: str) -> list:
    """Каскад запитів від найточнішого до найзагальнішого."""
    qs = []
    for v in variants:
        qs += [f"{v} {short}", v]
    base = re.sub(r"\s*\([^)]*\)", "", poi).strip()
    if base:
        qs.append(f"{base} {short}")
    if not variants:
        qs.append(f"{base} {ctx}")
    seen, out = set(), []
    for q in qs:
        q = " ".join(q.split())
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out


def call(worker, token, name, args, tries=3):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": args}}).encode()
    resp = None
    for attempt in range(tries):
        req = urllib.request.Request(worker, data=body, method="POST")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "trip-map-toolkit/1.0")
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                resp = json.load(r)
            break
        except Exception as e:
            if attempt == tries - 1:
                print(f"      ! мережа: {e}")
                return []
            time.sleep(2 * (attempt + 1))
    if not resp or "error" in resp:
        return []
    content = (resp.get("result") or {}).get("content") or []
    if not content:
        return []
    try:
        out = json.loads(content[0]["text"])
        return out if isinstance(out, list) else []
    except json.JSONDecodeError:
        return []


def stem(t: str) -> str:
    """Легка нормалізація множини: caves→cave, churches→church."""
    if t.endswith("es") and len(t) > 6:
        return t[:-2]
    if t.endswith("s") and len(t) > 4:
        return t[:-1]
    return t


def pick(cands, poi_name, variants, city_ctx, used):
    """Найкращий кандидат або None.

    Фільтр: збіг має бути по ЦІЛОМУ слову (не підрядку) і хоча б одне з них
    має бути СИЛЬНИМ — тобто власною назвою, а не родовим словом. Інакше
    «Cave of the Nymphs» приймало б книжку «The caves of the earth».
    """
    raw = tokens(" ".join(variants)) | tokens(poi_name)
    strong = {stem(t) for t in raw - STOP if len(t) >= 4}
    weak = {stem(t) for t in raw & STOP}
    city = {stem(t) for t in tokens(city_ctx) - STOP}
    best, best_score = None, 0
    for c in cands:
        url = (c.get("thumb_url") or "").strip()
        if not url.startswith("http") or url in used:
            continue
        title = c.get("title") or ""
        if re.search(r"\.(pdf|djvu|svg|ogg|webm|tif)\b", title, re.I):
            continue
        tt = {stem(t) for t in tokens(title)}
        hit_s = strong & tt
        # Місто звіряємо по префіксу: транслітерації часто різняться в
        # останніх літерах (грецьке -os/-us, румунське діакритичне ș/s тощо).
        # Для власних назв такої поблажки НЕ робимо: «prion» не має стати «prionia».
        hit_c = {c for c in city if len(c) >= 4
                 and any(t[:4] == c[:4] for t in tt if len(t) >= 4)}
        # Власна назва обовʼязкова, і її треба ПІДТВЕРДИТИ: або друга власна
        # назва, або назва міста в заголовку. Одного слова замало — саме так
        # «Rovani» підчепив фото людини з таким прізвищем, а «Cave» — картину.
        # Рідкісне довге слово (≥7 літер) підтверджує себе саме: «prionia»,
        # «marmarospilia», «postavarul» не бувають випадковими збігами.
        rare = any(len(t) >= 7 for t in hit_s)
        if not hit_s or (len(hit_s) < 2 and not hit_c and not rare):
            continue
        score = 3 * len(hit_s) + 2 * len(hit_c) + len(weak & tt)
        if score > best_score:
            best, best_score = c, score
    return best


def main():
    dry = "--dry-run" in sys.argv
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None

    env = load_env_file()
    worker = get_worker(env)
    token = get_access_token(env, worker)

    place_files = sorted(f for f in PLACES.glob("*.md") if f.name not in PLACE_META_SKIP)

    used = set()
    for p in place_files:
        used.update(re.findall(r"`(https://upload\.wikimedia\.org/[^`]+)`",
                               p.read_text(encoding="utf-8")))
    print(f"вже використано URL: {len(used)}\n")

    found = missed = 0
    for path in place_files:
        if only and path.name != only:
            continue
        card_text = path.read_text(encoding="utf-8")
        short_name, city_ctx = card_city_context(card_text)
        if not city_ctx:
            continue
        lines = card_text.splitlines(keepends=True)
        touched = False

        for i, line in enumerate(lines):
            if not line.startswith("|"):
                continue
            cells = line.split("|")
            if len(cells) < 6 or cells[4].strip() != "?":
                continue
            poi = re.sub(r"\*\*", "", cells[1]).strip()
            if not poi or poi == "Назва":
                continue

            variants = latin_variants(poi)
            short = short_name
            qlist = queries(poi, variants, short, city_ctx)

            best = None
            for q in qlist:
                best = pick(call(worker, token, "find_photos", {"query": q}),
                            poi, variants, city_ctx, used)
                if best:
                    break
                time.sleep(0.2)
            if best:
                used.add(best["thumb_url"])
                cells[4] = f" `{best['thumb_url']}` "
                lines[i] = "|".join(cells)
                touched = True
                found += 1
                print(f"  ✓ {path.name} · {poi[:30]:<30} → "
                      f"{best['title'].replace('File:', '')[:44]}")
            else:
                missed += 1
                print(f"  — {path.name} · {poi[:30]:<30} (нічого доречного)")
            time.sleep(0.3)

        if touched and not dry:
            path.write_text("".join(lines), encoding="utf-8")

    print(f"\n{'[DRY RUN] ' if dry else ''}знайдено {found}, без результату {missed}")
    if not dry and found:
        print("Далі: python3 scripts/generate_state.py")


if __name__ == "__main__":
    main()
