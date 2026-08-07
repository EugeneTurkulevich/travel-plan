"""
scripts/check_no_data.py

Гейт публічності (STRUCTURE_PROPOSAL.md §7, CLAUDE.md §0). Сканує ЦЕЙ
репозиторій (не ../travel-data) і падає, якщо в ньому лежить щось, що має
жити в даних: map_id, назви місць, категорії POI, дати конкретної поїздки,
абсолютні шляхи користувача, секрети.

ЧОМУ ТАК, А НЕ СЛОВНИКОМ МІСТ. Фіксований список географічних назв або
категорій POI старіє й дає або шум (будь-яке слово «схоже на місто»), або
тишу (нова поїздка — нові назви, яких у словнику нема). Натомість гейт
ЧИТАЄ дані поточної людини (../travel-data/places/*.md, trips/*/trip.json)
і шукає в репозиторії РІВНО ті назви/категорії/дати, які там знайшлись.
Це ловить точно свої дані, а не абстрактну «схожість». Якщо даних поруч
немає (чистий клон без ../travel-data) — ці перевірки чесно ПРОПУСКАЮТЬСЯ
з попередженням, а не мовчки вимикаються: чужа людина без даних мусить
отримати зелений гейт, а не помилку.

Статичні перевірки (не залежать від даних, працюють завжди):
  - рядок, схожий на map_id: 20 символів [a-z0-9] поспіль, і цифра, і літера
  - абсолютний шлях до домівки конкретного користувача (macOS/Linux) —
    видає, хто саме запускав скрипт, і завжди неправильний в іншого клона
  - ознаки картки місця: заголовок секції POI-таблиці бібліотеки, рядок
    метаданих з координатами, пара lat/lon у клітинці таблиці
  - секрети/експортований стан за іменем файлу: .env (не .env.example),
    .cloud-draft-id, live_mirror.json, *.cache.json, шлях під exports/

Перевірки на основі даних (ПРОПУСКАЮТЬСЯ, якщо ../travel-data не знайдено):
  - географічні назви: з H1 карток бібліотеки (двомовний заголовок картки,
    напр. «Назва / Name») і рядків метаданих `ID`
  - категорії POI цієї поїздки: іконка/секція з trip.json → poi_categories
    (СЛОВО В СЛОВО з STRUCTURE_PROPOSAL §Н3 — вони довільні, тому їх треба
    читати з даних, а не тримати фіксованим списком емодзі-піктограм)
  - дати поїздки: кожен день у вікні trip.json.start…end, у форматах
    YYYY-MM-DD і день.місяць без року

СКОУП СКАНУВАННЯ. Не весь диск і не все дерево репо — файли, які
опублікуються: `git ls-files --cached --others --exclude-standard`
(трекнуті + нові файли, яких git НЕ ігнорує). Якщо репозиторій не під git —
резервний обхід дерева з жорстким списком пропусків (див. FALLBACK_SKIP_*).
Це навмисно: `__pycache__/*.pyc` містить сирі рядки колишніх констант,
`.travel-local.json` — приватний шлях, `.DS_Store` — бінарник; жоден з них
не публікується, тож сканувати їх — це або хибний позитив, або крах
рідера. Секретні файли (.env тощо) гітигнорені й тому не бачить git
ls-files — це й є доказ, що вони не потраплять у комміт; перевірка їхньої
відсутності — на випадок, якщо хтось обійде .gitignore через `git add -f`
(тоді `--cached` таки їх покаже).

Usage:
    python3 scripts/check_no_data.py           # звіт людині + код виходу
    python3 scripts/check_no_data.py --json     # JSON-звіт у stdout

Код виходу: 0 — чисто, 1 — є знахідки.
"""
import argparse
import ast
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trip_ctx import resolve_data_root, list_trips, TripContextError  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

# ── явні виключення (постановка задачі) ─────────────────────────────────────
# Кожне з поясненням — мовчазних пропусків тут бути не може.
EXCLUDED_FILES = {
    "STRUCTURE_PROPOSAL.md": "документ міграції — навмисно описує конкретний випадок переходу (§0 постановки)",
    "MIGRATION_PLAN.md":     "документ міграції — навмисно описує конкретний випадок переходу (§0 постановки)",
}
EXCLUDED_PREFIXES = {
    "examples/": "навмисно вигадані демонстраційні дані, не мої",
}

