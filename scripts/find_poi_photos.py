"""
scripts/find_poi_photos.py

Заповнює `?` у колонці «Фото» таблиць POI і 🛍️ Місцева особливість —
через тул `find_photos` віддаленого воркера (Wikimedia Commons).

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
працює краще за кирилицю. Для POI без латинської назви в дужках —
QUERY_OVERRIDES.

Run (потрібен токен у .env):
    PYTHONPATH=scripts python3 scripts/find_poi_photos.py --dry-run
    PYTHONPATH=scripts python3 scripts/find_poi_photos.py
    PYTHONPATH=scripts python3 scripts/find_poi_photos.py --only 1300.md
Далі обовʼязково: python3 scripts/generate_state_from_md.py
"""
import json
import re
import sys
import time
import unicodedata
import urllib.request
from pathlib import Path

from cloud_push import get_access_token, get_worker, load_env_file

PLACES = Path("places")

# файл -> (коротка назва для запиту, повний контекст)
CITY = {
    "0100.md": ("Kyiv", "Kyiv Ukraine"),
    "0200.md": ("Chernivtsi", "Chernivtsi Ukraine"),
    "0280.md": ("Suceava", "Suceava Romania"),
    "0300.md": ("Sighisoara", "Sighisoara Romania"),
    "0320.md": ("Piatra", "Piatra Neamt Romania"),
    "0360.md": ("Lacu", "Lacu Rosu Harghita Romania"),
    "0380.md": ("Bicazului", "Cheile Bicazului Romania"),
    "0690.md": ("Valcea", "Ramnicu Valcea Romania"),
    "0720.md": ("Hurezi", "Hurezi Horezu Romania"),
    "1080.md": ("Kerkini", "Kerkini Serres Greece"),
    "1180.md": ("Chalkida", "Chalkida Evia Greece"),
    "0400.md": ("Brasov", "Poiana Brasov Postavarul Romania"),
    "0500.md": ("Brasov", "Brasov Romania"),
    "0550.md": ("Fagaras", "Fagaras Romania"),
    "0600.md": ("Sibiu", "Sibiu Romania"),
    "0650.md": ("Transfagarasan", "Balea Transfagarasan Romania"),
    "0680.md": ("Vidraru", "Vidraru Poenari Arges Romania"),
    "0700.md": ("Arges", "Curtea de Arges Romania"),
    "0750.md": ("Craiova", "Craiova Romania"),
    "0900.md": ("Belogradchik", "Belogradchik Bulgaria"),
    "1000.md": ("Bansko", "Bansko Pirin Bulgaria"),
    "1100.md": ("Litochoro", "Litochoro Olympus Greece"),
    "1150.md": ("Olympus", "Mount Olympus Greece"),
    "1200.md": ("Arta", "Arta Epirus Greece"),
    "1250.md": ("Astakos", "Astakos Aetolia Greece"),
    "1300.md": ("Ithaca", "Ithaca Ionian Greece"),
    "1400.md": ("Papingo", "Papingo Zagori Vikos Greece"),
    "1500.md": ("Edessa", "Edessa Macedonia Greece"),
    "1600.md": ("Melnik", "Melnik Bulgaria"),
    "1700.md": ("Koprivshtitsa", "Koprivshtitsa Bulgaria"),
    "1800.md": ("Bucharest", "Bucharest Romania"),
    "1900.md": ("Chernivtsi", "Chernivtsi Ukraine"),
}

