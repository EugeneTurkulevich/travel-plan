"""
scripts/trip_ctx.py

Єдина точка, яка знає, ДЕ лежать дані. Жоден інший скрипт не має права на
Path("places/…") чи будь-який інший здогад про розташування — тільки через
цей модуль.

Розділення рівнів (STRUCTURE_PROPOSAL.md §1):
  рівень 2  цей репозиторій — правила, скрипти, схеми. Публічний.
  рівень 3  ../travel-data   — зібрані дані, привʼязки, кеші, токени. Приватний.

Резолв кореня даних, у порядку пріоритету:
  1. --data <шлях>              явний аргумент
  2. $TRAVEL_DATA               змінна оточення
  3. .travel-local.json         {"data_root": "..."} у корені репо (не в git)
  4. ../travel-data             дефолт-сусід

Резолв активного профілю:
  1. --trip <id>                явний аргумент
  2. $TRIP                      змінна оточення
  3. .active-trip               один рядок у корені даних

Використання:

    from trip_ctx import context
    ctx = context()                 # розбирає sys.argv сам
    ctx.banner()                    # ▶ 2026-09-balkans (map zb87h…)
    cards = ctx.places_dir
    route = ctx.read_json(ctx.route_json)
"""
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCAL_CONF = REPO_ROOT / ".travel-local.json"
DEFAULT_DATA = REPO_ROOT.parent / "travel-data"


class TripContextError(RuntimeError):
    """Дані або профіль не знайдено — повідомлення вже придатне для користувача."""


# ── дрібні помічники ─────────────────────────────────────────────────────────

def _argv_value(argv, flag):
    """--flag value → value; --flag=value теж. Не мутує argv."""
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def parse_env_file(path):
    """KEY=VALUE без бібліотек. Порожній dict, якщо файлу немає."""
    values = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        values[k.strip()] = v.strip().strip('"').strip("'")
    return values


# ── контекст ─────────────────────────────────────────────────────────────────

class TripContext:
    def __init__(self, data_root, trip_id, trip):
        self.data_root = data_root
        self.trip_id = trip_id
        self.trip = trip

    # -- шляхи рівня даних ---------------------------------------------------
    @property
    def places_dir(self):
        return self.data_root / "places"

    @property
    def cache_dir(self):
        return self.data_root / "cache"

    @property
    def env_file(self):
        return self.data_root / ".env"

    @property
    def trip_dir(self):
        return self.data_root / "trips" / self.trip_id

    @property
    def trip_json(self):
        return self.trip_dir / "trip.json"

    @property
    def route_json(self):
        return self.trip_dir / "route.json"

    @property
    def live_mirror(self):
        return self.trip_dir / "live_mirror.json"

    @property
    def overlay_dir(self):
        return self.trip_dir / "overlay"

    @property
    def exports_dir(self):
        return self.trip_dir / "exports"

    @property
    def route_state(self):
        return self.exports_dir / "route_state.json"

    @property
    def draft_id_file(self):
        return self.trip_dir / ".cloud-draft-id"

    # -- зручності -----------------------------------------------------------
    @property
    def map_id(self):
        return self.trip.get("map_id")

    @property
    def is_archived(self):
        return self.trip.get("status") == "archived"

    def env(self):
        return parse_env_file(self.env_file)

    @staticmethod
    def read_json(path, default=None):
        if not path.exists():
            if default is not None:
                return default
            raise TripContextError(f"Немає файлу: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def write_json(path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8",
        )

    def banner(self, stream=None):
        """Перший рядок виводу будь-якого скрипта — щоб не працювати не в тій поїздці."""
        out = stream or sys.stdout
        mid = self.map_id or "—"
        short = mid if len(mid) <= 8 else mid[:8] + "…"
        flag = " [АРХІВ]" if self.is_archived else ""
        print(f"▶ {self.trip_id} (map {short}) · дані: {self.data_root}{flag}",
              file=out)


# ── резолв ───────────────────────────────────────────────────────────────────

def resolve_data_root(argv=None, explicit=None):
    argv = sys.argv if argv is None else argv
    candidate = explicit or _argv_value(argv, "--data") or os.environ.get("TRAVEL_DATA")
    if not candidate and LOCAL_CONF.exists():
        conf = json.loads(LOCAL_CONF.read_text(encoding="utf-8"))
        candidate = conf.get("data_root")
    root = Path(candidate).expanduser() if candidate else DEFAULT_DATA
    root = root.resolve() if root.exists() else root
    if not root.exists():
        raise TripContextError(
            f"Кореня даних немає: {root}\n"
            f"Створи його або вкажи шлях у {LOCAL_CONF.name} / $TRAVEL_DATA / --data"
        )
    return root


def resolve_trip_id(data_root, argv=None, explicit=None):
    argv = sys.argv if argv is None else argv
    trip_id = explicit or _argv_value(argv, "--trip") or os.environ.get("TRIP")
    if trip_id:
        return trip_id
    active = data_root / ".active-trip"
    if active.exists():
        value = active.read_text(encoding="utf-8").strip()
        if value:
            return value
    raise TripContextError(
        f"Активний профіль не заданий: немає {active}\n"
        f"Постав його: python3 scripts/trip.py use <id>"
    )


def context(argv=None, data_root=None, trip_id=None):
    root = resolve_data_root(argv, data_root)
    tid = resolve_trip_id(root, argv, trip_id)
    trip_file = root / "trips" / tid / "trip.json"
    if not trip_file.exists():
        available = sorted(p.name for p in (root / "trips").glob("*") if p.is_dir())
        raise TripContextError(
            f"Профілю «{tid}» немає ({trip_file}).\n"
            f"Наявні: {', '.join(available) or '— жодного'}"
        )
    return TripContext(root, tid, json.loads(trip_file.read_text(encoding="utf-8")))


def list_trips(data_root):
    """[(id, trip_dict)] — усі профілі в корені даних."""
    trips_dir = data_root / "trips"
    if not trips_dir.exists():
        return []
    out = []
    for d in sorted(trips_dir.iterdir()):
        f = d / "trip.json"
        if f.is_dir() or not f.exists():
            continue
        out.append((d.name, json.loads(f.read_text(encoding="utf-8"))))
    return out