# Точкові виключення — вужчі за файлові, для одного задокументованого
# випадку: МЕХАНІЗМ (не дані) текстово збігається з даними поточної людини.
# Не спосіб "мовчки заткнути гейт" — кожен запис із посиланням на архітектурне
# рішення, яке пояснює, чому саме тут це не витік.
#
# generate_state.py TYPE_NAMES — фіксований словник type_key → підпис, який
# показує МЕХАНІЗМ попапу (STRUCTURE_PROPOSAL.md §Н3: «type_name лишається
# зашитим» — той самий текст видавав ще попередній, вже видалений MD-
# генератор, задовго до цього профілю). Значення незалежні від trip.json і НЕ
# можуть змінитись під гейт: зміна вплинула б на type_name в route_state.json
# (критерій приймання 5 — побайтова рівність). Те, що частина значень
# текстово збігається із секціями САМЕ ЦІЄЇ поїздки, — випадковість
# природної мови (людина назвала MD-секцію тим самим словом, що й
# фіксований підпис), а не витік.
#
# Не (файл, номер рядка): TYPE_NAMES — суцільний блок, і номер рядка
# зсувається від будь-якої правки вище у файлі (уже траплялось під час
# розробки цього ж гейту — тихо переставив би виключення на сусідній, не
# той рядок). І не точний текст збігу: harvest_poi_categories() бере
# коротший підпис секції з trip.json.poi_categories.<key>.section, а
# TYPE_NAMES у generate_state.py може нести повнішу форму того самого
# підпису (напр. з уточнювальним словом) — знайдений токен трапляється як
# ПІДРЯДОК значення TYPE_NAMES, не обов'язково дослівно рівним рядком. Тому
# предикат нижче — "чи є ЗНАЙДЕНИЙ токен підрядком одного зі значень
# TYPE_NAMES у файлі й категорії poi_category" — а не рівність кортежів.
#
# Самі значення для звірки НЕ задубльовані тут літералом: буквальний дубль
# зробив би САМЕ ЦЕЙ файл другим джерелом тих самих слів (гейт зловив би
# сам себе на цьому ж рядку), а розбіжність зі справжнім TYPE_NAMES після
# майбутньої правки лишилась би непоміченою. Замість цього множина значень
# читається нижче з АСТ generate_state.py — без імпорту (нема побічних
# ефектів на кшталт резолву кореня даних) і без буквального повторення
# тексту.
def _type_names_span():
    """(значення TYPE_NAMES, перший рядок, останній рядок) — межі СУЦІЛЬНОГО
    блоку словника беремо з самого вузла АСТ (node.lineno/end_lineno), а не
    захардкожуємо: тоді межі самі рухаються разом з майбутнім рефакторингом
    файлу і виняток лишається прив'язаним до РЕАЛЬНОГО місця визначення."""
    gs_path = REPO_ROOT / "scripts" / "generate_state.py"
    try:
        tree = ast.parse(gs_path.read_text(encoding="utf-8"), filename=str(gs_path))
    except (OSError, SyntaxError):
        return set(), None, None
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "TYPE_NAMES" for t in node.targets)
                and isinstance(node.value, ast.Dict)):
            continue
        try:
            values = ast.literal_eval(node.value)
        except ValueError:
            return set(), None, None
        # "poi" — базова категорія, теж фіксована, але з trip.json не перетинається; лишаємо для повноти, шкоди нема
        return set(values.values()), node.lineno, node.end_lineno
    return set(), None, None


_TYPE_NAMES_VALUES, _TYPE_NAMES_LINE_START, _TYPE_NAMES_LINE_END = _type_names_span()


def is_type_names_exemption(f):
    """Чи ця конкретна знахідка — той самий задокументований збіг
    TYPE_NAMES generate_state.py з категорією поточної поїздки (див.
    коментар вище). Вузько:
      - тільки файл generate_state.py,
      - тільки category poi_category,
      - тільки РЯДОК У МЕЖАХ визначення самого словника TYPE_NAMES (не будь-
        де у файлі — інакше справжній витік в іншому місці файлу, який
        текстово випадково збігся б з одним із цих слів, теж мовчки пройшов
        би як «виняток»),
      - і тільки коли знайдений токен — справді підрядок одного зі значень
        фіксованого словника (не просто "десь у файлі щось збіглось")."""
    if f["file"] != "scripts/generate_state.py" or f["category"] != "poi_category":
        return False
    if _TYPE_NAMES_LINE_START is None:
        return False
    if not (_TYPE_NAMES_LINE_START <= f["line"] <= _TYPE_NAMES_LINE_END):
        return False
    return any(f["match"] in v for v in _TYPE_NAMES_VALUES)

