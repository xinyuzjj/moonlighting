"""标准 MCP server（stdio）：把浏览器能力和凭据能力暴露给任何 MCP 客户端。

接入方式（Claude Desktop / Claude Code / Codex CLI / Hermes 都是同一份）：
    {
      "command": "python",
      "args": ["<绝对路径>/mcp_server.py", "--cdp-endpoint", "http://127.0.0.1:18763"]
    }

Codex 用 TOML：
    [mcp_servers.browser]
    command = "python"
    args = ["<绝对路径>/mcp_server.py", "--cdp-endpoint", "http://127.0.0.1:18763"]
    startup_timeout_sec = 60

设计：浏览器那套（快照/点击/输入/导航/标签页）转发给微软官方的 @playwright/mcp 子进程，
不自己重写无障碍树——它的快照带 ref，比自研靠谱。凭据工具（cred_*）由本进程直接对接
凭据代理，**密码永远不回到模型上下文**。

协议：JSON-RPC 2.0 over stdio，一行一条。stdout 只写协议消息，日志走 stderr。
"""
import argparse
import collections
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

PROTOCOL = "2024-11-05"
LOG = sys.stderr


def log(*a):
    print("[mcp]", *a, file=LOG, flush=True)


def _node_dirs(npx=None):
    """node.exe 所在的目录。npx 只是个壳，没有 node 它自己都起不来（实测）。"""
    dirs = []
    for cand in (os.path.dirname(npx) if npx else "",
                 os.path.dirname(shutil.which("node") or ""),
                 os.environ.get("NVM_SYMLINK", ""),
                 os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "nodejs"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "nodejs")):
        if cand and os.path.exists(os.path.join(cand, "node.exe" if os.name == "nt" else "node")):
            dirs.append(cand)
    return dirs


def _child_env(npx):
    """把 node 目录补进子进程 PATH——MCP 客户端常给一个干净环境。"""
    env = dict(os.environ)
    dirs = _node_dirs(npx)
    if dirs:
        env["PATH"] = os.pathsep.join(dirs + [env.get("PATH", "")])
    return env


def _npx():
    """找 npx。MCP 客户端常用干净 PATH 启动子进程，光靠 PATH 会找不到（实测）。"""
    override = os.environ.get("MOONLIGHTING_NPX")
    if override and os.path.exists(override):
        return override
    found = shutil.which("npx.cmd") or shutil.which("npx")
    if found:
        return found
    # 常见安装位置兜底：nvm / Program Files / AppData
    cands = [
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "nodejs", "npx.cmd"),
        os.path.join(os.environ.get("APPDATA", ""), "npm", "npx.cmd"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "nodejs", "npx.cmd"),
    ]
    for c in cands:
        if c and os.path.exists(c):
            return c
    raise SystemExit(
        "找不到 npx。装 Node 18+，或设 MOONLIGHTING_NPX 指向 npx.cmd 的绝对路径。")


