"""常驻浏览器驱动：一个 MCP 会话 + 本地 HTTP 接口，按 ref 操作，不用坐标和选择器。

为什么这么做：
- Playwright MCP 的 browser_snapshot 返回完整无障碍树，每个可交互元素带 [ref=e42]，
  一次调用就能看清整页，不用逐个 querySelector 试。
- ref 在「同一个 MCP 会话」里有效，所以必须常驻一个进程，不能每次动作都新起一个。

启动:
  python browser_daemon.py --cdp 18763 --port 8897

接口:
  GET /snap                     完整快照（含标签页列表、ref）
  GET /click?target=e42&element=按钮描述
  GET /type?target=e12&text=hello&element=输入框
  GET /goto?url=https://example.com
  GET /eval?js=location.href
  GET /find?text=登录
  GET /status
"""
import argparse
import json
import re
import urllib.parse
import urllib.request
import subprocess
import threading
import time
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

try:
    import auth as _auth
except ImportError:                      # 单独拷走也能跑，只是没鉴权
    class _auth:                          # noqa: N801
        @staticmethod
        def check(_h, _q):
            return True

        @staticmethod
        def headers_for_client():
            return {}

LOCK = threading.Lock()
PROC = None
MID = [0]
STATE = {"ready": False, "calls": 0, "started": 0.0}


def _send(obj):
    PROC.stdin.write(json.dumps(obj) + "\n")
    PROC.stdin.flush()


