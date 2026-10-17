"""恢复所有私有桌面：枚举 D:\Hermes浏览器 下的 profile，各起一个桌面 Chrome。

用法:
  python desk_restore.py            # 全部恢复（恢复上次会话）
  python desk_restore.py --list     # 只看有哪些桌面

端口分配写在 D:\Hermes浏览器\desktops.json；没有就自动从 18763 顺排。
"""
import argparse
import json
import os
import subprocess
import sys

try:
    import ports as _ports
    BASE = _ports.profiles()
except Exception:  # noqa: BLE001
    BASE = os.environ.get("HERMES_PROFILES") or ('D:\\Hermes浏览器' if os.path.isdir('D:\\Hermes浏览器') else os.path.join(os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "hermes-browser"), "profiles"))
CFG = os.path.join(BASE, "desktops.json")
FIRST_PORT = int(os.environ.get("HERMES_CDP_PORT", "18763"))
FIRST_STREAM = int(os.environ.get("HERMES_STREAM_PORT", "8765"))


def profiles():
    if not os.path.isdir(BASE):
        return []
    return sorted(d for d in os.listdir(BASE)
                  if os.path.isdir(os.path.join(BASE, d))
                  and os.path.isfile(os.path.join(BASE, d, "Local State")))


def load_cfg(names):
    cfg = {}
    if os.path.isfile(CFG):
        try:
            with open(CFG, encoding="utf-8") as fh:
                cfg = json.load(fh)
        except (OSError, ValueError):
            cfg = {}
    changed = False
    for i, n in enumerate(names):
        if n not in cfg:
            cfg[n] = {"cdp": FIRST_PORT + i, "stream": FIRST_STREAM + i}
            changed = True
    if changed:
        with open(CFG, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
    return cfg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--streams", action="store_true", help="同时把推流也拉起来")
    args = ap.parse_args()

    names = profiles()
    if not names:
        print("D:\\Hermes浏览器 下没有 profile")
        return
    cfg = load_cfg(names)

    if args.list:
        for n in names:
            print(f"{n:16s} cdp={cfg[n]['cdp']} stream={cfg[n]['stream']}")
        return

    here = os.path.dirname(os.path.abspath(__file__))
    for n in names:
        port = cfg[n]["cdp"]
        print(f"=== 恢复 {n} (cdp {port})")
        r = subprocess.run([sys.executable, os.path.join(here, "desk_chrome.py"),
                            "--desktop", n, "--port", str(port), "--restore"],
                           capture_output=True, text=True, timeout=240)
        print(r.stdout.strip() or r.stderr.strip()[-400:])
        if args.streams:
            subprocess.Popen([sys.executable, os.path.join(here, "desk_stream.py"),
                              "--port", str(cfg[n]["stream"]), "--desktop", n],
                             creationflags=0x08000000)  # CREATE_NO_WINDOW


if __name__ == "__main__":
    main()