# Резервний обхід дерева, коли репозиторій не під git (див. докстрінг).
FALLBACK_SKIP_DIRS = {".git", "__pycache__"}
FALLBACK_SKIP_SUFFIXES = {".pyc"}
FALLBACK_SKIP_NAMES = {".DS_Store", ".travel-local.json"}

# Секретні / експортовані файли, яким не місце в репо (STRUCTURE_PROPOSAL §7).
# Перевіряються за іменем/шляхом, не за вмістом.
SECRET_EXACT_NAMES = {".env", ".cloud-draft-id", "live_mirror.json"}
SECRET_NAME_SUFFIXES = (".cache.json",)
SECRET_PATH_PARTS = {"exports"}

MAP_ID_RE = re.compile(r"(?<![a-z0-9])[a-z0-9]{20}(?![a-z0-9])")
ABS_PATH_RE = re.compile(r"(?:/Users|/home)/[^\s\"'`)]+")
CARD_HEADING_RE = re.compile(r"^##\s*Цікаві POI\s*$")
# Пара lat/lon ЗІ СПРАВЖНІМИ числами — не мітка "**Координати**" сама по
# собі: генератори карток (sync_from_map.py, migrate_cards.py) легітимно
# пишуть рядок `f"| **Координати** | {lat}, {lon} |"` як частину МЕХАНІЗМУ
# запису картки — це код, не вставлені дані. Справжні координати завжди
# несуть числа поряд; змінна назва {lat} — ні. Тому ловимо тільки числову
# пару, а не саму мітку.
CARD_COORD_PAIR_RE = re.compile(r"(?<![\d.])-?\d{1,3}\.\d{3,},\s*-?\d{1,3}\.\d{3,}(?![\d.])")

PLACE_META_SKIP = {"country_info.md", "practical_info.md", "CATALOG.md"}


# ── файловий скоуп ───────────────────────────────────────────────────────────

def repo_files():
    """Список файлів, які РЕАЛЬНО опублікуються, відносні до REPO_ROOT."""
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "--cached", "--others", "--exclude-standard"],
            capture_output=True, text=True, check=True,
        )
        return sorted(set(line for line in out.stdout.splitlines() if line))
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    result = []
    for p in REPO_ROOT.rglob("*"):
        if p.is_dir():
            continue
        rel = p.relative_to(REPO_ROOT)
        if any(part in FALLBACK_SKIP_DIRS for part in rel.parts):
            continue
        if p.suffix in FALLBACK_SKIP_SUFFIXES or p.name in FALLBACK_SKIP_NAMES:
            continue
        if p.name == ".env":  # .env.example лишається
            continue
        result.append(str(rel))
    return sorted(result)


def is_excluded(rel_path):
    if rel_path in EXCLUDED_FILES:
        return EXCLUDED_FILES[rel_path]
    for prefix, why in EXCLUDED_PREFIXES.items():
        if rel_path.startswith(prefix):
            return why
    return None


def read_text(path):
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None  # бінарник або нечитабельний — не текстова перевірка


# ── дані: харвест назв/категорій/дат поточної людини ────────────────────────

def _split_h1_names(h1):
    """H1 картки виду «Назва / Name» → ['Назва', 'Name'] (обидві мовні форми)."""
    parts = [p.strip() for p in h1.split(" / ")]
    return [p for p in parts if len(p) > 3]


def harvest_geo_names(data_root):
    """{name: чому (звідки)} — з H1 і рядків ID усіх карток бібліотеки."""
    names = {}
    places_dir = data_root / "places"
    if not places_dir.is_dir():
        return names
    for card in sorted(places_dir.glob("*.md")):
        if card.name in PLACE_META_SKIP:
            continue
        text = read_text(card)
        if not text:
            continue
        m = re.match(r"^#\s+(.+)$", text, re.MULTILINE)
        if m:
            for name in _split_h1_names(m.group(1)):
                names.setdefault(name, f"H1 картки {card.name}")
        idm = re.search(r"\*\*ID\*\*\s*\|\s*`?([a-z0-9-]+)`?", text)
        if idm and len(idm.group(1)) > 3:
            names.setdefault(idm.group(1), f"рядок ID картки {card.name}")
    return names


def harvest_poi_categories(data_root):
    """{token: чому} — іконки й назви секцій з trip.json → poi_categories
    усіх профілів (СТРУКТУРА §Н3: категорії довільні й належать даним)."""
    tokens = {}
    for trip_id, trip in list_trips(data_root):
        for key, cfg in (trip.get("poi_categories") or {}).items():
            icon = cfg.get("icon")
            section = cfg.get("section")
            if icon:
                tokens.setdefault(icon, f"poi_categories.{key}.icon у trip.json ({trip_id})")
            if section and len(section) > 2:
                tokens.setdefault(section, f"poi_categories.{key}.section у trip.json ({trip_id})")
    return tokens