class Child:
    """把 @playwright/mcp 当子进程用，转发 tools/*。"""

    def __init__(self, cdp_endpoint, extra):
        npx = _npx()
        cmd = [npx, "-y", "@playwright/mcp@latest",
               "--cdp-endpoint", cdp_endpoint, "--snapshot-mode", "none",
               "--timeout-settle", "250"] + extra
        self.err = collections.deque(maxlen=40)
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                  errors="replace", bufsize=1, env=_child_env(npx))
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self.lock = threading.Lock()
        self.n = 0
        r = self._call("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                      "clientInfo": {"name": "hermes-mcp", "version": "1.0"}}, 60)
        if not r or "result" not in r:
            tail = "\n".join(self.err) or "（子进程没有输出 stderr）"
            raise SystemExit(f"playwright-mcp 初始化失败: {r}\n--- stderr ---\n{tail}")
        self._notify("notifications/initialized", {})
        tools = self._call("tools/list", {}, 30)
        self.tools = (tools or {}).get("result", {}).get("tools", [])
        log(f"子进程提供 {len(self.tools)} 个工具")

    def _drain_stderr(self):
        # 不能丢：初始化失败时这几十行是唯一的线索
        try:
            for line in self.p.stderr:
                self.err.append(line.rstrip())
        except Exception:  # noqa: BLE001
            pass

    def _send(self, obj):
        self.p.stdin.write(json.dumps(obj) + "\n")
        self.p.stdin.flush()

    def _read(self, timeout):
        end = time.time() + timeout
        while time.time() < end:
            line = self.p.stdout.readline()
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

    def _call(self, method, params, timeout=120):
        with self.lock:
            self.n += 1
            mid = self.n
            self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
            while True:
                m = self._read(timeout)
                if m is None:
                    return None
                if m.get("id") == mid:
                    return m

    def _notify(self, method, params):
        with self.lock:
            self._send({"jsonrpc": "2.0", "method": method, "params": params})


# ---------- 凭据工具（本进程实现，值不回传） ----------

def _broker():
    try:
        import ports
        return "http://127.0.0.1:%d" % ports.port_of("broker")
    except Exception:  # noqa: BLE001
        return os.environ.get("HERMES_BROKER", "http://127.0.0.1:8899")


def _auth_headers():
    try:
        import auth
        return auth.headers_for_client()
    except Exception:  # noqa: BLE001
        return {}


def _get(path, timeout=60):
    req = urllib.request.Request(_broker() + path, headers=_auth_headers())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


CRED_TOOLS = [
    {
        "name": "cred_list",
        "description": "列出已保存的凭据（只有 handle/label/origin，没有密码）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "cred_fill",
        "description": ("把已保存的账号密码填进当前页面并可选提交。密码由本机凭据代理解密并注入，"
                        "不会出现在返回值或模型上下文里。origin 必须与保存时绑定的一致。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "handle": {"type": "string", "description": "cred_list 返回的 handle；不给则按 origin 找"},
                "origin": {"type": "string", "description": "例如 https://github.com"},
                "url": {"type": "string", "description": "页面 URL 关键字，用于在多个标签里定位"},
                "id_selector": {"type": "string", "description": "账号框选择器，如 #login_field"},
                "pwd_selector": {"type": "string", "description": "密码框选择器，默认 input[type=password]"},
                "submit_selector": {"type": "string", "description": "提交按钮选择器，给了就会点"},
                "cdp_port": {"type": "integer", "description": "浏览器 CDP 端口，默认取 ports.json"},
            },
            "required": ["origin"],
        },
    },
]


def handle_cred(name, args):
    if name == "cred_list":
        return _get("/list")
    if name == "cred_fill":
        handle = args.get("handle") or ""
        origin = (args.get("origin") or "").rstrip("/")
        if not handle:
            items = _get("/list").get("items", [])
            hit = [i for i in items if i["origin"].rstrip("/") == origin]
            if not hit:
                return {"ok": False, "code": "no_handle",
                        "error": f"没有绑定 {origin} 的凭据，先让用户保存一次"}
            handle = hit[-1]["handle"]
        cdp = args.get("cdp_port")
        if not cdp:
            try:
                import ports
                cdp = ports.port_of("cdp")
            except Exception:  # noqa: BLE001
                cdp = 18763
        q = urllib.parse.urlencode({
            "handle": handle, "cdp": cdp, "url": args.get("url", ""),
            "id_sel": args.get("id_selector", ""),
            "pwd_sel": args.get("pwd_selector", "input[type=password]"),
            "submit_sel": args.get("submit_selector", "")})
        return _get("/inject_sel?" + q, timeout=120)
    return {"ok": False, "error": f"未知凭据工具 {name}"}


# ---------- 主循环 ----------

def reply(mid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    if error:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp-endpoint", default=os.environ.get("HERMES_CDP_ENDPOINT", "http://127.0.0.1:18763"))
    ap.add_argument("--extra", nargs="*", default=[], help="额外传给 playwright-mcp 的参数")
    a = ap.parse_args()

    child = Child(a.cdp_endpoint, a.extra)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}

        if method == "initialize":
            reply(mid, {"protocolVersion": PROTOCOL,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "hermes-browser-kit", "version": "1.0.0"}})
        elif method == "notifications/initialized":
            continue
        elif method == "ping":
            reply(mid, {})
        elif method == "tools/list":
            reply(mid, {"tools": child.tools + CRED_TOOLS})
        elif method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments") or {}
            if name in ("cred_list", "cred_fill"):
                try:
                    out = handle_cred(name, args)
                except Exception as e:  # noqa: BLE001
                    out = {"ok": False, "code": "exception", "error": str(e)[:200]}
                reply(mid, {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
                            "isError": not out.get("ok", False)})
            else:
                r = child._call("tools/call", {"name": name, "arguments": args}, 300)
                if not r:
                    reply(mid, error={"code": -32000, "message": "子进程超时"})
                elif "error" in r:
                    reply(mid, error=r["error"])
                else:
                    reply(mid, r.get("result", {}))
        elif mid is not None:
            reply(mid, error={"code": -32601, "message": f"未实现 {method}"})


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass


def cli():
    """pip 安装后的命令行入口。"""
    raise SystemExit(main())
