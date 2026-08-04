"""
scripts/check_marks.py

Перед кожним пушем: що з мітками учасників зараз.

НАВІЩО. 03.08.2026 я закрив мітку «хоч на якусь гірочку заліземо» як `done`,
а вже після цього в треді зʼявилось «невірно враховано» з уточненням. Мітка
лишалась зеленою, побажання — невиконаним, і помітив це користувач, а не я.

Методичка вимагає перечитувати треди перед плануванням. Цього мало: обговорення
триває й після нього. Тому перевірка робиться **перед пушем**, коли ще можна
або врахувати уточнення, або чесно не закривати мітку.

Ловить два стани:
  ⏳ status="open"                        — побажання ще в роботі;
  ⚠️ закрито, але після `resolved_ts`     — статус застарів: людина відповіла
     зʼявились нові коментарі               вже після того, як ми відзвітували.

Run (потрібен токен у .env):
    PYTHONPATH=scripts python3 scripts/check_marks.py
    PYTHONPATH=scripts python3 scripts/check_marks.py --map <id>

Код виходу 1 — є що подивитись перед пушем.
"""
import datetime
import json
import sys
import urllib.request

from cloud_push import get_access_token, get_worker, load_env_file

MAP_ID = "zb87h25mvtdd8959f2zc"


def call(worker, token, name, args):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": args}}).encode()
    req = urllib.request.Request(worker, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "travel202609-cloud-push/1.0")
    with urllib.request.urlopen(req, timeout=90) as r:
        resp = json.load(r)
    if "error" in resp:
        sys.exit(f"{name}: {resp['error']}")
    content = (resp.get("result") or {}).get("content") or []
    return json.loads(content[0]["text"]) if content else {}


def ts(v) -> str:
    try:
        return datetime.datetime.fromtimestamp(int(v) / 1000).strftime("%d.%m %H:%M")
    except (TypeError, ValueError):
        return "?"


def comments(mark) -> list:
    c = mark.get("comments") or []
    if isinstance(c, dict):
        c = list(c.values())
    return sorted(c, key=lambda x: x.get("ts", 0))


def main() -> int:
    argv = sys.argv
    map_id = argv[argv.index("--map") + 1] if "--map" in argv else MAP_ID

    env = load_env_file()
    worker = get_worker(env)
    token = get_access_token(env, worker)
    data = call(worker, token, "list_marks",
                {"map_id": map_id, "include_resolved": True})

    marks = data.get("marks") or []
    todo = []
    print(f"міток на карті: {len(marks)}\n")

    for m in marks:
        cs = comments(m)
        first = cs[0].get("text", "") if cs else ""
        status = m.get("status", "open")
        head = f"[{status}] {m.get('title') or first[:44] or '(без назви)'}"

        if status == "open":
            todo.append((m, "⏳ ще в роботі — врахувати або пояснити, чому ні"))
            print(f"⏳ {head}")
        else:
            rts = m.get("resolved_ts") or 0
            later = [c for c in cs if c.get("ts", 0) > rts]
            if later:
                todo.append((m, "⚠️ закрито, але потім обговорювали"))
                print(f"⚠️  {head}   (закрито {ts(rts)}, rev {m.get('resolved_rev')})")
                for c in later:
                    print(f"      ↳ {ts(c.get('ts'))}  {str(c.get('author'))[:26]:<26} "
                          f"{c.get('text', '')[:80]}")
            else:
                print(f"✓  {head}   (rev {m.get('resolved_rev')})")

    print()
    if not todo:
        print("✓ усе враховано і закрито — можна пушити")
        return 0
    print(f"❗ потребує уваги ПЕРЕД пушем: {len(todo)}")
    for m, why in todo:
        print(f"   {m['id']}  {why}")
    print("\nПісля пушу — resolve_mark(status=done|skipped, comment=…) "
          "на кожну, якої зміна стосується.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
