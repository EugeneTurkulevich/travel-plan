"""
scripts/mcp_probe.py

Показує, що вміє віддалений MCP-воркер trip-map: список тулів, їх описи і
схеми аргументів. Потрібно, коли тули ще не підключені в сесії Claude Code
(HTTP MCP підхоплюється лише на старті), а знати контракт треба вже зараз.

Автентифікація — та сама, що в cloud_push.py (.env у корені репо).
Токен НІКОЛИ не друкується.

Usage:
    PYTHONPATH=scripts python3 scripts/mcp_probe.py           # список тулів
    PYTHONPATH=scripts python3 scripts/mcp_probe.py --full    # + повні JSON-схеми
"""
import json
import sys
import urllib.error
import urllib.request

from cloud_push import get_access_token, get_worker, load_env_file


def rpc(worker: str, token: str, method: str, params=None, rid: int = 1):
    body = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        body["params"] = params
    # Заголовки — рівно ті, що шле cloud_push.call_tool: воркер вибагливий,
    # зайвий Accept: text/event-stream повертає 403.
    req = urllib.request.Request(worker, data=json.dumps(body).encode(), method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "trip-map-toolkit/1.0")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode()
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code} на {method}: {e.read().decode(errors='replace')[:400]}")
    # воркер може відповідати SSE-обгорткою
    if raw.lstrip().startswith("event:") or raw.lstrip().startswith("data:"):
        for line in raw.splitlines():
            if line.startswith("data:"):
                raw = line[5:].strip()
                break
    return json.loads(raw)


def main():
    env = load_env_file()
    worker = get_worker(env)
    token = get_access_token(env, worker)          # токен не друкуємо

    # Методичка сервера + список ресурсів:
    #   PYTHONPATH=scripts python3 scripts/mcp_probe.py docs
    #   PYTHONPATH=scripts python3 scripts/mcp_probe.py read <uri>
    if len(sys.argv) > 1 and sys.argv[1] == "docs":
        init = rpc(worker, token, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "mcp_probe", "version": "1.0"},
        })
        instr = (init.get("result") or {}).get("instructions")
        if instr:
            print("=" * 78 + "\n INSTRUCTIONS (методичка сервера)\n" + "=" * 78)
            print(instr)
        res = rpc(worker, token, "resources/list", {}, rid=2)
        items = (res.get("result") or {}).get("resources") or []
        print("\n" + "=" * 78 + f"\n RESOURCES: {len(items)}\n" + "=" * 78)
        for r in items:
            print(f"  {r.get('uri')}  —  {r.get('name')}: {(r.get('description') or '')[:90]}")
        return

    if len(sys.argv) > 2 and sys.argv[1] == "read":
        res = rpc(worker, token, "resources/read", {"uri": sys.argv[2]}, rid=3)
        if "error" in res:
            sys.exit(f"resources/read: {res['error']}")
        for c in (res.get("result") or {}).get("contents") or []:
            print(c.get("text") or c)
        return

    # Читальний виклик будь-якого тула:
    #   PYTHONPATH=scripts python3 scripts/mcp_probe.py call get_map '{"map_id":"..."}'
    if len(sys.argv) > 2 and sys.argv[1] == "call":
        name = sys.argv[2]
        args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
        res = rpc(worker, token, "tools/call",
                  {"name": name, "arguments": args}, rid=3)
        if "error" in res:
            sys.exit(f"{name}: {res['error']}")
        result = res.get("result") or {}
        content = result.get("content") or []
        text = content[0].get("text", "") if content else ""
        if result.get("isError"):
            sys.exit(f"{name} повернув помилку: {text}")
        try:
            print(json.dumps(json.loads(text), ensure_ascii=False, indent=2))
        except json.JSONDecodeError:
            print(text)
        return

    print(f"worker: {worker}")
    try:
        init = rpc(worker, token, "initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "mcp_probe", "version": "1.0"},
        })
        info = (init.get("result") or {}).get("serverInfo") or {}
        caps = (init.get("result") or {}).get("capabilities") or {}
        print(f"server: {info.get('name')} {info.get('version')}")
        print(f"capabilities: {', '.join(sorted(caps)) or '—'}")
    except SystemExit as e:
        print(f"initialize недоступний ({e}) — читаємо лише tools/list")
    print()

    res = rpc(worker, token, "tools/list", {}, rid=2)
    if "error" in res:
        sys.exit(f"tools/list помилка: {res['error']}")
    tools = (res.get("result") or {}).get("tools") or []
    print(f"тулів: {len(tools)}\n" + "=" * 78)
    for t in tools:
        schema = t.get("inputSchema") or {}
        props = schema.get("properties") or {}
        req = set(schema.get("required") or [])
        print(f"\n▸ {t['name']}")
        desc = (t.get("description") or "").strip().splitlines()
        for line in desc[:6]:
            print(f"    {line}")
        if props:
            print("    args:")
            for k, v in props.items():
                mark = "*" if k in req else " "
                typ = v.get("type") or "/".join(
                    x.get("type", "?") for x in (v.get("anyOf") or v.get("oneOf") or []))
                d = (v.get("description") or "").split("\n")[0][:70]
                print(f"      {mark} {k:<22} {typ:<20} {d}")
        if "--full" in sys.argv:
            print("    schema:", json.dumps(schema, ensure_ascii=False)[:1500])
    print("\n" + "=" * 78)
    print("* — обовʼязковий аргумент")


if __name__ == "__main__":
    main()