def _read(timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        line = PROC.stdout.readline()
        if not line:
            time.sleep(0.02)
            continue
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def mcp(method, params, timeout=120):
    """串行调用 MCP，返回 result 里的文本。"""
    with LOCK:
        MID[0] += 1
        mid = MID[0]
        _send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        while True:
            m = _read(timeout)
            if m is None:
                return None
            if m.get("id") == mid:
                return m


def _ref_by_name(name):
    """按可访问名在最新快照里找 ref。空 target 直接透传会让 playwright-mcp
    把 "" 当 CSS 选择器解析，报语法错——和"元素不存在"完全是两码事。"""
    text = tool("browser_snapshot", {})
    for line in text.splitlines():
        if f'"{name}"' in line and "[ref=" in line:
            m = re.search(r"\[ref=([a-z0-9]+)\]", line)
            if m:
                return m.group(1)
    return ""


def tool(name, args, timeout=120, _retry=True):
    r = mcp("tools/call", {"name": name, "arguments": args}, timeout)
    # playwright-mcp 冷启动偶尔返回空内容（实测首次 /snap 拿到 0 字节），重试一次
    if _retry and r and not (r.get("result", {}) or {}).get("content"):
        time.sleep(0.6)
        return tool(name, args, timeout, _retry=False)
    if not r:
        return "MCP 超时"
    if "error" in r:
        return f"MCP 错误: {r['error']}"
    res = r.get("result", {})
    text = "\n".join(c.get("text", "") for c in (res.get("content") or []) if isinstance(c, dict))
    if res.get("isError"):
        return "工具报错:\n" + text
    return text


def start(cdp, extra):
    global PROC
    # snapshot-mode=none：动作后不再附带整页快照（GitHub 这种大页面能省 2s）；
    # timeout-settle=250：动作后只等 250ms 让页面安定。需要看状态时我自己调 /snap。
    cmd = ["npx.cmd", "-y", "@playwright/mcp@latest",
           "--cdp-endpoint", f"http://127.0.0.1:{cdp}",
           "--snapshot-mode", "none", "--timeout-settle", "250"] + extra
    PROC = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                            errors="replace", bufsize=1)
    r = mcp("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "hermes-browser", "version": "1.0"}}, 90)
    if not r or "result" not in r:
        raise SystemExit(f"初始化失败: {r}")
    _send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    STATE["ready"] = True
    STATE["started"] = time.time()


def sync_preview(url):
    """让推流页面跟着我操作的标签走（私有桌面上无法真正切换可见标签）。"""
    try:
        import ports as _p
        host = (url or "").split("//")[-1].split("/")[0]
        if not host:
            return
        q = urllib.parse.urlencode({"url": host, "token": _auth.token() if hasattr(_auth, "token") else ""})
        urllib.request.urlopen(f"http://127.0.0.1:{_p.port_of('stream')}/focus?{q}", timeout=3).read()
    except Exception:  # noqa: BLE001
        pass


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _out(self, text, code=200):
        body = text.encode("utf-8", "replace")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        # 除 /status 外都要 token：这个进程能直接驱动浏览器
        if u.path != "/status" and not _auth.check(self.headers, q):
            return self._out("401 unauthorized: 缺少或错误的 X-Hermes-Token", 401)
        t0 = time.time()
        try:
            if u.path == "/status":
                ident = {}
                try:
                    import ports as _p
                    ident = {"home": _p.home(), "desk": os.environ.get("HERMES_DESK", "HermesDesk")}
                except Exception:  # noqa: BLE001
                    pass
                return self._out(json.dumps({"ready": STATE["ready"], "calls": STATE["calls"],
                                             "uptime": round(time.time() - STATE["started"], 1),
                                             **ident}, ensure_ascii=False))
            if u.path == "/snap":
                text = tool("browser_snapshot", {})
            elif u.path == "/click":
                tgt, name = q.get("target", ""), q.get("element", "")
                if not tgt and name:
                    tgt = _ref_by_name(name)
                    if not tgt:
                        return self._out(f"### Error\nError: 元素不存在：{name}（页面已变？重新 /snap）")
                text = tool("browser_click", {"target": tgt, "element": name})
            elif u.path == "/type":
                tgt, name = q.get("target", ""), q.get("element", "")
                if not tgt and name:
                    tgt = _ref_by_name(name)
                    if not tgt:
                        return self._out(f"### Error\nError: 元素不存在：{name}（页面已变？重新 /snap）")
                text = tool("browser_type", {"target": tgt,
                                             "text": q.get("text", ""),
                                             "element": name})
            elif u.path == "/goto":
                text = tool("browser_navigate", {"url": q.get("url", "")})
                sync_preview(q.get("url", ""))
            elif u.path == "/eval":
                text = tool("browser_evaluate", {"function": q.get("js", "() => 1")})
            elif u.path == "/find":
                text = tool("browser_find", {"query": q.get("text", "")})
            elif u.path == "/key":
                text = tool("browser_press_key", {"key": q.get("key", "Enter")})
            elif u.path == "/tool":
                args = json.loads(q.get("args", "{}"))
                text = tool(q.get("name", ""), args)
            elif u.path == "/back":
                text = tool("browser_navigate_back", {})
            elif u.path == "/tabs":
                text = tool("browser_tabs", {"action": "list"})
            elif u.path == "/tab":
                # 切标签：多开页面时，读验证码和填表单就在同一个浏览器里来回切
                text = tool("browser_tabs", {"action": "select", "index": int(q.get("i", "0"))})
            elif u.path == "/newtab":
                text = tool("browser_tabs", {"action": "new", "url": q.get("url", "about:blank")})
            else:
                return self._out("未知接口", 404)
            STATE["calls"] += 1
            dt = (time.time() - t0) * 1000
            return self._out(f"[{dt:.0f}ms]\n{text}")
        except Exception as e:  # noqa: BLE001
            return self._out(f"异常: {e}", 500)


def _port(v):
    """接受 18763 或 http://127.0.0.1:18763 两种写法。"""
    s = str(v).rstrip("/")
    return int(s.rsplit(":", 1)[-1].rsplit("/", 1)[-1])


def main():
    ap = argparse.ArgumentParser()
    # --cdp-endpoint 是 mcp_server 的参数名，这里一并接受，省得两处命令行记混
    ap.add_argument("--cdp", "--cdp-endpoint", dest="cdp", type=_port,
                    default=int(os.environ.get("HERMES_CDP_PORT", "18763")))
    ap.add_argument("--port", type=int, default=int(os.environ.get("HERMES_DRIVER_PORT", "8897")))
    ap.add_argument("--snapshot-boxes", action="store_true", help="快照里带坐标框")
    a = ap.parse_args()
    extra = ["--snapshot-boxes"] if a.snapshot_boxes else []
    start(a.cdp, extra)
    print(f"[browser] MCP 已连上 CDP {a.cdp}，接口 http://127.0.0.1:{a.port}/snap", flush=True)
    # 端口被占就自动换（直接启动也要有这层）
    port = a.port
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    except OSError:
        import socket as _s
        with _s.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        print(f"[port] {a.port} 已被占用，改用 {port}", flush=True)
        srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.serve_forever()


if __name__ == "__main__":
    main()
