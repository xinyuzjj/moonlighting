"""多 agent 隔离：一个 session = 一套独立的桌面 + Chrome + profile + 端口。

为什么要这个：两个 agent 共用一个浏览器会互相抢标签、快照 ref 互相失效、登录态串号。
一个 session 里所有东西都是自己的：

    session "a1"  →  桌面 HermesDesk-a1
                     profile <home>/profiles/a1      （独立 cookie / 扩展 / 登录态）
                     CDP 18763+  / 推流 8765+  / 驱动 8897+   （各自端口，互不干扰）

用法:
    python session.py new --name a1 [--url https://example.com]
    python session.py list
    python session.py info --name a1
    python session.py stop --name a1
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ports as _ports          # noqa: E402
import svc as _svc              # noqa: E402  （复用它的隐藏启动）

SESS_DIR = os.path.join(_ports.home(), "sessions")
PY = sys.executable


def _free(start, used):
    p = start
    while p in used or _ports.occupied(p):
        p += 1
    return p


def _load(name=None):
    os.makedirs(SESS_DIR, exist_ok=True)
    if name:
        path = os.path.join(SESS_DIR, f"{name}.json")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    out = {}
    for fn in os.listdir(SESS_DIR):
        if fn.endswith(".json"):
            with open(os.path.join(SESS_DIR, fn), encoding="utf-8") as f:
                d = json.load(f)
                out[d["name"]] = d
    return out


def _save(s):
    os.makedirs(SESS_DIR, exist_ok=True)
    with open(os.path.join(SESS_DIR, f"{s['name']}.json"), "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2, ensure_ascii=False)


def _wait(port, timeout=25):
    end = time.time() + timeout
    while time.time() < end:
        if _ports.occupied(port):
            return True
        time.sleep(0.5)
    return False


def cmd_new(a):
    if _load(a.name):
        print(f"session {a.name} 已存在；先 stop 或换个名字")
        return 2
    used = set()
    for s in _load().values():
        used.update([s["cdp"], s["stream"], s["driver"]])

    cdp = _free(int(os.environ.get("HERMES_CDP_PORT", "18763")), used)
    stream = _free(int(os.environ.get("HERMES_STREAM_PORT", "8765")), used)
    driver = _free(int(os.environ.get("HERMES_DRIVER_PORT", "8897")), used)
    desktop = f"HermesDesk-{a.name}"
    profile = os.path.join(_ports.profiles(), a.name)

    print(f"[session {a.name}] 桌面={desktop} profile={profile}")
    print(f"                cdp={cdp} stream={stream} driver={driver}")

    r = subprocess.run([PY, os.path.join(HERE, "desk_chrome.py"), "--desktop", desktop,
                        "--port", str(cdp), "--url", a.url],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if not _wait(cdp, 20):
        print("Chrome 没起来：", (r.stdout or r.stderr)[-400:])
        return 1

    _svc.spawn_detached("desk_stream.py", ["--port", str(stream), "--desktop", desktop,
                                           "--cdp", str(cdp)])
    _svc.spawn_detached("browser_daemon.py", ["--cdp", str(cdp), "--port", str(driver)])
    ok_stream = _wait(stream, 25)
    ok_driver = _wait(driver, 40)

    s = {"name": a.name, "desktop": desktop, "profile": profile,
         "cdp": cdp, "stream": stream, "driver": driver,
         "preview": f"http://127.0.0.1:{stream}/ctl",
         "created": time.strftime("%Y-%m-%d %H:%M:%S")}
    _save(s)
    print(f"                stream={'UP' if ok_stream else 'DOWN'} driver={'UP' if ok_driver else 'DOWN'}")
    print(f"                预览: {s['preview']}")
    return 0 if (ok_stream and ok_driver) else 1


def cmd_list(_a):
    ss = _load()
    if not ss:
        print("没有 session")
        return 0
    for n, s in sorted(ss.items()):
        print(f"  {n:<10} cdp={s['cdp']:<6} stream={s['stream']:<6} driver={s['driver']:<6} "
              f"{'UP' if _ports.occupied(s['cdp']) else 'DOWN'}  {s['preview']}")
    return 0


def cmd_info(a):
    s = _load(a.name)
    if not s:
        print("没有这个 session")
        return 1
    s["cdp_alive"] = _ports.occupied(s["cdp"])
    print(json.dumps(s, ensure_ascii=False, indent=2))
    return 0


def cmd_stop(a):
    s = _load(a.name)
    if not s:
        print("没有这个 session")
        return 1
    for port in (s["stream"], s["driver"]):
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace").stdout
        for line in out.splitlines():
            if f":{port} " in line and "LISTENING" in line:
                pid = line.split()[-1]
                subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True)
    # 关掉该桌面上的 Chrome（只关这个桌面的，不碰别人的）
    subprocess.run([PY, os.path.join(HERE, "desk_chrome.py"), "--desktop", s["desktop"],
                    "--port", str(s["cdp"]), "--no-kill"], capture_output=True)
    os.remove(os.path.join(SESS_DIR, f"{a.name}.json"))
    print(f"session {a.name} 已停止")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new")
    n.add_argument("--name", required=True)
    n.add_argument("--url", default="about:blank")
    n.set_defaults(fn=cmd_new)
    for name, fn in (("list", cmd_list), ("info", cmd_info), ("stop", cmd_stop)):
        p = sub.add_parser(name)
        if name != "list":
            p.add_argument("--name", required=True)
        p.set_defaults(fn=fn)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())


def cli():
    """pip 安装后的命令行入口。"""
    raise SystemExit(main())
