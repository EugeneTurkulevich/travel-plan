"""
scripts/merge_cloud_photos.py

Зустрічне злиття правок POI з хмарної карти в локальні MD-файли.

НАВІЩО. Карта спільна: з нею паралельно працюють інші агенти й люди
(методичка, розділ «Ревізії і спільне редагування»). Наш pipeline —
MD → route_state.json → set_route — перезаписує ВЕСЬ route-шар, тому будь-яка
чужа правка на карті буде мовчки затерта, якщо перед пушем не забрати її в MD.

ЩО РОБИТЬ. Для кожного nested_point звіряє `photo_url` і координати між хмарою
і локальним MD:
  • хмара має інше фото і воно не в чорному списку → **беремо хмарне**
    (це чужа робота, вона свіжіша за нашу);
  • фото (локальне чи хмарне) у чорному списку → **знімаємо** (ставимо `?`) —
    порожня клітинка завжди краща за впевнено чуже фото;
  • координати відрізняються → лише ПОКАЗУЄМО. Автоматично брати їх не можна:
    розбіжність однаково виглядає і коли чужий агент уточнив координату, і коли
    карта просто не знає про нашу ще не запушену правку. `--take-coords` —
    свідомо взяти хмарні.

ЧОРНИЙ СПИСОК. `find_poi_photos.py` шукає по назві через opensearch/Commons і
на коротких або транслітерованих назвах видає омоніми: «Bâlea Lac» → місячний
кратер Lacus Timoris, «Prionia» → молекула пріона, «Anogi» → анатомічна
ілюстрація, «Astakos paralia» → жук Trogoderma paralia, «Golden Gate» → міст у
Сан-Франциско. Список нижче — результат ручного аудиту 110 фото (03.08.2026).

Run (потрібен токен у .env):
    PYTHONPATH=scripts python3 scripts/merge_cloud_photos.py --dry-run
    PYTHONPATH=scripts python3 scripts/merge_cloud_photos.py
    PYTHONPATH=scripts python3 scripts/merge_cloud_photos.py --take-coords
Далі обовʼязково: python3 scripts/generate_state_from_md.py
"""
import json
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

from cloud_push import get_access_token, get_worker, load_env_file

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trip_ctx import paths
CTX = paths()

PLACES = CTX.places_dir
MAP_ID = CTX.map_id      # з профілю: літерал у публічному репо неприпустимий

# Фрагменти імені файла Wikimedia, які означають «це не те місце».
BAD_SLUGS = [
    "Lacus_Timoris", "Prion_subdomain", "Ateneumin_taidemuseo", "Anogenital",
    "Conic_sections", "Fra_Angelico", "Samos_049", "Kedi-film-poster",
    "Trogoderma", "Thousand_Islands_Bridge", "Wartburg", "Clock_of_Erbil",
    "Ritchie_Valens", "Kunstkammer", "Bubene", "Prometheus_and_Atlas",
    "Targoviste", "Fortress_Gurzuf", "Хорупань", "Kritou_Terra",
    "Chalon-sur-Sa", "Mithras", "Longhi_1684_Preveza", "Wpdms_usgs",
    "Hrm4", "Richtone", "peasant_museum", "Biserica_Sf._Nicolae_din_Poieni",
    "School_of_Homer,_Scio", "School_of_Homer%2C_Scio",
]


def _flat(s: str) -> str:
    """Нижній регістр без діакритики: Târgoviște → targoviste."""
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn").lower()


def is_bad(url: str) -> bool:
    if not url or not url.startswith("http"):
        return False
    dec = _flat(urllib.parse.unquote(url))
    return any(_flat(s) in dec for s in BAD_SLUGS)


def call(worker, token, name, args):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": args}}).encode()
    req = urllib.request.Request(worker, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "trip-map-toolkit/1.0")
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.load(r)
    if "error" in resp:
        sys.exit(f"{name}: {resp['error']}")
    content = (resp.get("result") or {}).get("content") or []
    return json.loads(content[0]["text"]) if content else {}


def h1_label(text: str) -> str:
    m = re.match(r"^# (.+)$", text, re.MULTILINE)
    lbl = m.group(1).strip() if m else ""
    return lbl.split(" / ")[0].strip()


