"""站点记忆：同站点第二次跑，直接给上次成功的动作序列。

存 JSONL，一条 = 一次成功任务的 {host, path, steps}。ref 是一次性的，所以存
role+name 而不是 ref，回放时按名字重新定位。
"""
import json
import os
import re
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.environ.get("MOONLIGHTING_MEM",
                       os.path.join(os.path.dirname(HERE), "mem", "sites.jsonl"))


def key(url):
    m = re.match(r"https?://([^/]+)(/[^?#]*)", url or "")
    return (m.group(1), m.group(2)) if m else ("", "")


def _rows():
    try:
        with open(STORE, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]
    except OSError:
        return []


def hint(url):
    """找同 host + 同路径前缀的上一次成功序列（最长匹配优先）。"""
    host, path = key(url)
    best = None
    for r in _rows():
        if r.get("host") == host and path.startswith(r.get("path", "")):
            # 路径一样长时取最新的那条（>=），否则旧记忆永远压住新记忆
            if best is None or len(r["path"]) >= len(best["path"]):
                best = r
    return best


def record(url, steps, task_id=""):
    host, path = key(url)
    if not host or not steps:
        return None
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    row = {"host": host, "path": path, "task": task_id, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
           "steps": [{"action": s.get("action"), "name": s.get("name", ""),
                      "role": s.get("role", "")} for s in steps]}
    with open(STORE, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row