def harvest_trip_dates(data_root):
    """{рядок-дата: чому} — кожен день вікна start…end усіх профілів,
    у форматах YYYY-MM-DD і день.місяць без року (саме в такому вигляді
    дати трапляються в коді, коли їх не виводять з даних)."""
    dates = {}
    for trip_id, trip in list_trips(data_root):
        start_s, end_s = trip.get("start"), trip.get("end")
        if not start_s or not end_s:
            continue
        try:
            start = datetime.date.fromisoformat(start_s)
            end = datetime.date.fromisoformat(end_s)
        except ValueError:
            continue
        if end < start or (end - start).days > 366:
            continue  # захист від зіпсованих даних, не наша турбота тут
        d = start
        while d <= end:
            iso = d.isoformat()
            ddmm = f"{d.day:02d}.{d.month:02d}"
            dates.setdefault(iso, f"день поїздки {trip_id} ({start_s}–{end_s})")
            dates.setdefault(ddmm, f"день поїздки {trip_id} ({start_s}–{end_s})")
            d += datetime.timedelta(days=1)
    return dates


# ── перевірки одного файлу ───────────────────────────────────────────────────

def scan_lines(rel_path, text, matcher, category, why_fn):
    findings = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in matcher(line):
            findings.append({
                "file": rel_path, "line": lineno, "category": category,
                "match": m, "why": why_fn(m),
            })
    return findings


def check_map_id(rel_path, text):
    def matcher(line):
        for m in MAP_ID_RE.finditer(line):
            s = m.group(0)
            if any(c.isdigit() for c in s) and any(c.isalpha() for c in s):
                yield s
    return scan_lines(rel_path, text, matcher, "map_id",
                       lambda s: f"«{s}» — 20 символів [a-z0-9] з цифрами й літерами, "
                                 "виглядає як map_id. map_id живе в trip.json ../travel-data, "
                                 "не в репозиторії (STRUCTURE_PROPOSAL §7)")


def check_abs_path(rel_path, text):
    def matcher(line):
        for m in ABS_PATH_RE.finditer(line):
            yield m.group(0)
    return scan_lines(rel_path, text, matcher, "abs_path",
                       lambda s: f"абсолютний шлях користувача «{s}» — шляхи мають резолвитись "
                                 "через trip_ctx (--data/$TRAVEL_DATA/.travel-local.json), "
                                 "а не бути захардкоджені (CLAUDE.md §2)")


def check_card_markers(rel_path, text):
    findings = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if CARD_HEADING_RE.match(line.strip()):
            findings.append({"file": rel_path, "line": lineno, "category": "card_marker",
                              "match": line.strip(),
                              "why": "заголовок «## Цікаві POI» — це секція картки місця; "
                                     "картки живуть у ../travel-data/places/*.md"})
        for m in CARD_COORD_PAIR_RE.finditer(line):
            findings.append({"file": rel_path, "line": lineno, "category": "card_marker",
                              "match": m.group(0),
                              "why": f"пара «{m.group(0)}» схожа на координати lat, lon у "
                                     "клітинці таблиці картки — координати місця належать даним"})
    return findings


def check_secret_filenames(rel_path):
    findings = []
    p = Path(rel_path)
    reason = None
    if p.name in SECRET_EXACT_NAMES:
        reason = f"файл «{p.name}» — секрет/експортований стан (STRUCTURE_PROPOSAL §7)"
    elif p.name.endswith(SECRET_NAME_SUFFIXES):
        reason = f"файл «{p.name}» — кеш/експорт (*.cache.json), не публікується"
    elif SECRET_PATH_PARTS & set(p.parts):
        reason = f"шлях «{rel_path}» містить exports/ — експортований стан даних"
    if reason:
        findings.append({"file": rel_path, "line": 0, "category": "secret_file",
                          "match": rel_path, "why": reason})
    return findings


def _token_pattern(name):
    """Слово-межі для алфанумерних токенів (щоб коротше слово не спрацьовувало
    підрядком усередині довшого); для символів без \\w (емодзі-іконки
    категорій) межі не мають сенсу — шукаємо просто підрядок."""
    if name[:1].isalnum():
        return re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)")
    return re.compile(re.escape(name))