def main():
    dry = "--dry-run" in sys.argv
    take_coords = "--take-coords" in sys.argv
    env = load_env_file()
    worker = get_worker(env)
    route = call(worker, get_access_token(env, worker), "get_route", {"map_id": MAP_ID})
    print(f"хмара: rev={route.get('rev')}, точок {len(route.get('points') or [])}\n")

    # (label, poi_name) -> nested_point
    cloud = {}
    for p in route.get("points") or []:
        for n in p.get("nested_points") or []:
            cloud.setdefault((p.get("label", ""), n.get("name", "")), n)

    adopted = cleared = coords = 0
    for path in sorted(PLACES.glob("[0-9][0-9][0-9][0-9].md")):
        text = path.read_text(encoding="utf-8")
        label = h1_label(text)
        lines = text.splitlines(keepends=True)
        touched = False

        for i, line in enumerate(lines):
            if not line.startswith("|"):
                continue
            cells = line.split("|")
            if len(cells) < 5:
                continue
            name = re.sub(r"\*\*", "", cells[1]).strip()
            if not name or name in ("Назва", "Заклад") or set(name) <= set(" :-"):
                continue
            local_photo = cells[4].strip().strip("`").strip() if len(cells) > 5 else ""

            # Чистимо ХИБНЕ локальне фото незалежно від того, чи знайшовся POI
            # у хмарі: після перейменування (напр. «Музей АСТРА» → «Парк
            # Дубрава») збіг за назвою зникає, і сміття лишалось непоміченим.
            c = cloud.get((label, name))
            if c is None:
                if len(cells) > 5 and is_bad(local_photo):
                    cells[4] = " ? "
                    lines[i] = "|".join(cells)
                    touched = True
                    cleared += 1
                    print(f"  [clr ] {path.name} · {name[:34]} (немає в хмарі — перейменовано)")
                continue

            cloud_photo = (c.get("photo_url") or "").strip()

            # 1) фото
            # АСИМЕТРИЧНЕ правило: хмара ЗАПОВНЮЄ порожнечі, але НЕ перетирає
            # справний локальний контент. Інакше застарілий стан карти (вона
            # ж не знає про наші ще не запушені правки) відкотив би свідомі
            # локальні зміни — саме так ледь не повернулись координати
            # виноградників Науси замість крамниці в Едессі.
            if len(cells) > 5:
                new = None
                cloud_ok = bool(cloud_photo) and not is_bad(cloud_photo)
                local_ok = bool(local_photo) and local_photo != "?" and not is_bad(local_photo)
                if cloud_ok and not local_ok:
                    new, act = f" `{cloud_photo}` ", "take"
                elif not cloud_ok and is_bad(local_photo):
                    new, act = " ? ", "clr"
                if new is not None:
                    cells[4] = new
                    lines[i] = "|".join(cells)
                    touched = True
                    if act == "clr":
                        cleared += 1
                        print(f"  [clr ] {path.name} · {name[:34]}")
                    else:
                        adopted += 1
                        slug = urllib.parse.unquote(cloud_photo).rsplit("/", 1)[-1][:44]
                        print(f"  [take] {path.name} · {name[:30]:<30} ← {slug}")

            # 2) координати — за замовчуванням ТІЛЬКИ показуємо.
            # Автоматично брати їх не можна: розбіжність однаково виглядає і
            # коли чужий агент уточнив координату, і коли карта просто не знає
            # про нашу ще не запушену правку. Рішення — за людиною.
            m = re.match(r"\s*`?\s*([-\d.]+)\s*,\s*([-\d.]+)", cells[3])
            if m and c.get("lat") is not None:
                llat, llon = float(m.group(1)), float(m.group(2))
                clat, clon = float(c["lat"]), float(c["lon"])
                if abs(llat - clat) > 1e-4 or abs(llon - clon) > 1e-4:
                    coords += 1
                    mark = "crd!" if take_coords else "crd?"
                    print(f"  [{mark}] {path.name} · {name[:30]:<30} "
                          f"локально {llat:.4f},{llon:.4f} · хмара {clat:.4f},{clon:.4f}")
                    if take_coords:
                        cells[3] = f" `{clat:.4f}, {clon:.4f}` "
                        lines[i] = "|".join(cells)
                        touched = True

        if touched and not dry:
            path.write_text("".join(lines), encoding="utf-8")

    print(f"\n{'[DRY RUN] ' if dry else ''}узято з хмари: {adopted} фото · "
          f"розбіжностей координат: {coords}"
          f"{' (узято)' if take_coords else ' (лише показано)'} · знято хибних: {cleared}")
    if not dry:
        print("Далі: python3 scripts/generate_state_from_md.py")


if __name__ == "__main__":
    main()
