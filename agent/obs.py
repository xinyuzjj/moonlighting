"""观测层：从 browser_daemon 取无障碍树，压到 ≤6KB 再喂模型。

原始快照 44KB ≈ 1.1 万 token/步；压缩只留 role + name + ref + 元素自带文本。
丢弃：generic 空容器、/url: 行、无 alt 的 img、纯装饰节点。
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request


def _from_scripts(name):
    """复用 repo 里 scripts/ 的端口与 token 解析，别在这里再写一份默认值。"""
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
    if p not in sys.path:
        sys.path.append(p)
    try:
        return __import__(name)
    except Exception:  # noqa: BLE001
        return None


_ports = _from_scripts("ports")
DRIVER = os.environ.get("HERMES_DRIVER") or (
    f"http://127.0.0.1:{_ports.port_of('driver') if _ports else 8897}")
_TOKEN = os.environ.get("HERMES_TOKEN") or ""


def _token():
    global _TOKEN
    if _TOKEN:
        return _TOKEN
    auth = _from_scripts("auth")
    if auth:
        try:
            _TOKEN = auth.token()
            return _TOKEN
        except Exception:  # noqa: BLE001
            pass
    home = os.environ.get("HERMES_BROWSER_HOME",
                          os.path.expandvars(r"%LOCALAPPDATA%\hermes-browser"))
    try:
        with open(os.path.join(home, "token.txt"), encoding="utf-8") as f:
            _TOKEN = f.read().strip()
    except OSError:
        pass
    return _TOKEN


def call(path, **params):
    url = DRIVER + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"X-Hermes-Token": _token()})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read().decode("utf-8", "replace")


def page():
    """当前 URL 和标题，来自快照头部。"""
    t = call("/snap")
    url = re.search(r"Page URL: (\S+)", t)
    title = re.search(r"Page Title: (.*)", t)
    return {"url": url.group(1) if url else "", "title": title.group(1) if title else "", "raw": t}


_KEEP = {"link", "button", "textbox", "checkbox", "radio", "combobox", "tab",
         "menuitem", "heading", "img", "switch", "option", "searchbox", "dialog"}
_NOISE = re.compile(r"\[(cursor=pointer|level=\d+|active|expanded|disabled|checked|"
                    r"selected|pressed|readonly|required)\]")


def compress(raw, limit=6000):
    """只留可操作节点（role 白名单），并去掉不影响决策的属性标记。

    正文文本不进上下文：需要内容时用 read/eval 按需取。
    实测 42KB 的 MDN 页面 → 6.8KB，交互元素全保留。
    """
    kept, dropped = [], 0
    for line in raw.splitlines():
        s = line.strip()
        if "[ref=" not in s:
            continue                      # 纯文本节点一律不进（用 read 按需取）
        parts = s.lstrip("- ").split(None, 1)
        role = parts[0] if parts else ""
        if role not in _KEEP:
            dropped += 1
            continue
        kept.append(_NOISE.sub("", line.rstrip()))
    text = "\n".join(kept)
    if len(text) > limit:
        head, size = [], 0
        for line in kept:
            if size + len(line) + 1 > limit:
                break
            head.append(line)
            size += len(line) + 1
        dropped += len(kept) - len(head)
        text = "\n".join(head)
    return text + (f"\n# 省略 {dropped} 行（非交互节点/超出预算）" if dropped else "")


def snapshot(limit=6000):
    p = page()
    tree = compress(p["raw"], limit)
    return {"url": p["url"], "title": p["title"], "tree": tree,
            "raw_size": len(p["raw"]), "needs_vision": len(tree) < 500}


def shot(path):
    """视觉兜底：从推流服务抓一张当前画面存成 jpg。

    无障碍树解决不了的时候（纯 canvas、树被清空、点不中）才用，别当默认路径。
    """
    port = os.environ.get("HERMES_STREAM_PORT", "8765")
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/snapshot", timeout=20) as r:
        data = r.read()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return {"path": path, "bytes": len(data)}


if __name__ == "__main__":
    s = snapshot()
    print(f"URL   {s['url']}\nTITLE {s['title']}")
    print(f"原始 {s['raw_size']}B → 压缩 {len(s['tree'])}B")
    print(s["tree"][:800])