def check_tokens(rel_path, text, tokens, category, why_fn):
    if not tokens:
        return []
    compiled = [(name, _token_pattern(name)) for name in tokens]
    findings = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for name, pattern in compiled:
            if pattern.search(line):
                findings.append({"file": rel_path, "line": lineno, "category": category,
                                  "match": name, "why": why_fn(name, tokens[name])})
    return findings


# ── основний прогін ──────────────────────────────────────────────────────────

def run():
    findings = []
    skipped = []
    excluded_report = []

    files = repo_files()

    data_root = None
    try:
        data_root = resolve_data_root()
    except TripContextError as e:
        skipped.append("географічні назви · категорії POI · дати поїздки — "
                        f"немає кореня даних ({e}). У чужому клоні без "
                        "../travel-data це очікувано — гейт не падає на цьому.")

    geo_names, poi_tokens, trip_dates = {}, {}, {}
    if data_root is not None:
        geo_names = harvest_geo_names(data_root)
        poi_tokens = harvest_poi_categories(data_root)
        trip_dates = harvest_trip_dates(data_root)

    for rel_path in files:
        why_excl = is_excluded(rel_path)
        if why_excl is not None:
            excluded_report.append((rel_path, why_excl))
            continue

        full = REPO_ROOT / rel_path
        findings += check_secret_filenames(rel_path)

        text = read_text(full)
        if text is None:
            continue

        findings += check_map_id(rel_path, text)
        findings += check_abs_path(rel_path, text)
        findings += check_card_markers(rel_path, text)
        findings += check_tokens(
            rel_path, text, geo_names, "geo_name",
            lambda name, src: f"назва «{name}» зустрічається в бібліотеці місць ({src}) — "
                               "це дані конкретної подорожі, їм місце в ../travel-data, "
                               "не в тулкіті")
        findings += check_tokens(
            rel_path, text, poi_tokens, "poi_category",
            lambda name, src: f"«{name}» — категорія POI цієї поїздки ({src}); категорії "
                               "довільні й належать trip.json в даних, не коду тулкіта "
                               "(STRUCTURE_PROPOSAL §Н3)")
        findings += check_tokens(
            rel_path, text, trip_dates, "trip_date",
            lambda name, src: f"дата «{name}» — {src}; дата конкретної поїздки належить "
                               "route.json/trip.json в даних, не репозиторію")

    exempted = []
    kept = []
    for f in findings:
        if is_type_names_exemption(f):
            exempted.append((f, "TYPE_NAMES у generate_state.py — фіксований механізм, не дані (див. коментар вище)"))
        else:
            kept.append(f)

    return kept, skipped, excluded_report, len(files), exempted


def print_report(findings, skipped, excluded_report, n_files, exempted):
    print(f"check_no_data.py: проскановано {n_files} файлів "
          f"({len(excluded_report)} виключено явно, {len(exempted)} рядкових винятків)")
    for path, why in excluded_report:
        print(f"  · виключено: {path} — {why}")
    for f, why in exempted:
        print(f"  · рядковий виняток: {f['file']}:{f['line']} [{f['category']}] — {why}")
    for s in skipped:
        print(f"⚠️  ПРОПУЩЕНО: {s}")
    print()

    if not findings:
        print("✓ чисто — жодних даних поїздки в репозиторії не знайдено")
        return

    by_file = {}
    for f in findings:
        by_file.setdefault(f["file"], []).append(f)

    for path in sorted(by_file):
        print(f"✗ {path}")
        for f in sorted(by_file[path], key=lambda x: x["line"]):
            loc = f":{f['line']}" if f["line"] else ""
            print(f"    {path}{loc}  [{f['category']}]  {f['match']!r}")
            print(f"      → {f['why']}")
    print(f"\n❗ знахідок: {len(findings)} у {len(by_file)} файл(ах) — "
          "гейт публічності НЕ пройдено")


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="тільки JSON-звіт у stdout")
    args = ap.parse_args(argv[1:])

    findings, skipped, excluded_report, n_files, exempted = run()

    if args.json:
        print(json.dumps({
            "scanned_files": n_files,
            "excluded": [{"file": p, "why": w} for p, w in excluded_report],
            "line_exemptions": [{"file": f["file"], "line": f["line"],
                                  "category": f["category"], "why": w}
                                 for f, w in exempted],
            "skipped_checks": skipped,
            "findings": findings,
            "ok": not findings,
        }, ensure_ascii=False, indent=1))
    else:
        print_report(findings, skipped, excluded_report, n_files, exempted)

    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