# POI без латинської назви в дужках — задаємо запит вручну.
QUERY_OVERRIDES = {
    ("0500.md", "Strada Sforii"): "Strada Sforii Brasov",
    ("0500.md", "Strada Republicii"): "Strada Republicii Brasov",
    ("0550.md", "Рів і вали"): "Fagaras fortress moat Romania",
    ("0600.md", "Джелато на Piața Mare"): "Piata Mare Sibiu",
    ("0600.md", "Парк Дубрава (Pădurea Dumbrava)"): "Dumbrava forest Sibiu",
    ("0650.md", "Північний серпантин (Serpentinele Nordice)"):
        "Transfagarasan serpentine road Romania",
    ("0680.md", "Статуя Прометея (Prometeu)"): "Prometeu statue Vidraru Romania",
    ("0700.md", "Криниця Манолє (Fântâna Meșterului Manole)"):
        "Fantana Mesterului Manole Curtea de Arges",
    ("0700.md", "Руїни княжого двору (Curtea Domnească)"):
        "Curtea Domneasca Curtea de Arges",
    ("0900.md", "Оглядовий майданчик фортеці"): "Belogradchik fortress rocks view",
    ("1000.md", "Вулиці Старого Банско"): "Bansko old town houses Bulgaria",
    ("1100.md", "Вид на Олімп (Mount Olympus viewpoint)"): "Olympus Litochoro Greece",
    ("1150.md", "Мітікас (Mytikas, 2917 м)"): "Mytikas Olympus summit Greece",
    ("1150.md", "Притулок Спіліос Агапітос (Refuge A, 2100 м)"):
        "Spilios Agapitos refuge Olympus",
    ("1250.md", "Порт Астакос (Astakos Port)"): "Astakos port harbour Greece",
    ("1250.md", "Набережна Астакоса (Paralia)"): "Astakos Greece waterfront",
    ("1300.md", "Школа Гомера (School of Homer)"): "School of Homer Stavros Ithaca",
    ("1500.md", "Квартал млинів біля водоспадів"): "Edessa watermills Greece",
    ("1600.md", "Винні підвали Мелника"): "Melnik wine cellar Bulgaria",
    ("1700.md", "Церква Успіння Богородиці"): "Koprivshtitsa church Bulgaria",
    ("1800.md", "Парк Херестреу (Parcul Herăstrău / Regele Mihai I)"):
        "Herastrau park Bucharest",
    ("0280.md", "Оглядова з валів цитаделі"): "Cetatea Suceava fortress",
    ("0320.md", "Телегондола на Козлу"): "Telegondola Piatra Neamt",
    ("0320.md", "Княжий двір і церква Св. Іоанна"): "Biserica Sfantul Ioan Piatra Neamt",
    ("0320.md", "Дзвіниця княжого двору"): "Turnul lui Stefan Piatra Neamt",
    ("0320.md", "Пішохідна вул. Штефана Великого"): "Piatra Neamt centru",
    ("0360.md", "Червоне озеро (Lacu Roșu)"): "Lacu Rosu Romania",
    ("0380.md", "Горло пекла (Gâtul Iadului)"): "Cheile Bicazului gorge",
    ("0380.md", "Ринок ремісників в ущелині"): "Cheile Bicazului Romania",
    ("0690.md", "Центральний парк Зевзеконія"): "Ramnicu Valcea park",
    ("0690.md", "Пішохідна вул. Траян (Calea lui Traian)"): "Calea lui Traian Ramnicu Valcea",
    ("0720.md", "Розписана галерея собору"): "Manastirea Hurezi",
    ("0720.md", "Керамічні майстерні Хорезу"): "ceramica Horezu",
    ("1080.md", "Човнова прогулянка по озеру"): "Kerkini lake boat",
    ("1080.md", "Водяні буйволи Керкіні"): "Kerkini buffalo",
    ("1180.md", "Набережна Кріазотоу (Παραλία Κριεζώτου)"): "Chalkida Greece bridge",
    ("1180.md", "Протока Евріп і старий міст"): "Chalkida Euripus",
    ("1180.md", "Фортеця Караბаба (Κάστρο Καράμπαμπα)"): "Karababa fortress",
    ("1180.md", "Церква Св. Параскеви"): "Agia Paraskevi Chalkida",
    # Додано після другого проходу: POI з суто кириличними назвами, де
    # автозапит не працює. Кожен запит перевірено вручну через find_photos.
    ("0750.md", "Парк Ніколає Романеску"): "Parcul Nicolae Romanescu Craiova",
    ("0750.md", "Каля Уніріі"): "Calea Unirii Craiova",
    ("1200.md", "Міст Арти (Gefyri tis Artas)"): "Bridge of Arta",
    ("1200.md", "Замок Арти (Kastro Artas)"): "Arta castle Greece",
    ("1400.md", "Мегало і Мікро Папінго"): "Papingo village Zagori",
    ("0400.md", "Канатка Пояна-Брашов → Крістіанул-Маре"): "Poiana Brasov cable car",
    ("0650.md", "Північний серпантин (Serpentinele Nordice)"):
        "Transfagarasan road Romania",
    ("0900.md", "Оглядовий майданчик фортеці"): "Belogradchik rocks",
    ("0300.md", "Церква домініканського монастиря"): "Sighisoara monastery church",
    ("1250.md", "Порт Астакос (Astakos Port)"): "Astakos Greece",
}

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
        req.add_header("User-Agent", "travel202609-cloud-push/1.0")
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
        # Місто звіряємо по префіксу: Olympus/Olympos, Arges/Argeș — це те саме.
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

    used = set()
    for p in PLACES.glob("[0-9][0-9][0-9][0-9].md"):
        used.update(re.findall(r"`(https://upload\.wikimedia\.org/[^`]+)`",
                               p.read_text(encoding="utf-8")))
    print(f"вже використано URL: {len(used)}\n")

    found = missed = 0
    for path in sorted(PLACES.glob("[0-9][0-9][0-9][0-9].md")):
        if only and path.name != only:
            continue
        short_name, city_ctx = CITY.get(path.name, ("", ""))
        if not city_ctx:
            continue
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
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
            override = QUERY_OVERRIDES.get((path.name, poi))
            if override:
                # Запит написаний людиною — беремо його слова як характерні,
                # інакше для суто кириличних назв «сильних» токенів немає взагалі.
                qlist, variants = [override], [override]
            else:
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
        print("Далі: python3 scripts/generate_state_from_md.py")


if __name__ == "__main__":
    main()
