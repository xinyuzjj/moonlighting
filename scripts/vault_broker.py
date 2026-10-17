"""私有浏览器的凭据代理（Credential Broker）。

设计目标（对齐 Hark Handoff 的“令牌化 + 最后一公里注入”）：
1. 密码只存在本服务里，用 Windows DPAPI 加密落盘，谁都拿不到明文
2. agent 只拿到 handle（令牌），永远看不到值
3. 注入由本服务自己做：校验目标页面 origin 与凭据绑定一致后，才通过 CDP 打进输入框
4. 值只在内存里活一次，不写日志、不回显

用法:
  python vault_broker.py --port 8899
  -> 浏览器打开 http://127.0.0.1:8899/ 填一次（掩码输入）
  -> agent 只需调 /inject?handle=...&cdp=18763
"""
import argparse
import base64
import ctypes
import ctypes.wintypes as w
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import websockets
import asyncio

try:
    import ports as _ports
    STORE = _ports.vault_path()
except Exception:  # noqa: BLE001
    _HOME = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "hermes-browser")
    STORE = os.environ.get("HERMES_VAULT") or (
        r"E:\hermes_app\vault\broker.json" if os.path.exists(r"E:\hermes_app\vault\broker.json")
        else os.path.join(_HOME, "vault", "broker.json"))

try:
    import auth as _auth
except ImportError:                      # 单独拷走也能跑，只是没鉴权
    class _auth:                          # noqa: N801
        @staticmethod
        def check(_h, _q):
            return True

# 需要 token 的接口：会读/写凭据或往页面里注入值
PROTECTED = ("/save", "/delete", "/describe", "/inject", "/list")
AUDIT = os.path.join(os.path.dirname(os.path.abspath(STORE)), "audit.jsonl")


def _audit(**rec):
    """谁在什么时候、对哪个 origin、用哪个 handle 做了什么。绝不记录明文。"""
    try:
        os.makedirs(os.path.dirname(AUDIT), exist_ok=True)
        with open(AUDIT, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), **rec},
                               ensure_ascii=False) + "\n")
    except OSError:
        pass
LOCK = threading.Lock()

# ---------- DPAPI ----------
crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", w.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def dpapi_encrypt(text: str) -> str:
    blob_in, blob_out = _blob(text.encode()), DATA_BLOB()
    ok = crypt32.CryptProtectData(ctypes.byref(blob_in), None, None, None, None, 0,
                                  ctypes.byref(blob_out))
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptProtectData failed")
    raw = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    kernel32.LocalFree(blob_out.pbData)
    return base64.b64encode(raw).decode()


def dpapi_decrypt(b64: str) -> str:
    blob_in, blob_out = _blob(base64.b64decode(b64)), DATA_BLOB()
    ok = crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0,
                                    ctypes.byref(blob_out))
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptUnprotectData failed")
    raw = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    kernel32.LocalFree(blob_out.pbData)
    return raw.decode()


def load_store() -> dict:
    try:
        with open(STORE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_store(data: dict) -> None:
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, STORE)


def normalize_origin(url: str) -> str:
    p = urllib.parse.urlsplit((url or "").strip())
    scheme, host = (p.scheme or "").lower(), (p.hostname or "").lower()
    if not scheme or not host:
        raise ValueError(f"无法解析 origin: {url!r}")
    if p.port and not ((scheme == "https" and p.port == 443) or (scheme == "http" and p.port == 80)):
        return f"{scheme}://{host}:{p.port}"
    return f"{scheme}://{host}"


# ---------- CDP ----------
async def _cdp(cdp_port: int, kind: str = "page"):
    raw = urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json", timeout=4).read()
    targets = json.loads(raw)
    page = next((t for t in targets
                 if t.get("type") == "page" and "chrome-extension" in t.get("url", "")), None) \
        or next((t for t in targets if t.get("type") == "page"), None)
    if not page:
        raise RuntimeError("没有可用的页面目标")
    return await websockets.connect(page["webSocketDebuggerUrl"], max_size=20000000)


async def _call(ws, n, method, **params):
    n[0] += 1
    mid = n[0]
    await ws.send(json.dumps({"id": mid, "method": method, "params": params}))
    while True:
        m = json.loads(await ws.recv())
        if m.get("id") == mid:
            if "error" in m:
                raise RuntimeError(f"{method}: {m['error']}")
            return m.get("result", {})


FIND_INPUT_JS = """(() => {
  const vis = (i) => { const s = getComputedStyle(i), r = i.getBoundingClientRect();
    return r.width > 8 && r.height > 8 && s.visibility !== 'hidden' && s.display !== 'none' && !i.disabled; };
  const label = (el) => {
    const own = (el.getAttribute('aria-label') || el.placeholder || el.name || '').trim();
    if (own) return own;
    let p = el.parentElement;
    for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
      const t = (p.textContent || '').trim();
      if (t && t.length <= 24) return t;
    }
    return '';
  };
  const all = [...document.querySelectorAll('input')].filter(vis);
  const pwds = all.filter(i => i.type === 'password');
  // 账号框：密码框之前出现的第一个 text/tel/email
  let ids = [];
  if (pwds.length) {
    const firstPwdIdx = all.indexOf(pwds[0]);
    const cand = all.slice(0, firstPwdIdx).filter(i => ['text','tel','email',''].includes(i.type || ''));
    if (cand.length) ids = [cand[cand.length - 1]];
  }
  if (!pwds.length && !ids.length) ids = all.filter(i => ['text','tel','email',''].includes(i.type || '')).slice(0,1);
  return [...ids, ...pwds].map(el => { const r = el.getBoundingClientRect();
    return {type: el.type || 'text', empty: el.value === '', label: label(el),
            x: r.left + r.width / 2, y: r.top + r.height / 2}; });
})()"""

IFRAMES_JS = """(() => [...document.querySelectorAll('iframe')].map(f => {
  const r = f.getBoundingClientRect();
  return {src: f.src || '', x: r.left, y: r.top};
}))()"""


async def _targets(cdp_port: int):
    raw = urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json", timeout=4).read()
    return json.loads(raw)


def _pick_page(targets, hint: str = ""):
    """选目标页：给了 hint 就按 URL 子串匹配，否则优先扩展页，再退回第一个 page。"""
    pages = [t for t in targets if t.get("type") == "page"]
    if hint:
        for t in pages:
            if hint in (t.get("url") or ""):
                return t
        return None
    return next((t for t in pages if "chrome-extension" in (t.get("url") or "")), None)         or (pages[0] if pages else None)


async def detect_fields(cdp_port: int, hint: str = "") -> dict:
    """读出目标页面上“要填哪些框”——给聊天里的表单用，保证表单和页面一一对应。"""
    targets = await _targets(cdp_port)
    page = _pick_page(targets, hint)
    if not page:
        return {"ok": False, "error": f"没有匹配的页面目标（hint={hint!r}）"}

    ws = await websockets.connect(page["webSocketDebuggerUrl"], max_size=20000000)
    n = [0]
    try:
        origin = (await _call(ws, n, "Runtime.evaluate", expression="location.origin",
                              returnByValue=True)).get("result", {}).get("value") or ""
        title = (await _call(ws, n, "Runtime.evaluate", expression="document.title",
                             returnByValue=True)).get("result", {}).get("value") or ""
        frames = (await _call(ws, n, "Runtime.evaluate", expression=IFRAMES_JS,
                              returnByValue=True)).get("result", {}).get("value") or []
        hits = (await _call(ws, n, "Runtime.evaluate", expression=FIND_INPUT_JS,
                            returnByValue=True)).get("result", {}).get("value") or []
        if not hits:
            for t in targets:
                if "chrome-extension" not in t.get("url", "") or not t.get("webSocketDebuggerUrl"):
                    continue
                if t.get("type") not in ("page", "iframe") or t["id"] == page["id"]:
                    continue
                try:
                    iws = await websockets.connect(t["webSocketDebuggerUrl"], max_size=20000000)
                    try:
                        locs = (await _call(iws, [0], "Runtime.evaluate", expression=FIND_INPUT_JS,
                                            returnByValue=True)).get("result", {}).get("value") or []
                    finally:
                        await iws.close()
                except Exception:  # noqa: BLE001
                    continue
                if not locs:
                    continue
                hits = locs
                break
        return {"ok": True, "origin": normalize_origin(origin) if origin else "",
                "title": title, "fields": [{"type": h["type"], "label": h.get("label", "")}
                                           for h in hits]}
    finally:
        await ws.close()


async def inject_sel(cdp_port: int, expect_origin: str, secret: str, hint: str,
                     id_sel: str, pwd_sel: str, submit_sel: str = "",
                     identifier: str = "") -> dict:
    """按 CSS 选择器实时定位再输入，并读回校验。

    坐标会随页面重排失效（错误提示条一出现，整张表单下移），所以绝不缓存坐标：
    每次都用 querySelector 取当前 rect，输完读回长度确认真的进了对应的框。
    """
    targets = await _targets(cdp_port)
    page = _pick_page(targets, hint)
    if not page:
        return {"ok": False, "code": "no_target", "error": "没有匹配的页面目标"}
    ws = await websockets.connect(page["webSocketDebuggerUrl"], max_size=20000000)
    n = [0]
    try:
        origin = (await _call(ws, n, "Runtime.evaluate", expression="location.origin",
                              returnByValue=True)).get("result", {}).get("value") or ""
        if normalize_origin(origin) != normalize_origin(expect_origin):
            return {"ok": False, "code": "origin_mismatch", "error": f"origin 不匹配：页面 {origin} ≠ 凭据 {expect_origin}"}

        RECT = ("(()=>{const e=document.querySelector(%s);if(!e)return null;"
                "const r=e.getBoundingClientRect();"
                "return {x:r.left+r.width/2,y:r.top+r.height/2};})()")

        async def rect(sel):
            r = await _call(ws, n, "Runtime.evaluate", expression=RECT % json.dumps(sel),
                            returnByValue=True)
            return r.get("result", {}).get("value")

        async def fill(sel, text):
            p = await rect(sel)
            if not p:
                return False
            for t in ("mouseMoved", "mousePressed", "mouseReleased"):
                await _call(ws, n, "Input.dispatchMouseEvent", type=t, x=p["x"], y=p["y"],
                            button="none" if t == "mouseMoved" else "left",
                            clickCount=0 if t == "mouseMoved" else 1)
            await _call(ws, n, "Input.dispatchKeyEvent", type="keyDown", key="a", code="KeyA",
                        windowsVirtualKeyCode=65, nativeVirtualKeyCode=65, modifiers=2)
            await _call(ws, n, "Input.dispatchKeyEvent", type="keyUp", key="a", code="KeyA",
                        windowsVirtualKeyCode=65, nativeVirtualKeyCode=65, modifiers=2)
            await _call(ws, n, "Input.insertText", text=text)
            return True

        if identifier and id_sel:
            if not await fill(id_sel, identifier):
                return {"ok": False, "code": "not_found", "error": f"找不到账号框 {id_sel}"}
        if not await fill(pwd_sel, secret):
            return {"ok": False, "code": "not_found", "error": f"找不到密码框 {pwd_sel}"}

        LENS = ("(()=>{const i=document.querySelector(%s),p=document.querySelector(%s);"
                "return {id_match:i?i.value===%s:null,id_len:i?i.value.length:-1,"
                "pwd_len:p?p.value.length:-1};})()")
        got = (await _call(ws, n, "Runtime.evaluate",
                           expression=LENS % (json.dumps(id_sel or "input"), json.dumps(pwd_sel),
                                              json.dumps(identifier or "")),
                           returnByValue=True)).get("result", {}).get("value") or {}
        ok_id = (not identifier) or got.get("id_match") is True
        ok_pwd = got.get("pwd_len") == len(secret)
        if not (ok_id and ok_pwd):
            return {"ok": False, "code": "verify_failed", "error": "读回校验不通过，已放弃提交",
                    "id_len": got.get("id_len"), "pwd_len": got.get("pwd_len"),
                    "expect_pwd_len": len(secret)}

        submitted = False
        if submit_sel:
            p = await rect(submit_sel)
            if p:
                for t in ("mouseMoved", "mousePressed", "mouseReleased"):
                    await _call(ws, n, "Input.dispatchMouseEvent", type=t, x=p["x"], y=p["y"],
                                button="none" if t == "mouseMoved" else "left",
                                clickCount=0 if t == "mouseMoved" else 1)
                submitted = True
        return {"ok": True, "verified": True, "submitted": submitted,
                "id_match": got.get("id_match"), "id_len": got.get("id_len"),
                "pwd_len": got.get("pwd_len")}
    finally:
        await ws.close()


async def type_at(cdp_port: int, expect_origin: str, hint: str, pwd_xy, secret: str,
                  identifier: str = "", id_xy=(0.0, 0.0)) -> dict:
    """在指定页面坐标处输入：账号 -> id_xy，密码 -> pwd_xy。坐标由调用方算好。"""
    targets = await _targets(cdp_port)
    page = _pick_page(targets, hint)
    if not page:
        return {"ok": False, "error": "没有匹配的页面目标"}
    ws = await websockets.connect(page["webSocketDebuggerUrl"], max_size=20000000)
    n = [0]
    try:
        origin = (await _call(ws, n, "Runtime.evaluate", expression="location.origin",
                              returnByValue=True)).get("result", {}).get("value") or ""
        if normalize_origin(origin) != normalize_origin(expect_origin):
            return {"ok": False, "error": f"origin 不匹配：页面 {origin} ≠ 凭据 {expect_origin}"}

        async def click_type(x, y, text):
            for t in ("mouseMoved", "mousePressed", "mouseReleased"):
                await _call(ws, n, "Input.dispatchMouseEvent", type=t, x=x, y=y,
                            button="none" if t == "mouseMoved" else "left",
                            clickCount=0 if t == "mouseMoved" else 1)
            # 先 Ctrl+A 全选再输入：框里已有内容时 insertText 是「追加」，
            # 重试第二次会把邮箱变成两遍，登录必失败。
            await _call(ws, n, "Input.dispatchKeyEvent", type="keyDown", key="a", code="KeyA",
                        windowsVirtualKeyCode=65, nativeVirtualKeyCode=65, modifiers=2)
            await _call(ws, n, "Input.dispatchKeyEvent", type="keyUp", key="a", code="KeyA",
                        windowsVirtualKeyCode=65, nativeVirtualKeyCode=65, modifiers=2)
            await _call(ws, n, "Input.insertText", text=text)

        done = 0
        if identifier and id_xy[0] and id_xy[1]:
            await click_type(id_xy[0], id_xy[1], identifier)
            done += 1
        await click_type(pwd_xy[0], pwd_xy[1], secret)
        done += 1
        return {"ok": True, "fields": done, "origin": normalize_origin(origin)}
    finally:
        await ws.close()


async def inject(cdp_port: int, expect_origin: str, secret: str, hint: str = "", identifier: str = "") -> dict:
    """在 CDP 页面上找到第一个可见输入框并注入。

    关键：钱包这类 UI 在 OOPIF(ses.html) 里 —— 元素要在 iframe 目标上找坐标，
    但鼠标/键盘必须发给顶层 page 目标（发给 iframe 会被丢弃）。
    """
    targets = await _targets(cdp_port)
    page = _pick_page(targets, hint)
    if not page:
        return {"ok": False, "error": f"没有匹配的页面目标（hint={hint!r}）"}

    ws = await websockets.connect(page["webSocketDebuggerUrl"], max_size=20000000)
    n = [0]
    try:
        r = await _call(ws, n, "Runtime.evaluate", expression="location.origin", returnByValue=True)
        origin = r.get("result", {}).get("value") or ""
        if normalize_origin(origin) != normalize_origin(expect_origin):
            return {"ok": False, "error": f"origin 不匹配：页面 {origin} ≠ 凭据 {expect_origin}"}

        frames = (await _call(ws, n, "Runtime.evaluate", expression=IFRAMES_JS,
                              returnByValue=True)).get("result", {}).get("value") or []

        # 先在顶层页面找（返回列表）
        hits = (await _call(ws, n, "Runtime.evaluate", expression=FIND_INPUT_JS,
                            returnByValue=True)).get("result", {}).get("value") or []
        hit_origin = origin

        # 顶层没有 -> 逐个 iframe 目标里找，再加 iframe 偏移
        if not hits:
            for t in targets:
                if not t.get("webSocketDebuggerUrl"):
                    continue
                if t.get("type") not in ("page", "iframe") or t["id"] == page["id"]:
                    continue
                try:
                    iws = await websockets.connect(t["webSocketDebuggerUrl"], max_size=20000000)
                    try:
                        locs = (await _call(iws, [0], "Runtime.evaluate", expression=FIND_INPUT_JS,
                                            returnByValue=True)).get("result", {}).get("value") or []
                    finally:
                        await iws.close()
                except Exception:  # noqa: BLE001
                    continue
                if not locs:
                    continue
                # 目标 url 形如 .../ses.html?sestheme=light#/password-type?from=create
                # 取路径最后一段（ses.html），不能在 ? 前截断就把 #hash 也带进去了
                name = t["url"].split("#")[0].split("?")[0].split("/")[-1]
                off = next((f for f in frames if name and name in f["src"]), None)
                ox = off["x"] if off else 0.0
                oy = off["y"] if off else 0.0
                hits = [{"type": h["type"], "empty": h["empty"], "x": h["x"] + ox, "y": h["y"] + oy}
                        for h in locs]
                hit_origin = origin          # 信任锚点是用户看到的那个页面 origin
                break

        if not hits:
            return {"ok": False, "error": "页面上没有可见的输入框"}

        # 只填空的；如果全都已有内容，就填第一个
        targets_to_fill = [h for h in hits if h.get("empty", True)] or hits[:1]
        filled = 0
        for h in targets_to_fill:
            val = identifier if (h.get("type") not in ("password",) and identifier) else secret
            if h.get("type") not in ("password",) and not identifier:
                continue                     # 账号框没给值就跳过
            for t in ("mouseMoved", "mousePressed", "mouseReleased"):
                await _call(ws, n, "Input.dispatchMouseEvent", type=t, x=h["x"], y=h["y"],
                            button="none" if t == "mouseMoved" else "left",
                            clickCount=0 if t == "mouseMoved" else 1)
            await _call(ws, n, "Input.dispatchKeyEvent", type="keyDown", key="a", code="KeyA",
                        windowsVirtualKeyCode=65, nativeVirtualKeyCode=65, modifiers=2)
            await _call(ws, n, "Input.dispatchKeyEvent", type="keyUp", key="a", code="KeyA",
                        windowsVirtualKeyCode=65, nativeVirtualKeyCode=65, modifiers=2)
            await _call(ws, n, "Input.insertText", text=val)
            filled += 1
        return {"ok": True, "fields": filled,
                "kind": targets_to_fill[0]["type"],
                "origin": normalize_origin(hit_origin)}
    finally:
        await ws.close()


# ---------- HTTP ----------
PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>凭据代理</title>
<style>
 body{font-family:system-ui;max-width:520px;margin:32px auto;padding:0 16px;color:#111}
 h2{font-size:16px;margin:0 0 4px}
 p{font-size:12px;color:#666;margin:0 0 16px}
 label{display:block;font-size:12px;margin:12px 0 4px;color:#333}
 input{width:100%;box-sizing:border-box;padding:10px;font-size:14px;
       border:1px solid #d4d4d8;border-radius:8px}
 button{margin-top:18px;width:100%;padding:12px;font-size:14px;border:0;
        border-radius:8px;background:#111;color:#fff;cursor:pointer}
 .out{margin-top:14px;font-size:12px;color:#0a7}
</style></head><body>
<h2>凭据代理 · 本地加密存储</h2>
<p>密码用 Windows DPAPI 加密后存在本机，只有这台机器的这个用户能解开。agent 只拿到一个 handle。</p>
<label>这个凭据属于哪个页面（Origin）</label>
<input id="origin" placeholder="https://www.okx.com">
<label>标签</label>
<input id="label" placeholder="OKX 登录">
<label>密码 / 密钥（掩码输入）</label>
<input id="secret" type="password" placeholder="只在本页与本地服务之间传递">
<button id="save">加密保存</button>
<div class="out" id="out"></div>
<script>
const qp = new URLSearchParams(location.search);
if (qp.get('origin')) document.getElementById('origin').value = qp.get('origin');
if (qp.get('label')) document.getElementById('label').value = qp.get('label');
document.getElementById('save').onclick = async () => {
  const body = {
    origin: document.getElementById('origin').value.trim(),
    label: document.getElementById('label').value.trim(),
    secret: document.getElementById('secret').value,
  };
  document.getElementById('secret').value = '';
  const r = await fetch('/save', {method:'POST', headers:{'Content-Type':'application/json'},
                                  body: JSON.stringify(body)});
  const j = await r.json();
  document.getElementById('out').textContent = j.ok ? ('已保存，handle = ' + j.handle) : ('失败：' + j.error);
};
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 允许聊天里的本地页面直接 POST（file:// 发起的请求 Origin 为 null，* 能覆盖）
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204, b"")

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(u.query)
        # 凭据相关接口一律要 token：同机任何进程都能调 8899，不加就等于把密码交出去
        if any(u.path.startswith(p) for p in PROTECTED) and not _auth.check(self.headers, q):
            return self._send(401, json.dumps({"ok": False, "code": "unauthorized",
                                               "error": "缺少或错误的 X-Hermes-Token"}))
        if u.path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if u.path == "/delete":
            handle = (q.get("handle") or [""])[0]
            data = load_store()
            if handle not in data:
                return self._send(404, json.dumps({"ok": False, "error": "handle 不存在"}))
            del data[handle]
            save_store(data)
            return self._send(200, json.dumps({"ok": True, "deleted": handle}))
        if u.path == "/health":
            return self._send(200, json.dumps({"ok": True, "service": "vault-broker"}))
        if u.path == "/list":
            data = load_store()
            return self._send(200, json.dumps({
                "items": [{"handle": k, "label": v["label"], "origin": v["origin"]}
                          for k, v in data.items()]}, ensure_ascii=False))
        if u.path == "/form":
            # 必须用 http 提供：file:// 页面无法 fetch http://127.0.0.1（Chrome 直接拦）
            here = os.path.dirname(os.path.abspath(__file__))
            cands = [os.environ.get("MOONLIGHTING_TEMPLATES", ""), here,
                     os.path.join(sys.prefix, "share", "moonlighting")]
            fpath = next((os.path.join(c, "cred_form.html") for c in cands
                          if c and os.path.exists(os.path.join(c, "cred_form.html"))), "")
            try:
                with open(fpath, encoding="utf-8") as fh:
                    html = fh.read()
            except OSError as e:
                return self._send(500, f"form 缺失: {e}", "text/plain; charset=utf-8")
            return self._send(200, html, "text/html; charset=utf-8")
        if u.path == "/describe":
            port = int((q.get("cdp") or ["0"])[0])
            try:
                res = asyncio.run(detect_fields(port, (q.get("url") or [""])[0]))
            except Exception as e:  # noqa: BLE001
                res = {"ok": False, "error": str(e)[:200]}
            return self._send(200, json.dumps(res, ensure_ascii=False))
        if u.path == "/inject_sel":
            handle = (q.get("handle") or [""])[0]
            item = load_store().get(handle)
            if not item:
                _audit(action="inject_sel", handle=handle, ok=False, code="no_handle")
                return self._send(404, json.dumps({"ok": False, "code": "no_handle",
                                                   "error": "handle 不存在"}))
            try:
                secret = dpapi_decrypt(item["blob"])
            except Exception as e:  # noqa: BLE001
                return self._send(500, json.dumps({"ok": False, "code": "decrypt_failed",
                                                   "error": f"解密失败: {e}"}))
            try:
                res = asyncio.run(inject_sel(
                    int((q.get("cdp") or ["0"])[0]), item["origin"], secret,
                    (q.get("url") or [""])[0],
                    (q.get("id_sel") or [""])[0], (q.get("pwd_sel") or ["input[type=password]"])[0],
                    (q.get("submit_sel") or [""])[0], item.get("identifier", "")))
            except Exception as e:  # noqa: BLE001
                res = {"ok": False, "code": "exception", "error": str(e)[:200]}
            finally:
                del secret
            res.setdefault("code", "ok" if res.get("ok") else "failed")
            _audit(action="inject_sel", origin=item["origin"], handle=handle,
                   ok=bool(res.get("ok")), code=res["code"])
            return self._send(200, json.dumps(res, ensure_ascii=False))
        if u.path == "/inject_at":
            handle = (q.get("handle") or [""])[0]
            port = int((q.get("cdp") or ["0"])[0])
            hint = (q.get("url") or [""])[0]
            try:
                px, py = float((q.get("x") or ["0"])[0]), float((q.get("y") or ["0"])[0])
            except ValueError:
                return self._send(400, json.dumps({"ok": False, "error": "bad x/y"}))
            item = load_store().get(handle)
            if not item:
                return self._send(404, json.dumps({"ok": False, "error": "handle 不存在"}))
            try:
                secret = dpapi_decrypt(item["blob"])
            except Exception as e:  # noqa: BLE001
                return self._send(500, json.dumps({"ok": False, "error": f"解密失败: {e}"}))
            try:
                res = asyncio.run(type_at(port, item["origin"], hint,
                                          (px, py), secret,
                                          item.get("identifier", ""),
                                          (float((q.get("ix") or ["0"])[0]),
                                           float((q.get("iy") or ["0"])[0]))))
            except Exception as e:  # noqa: BLE001
                res = {"ok": False, "error": str(e)[:200]}
            finally:
                del secret
            return self._send(200, json.dumps(res, ensure_ascii=False))
        if u.path == "/inject":
            handle = (q.get("handle") or [""])[0]
            port = int((q.get("cdp") or ["0"])[0])
            data = load_store()
            item = data.get(handle)
            if not item:
                return self._send(404, json.dumps({"ok": False, "error": "handle 不存在"}))
            try:
                secret = dpapi_decrypt(item["blob"])
            except Exception as e:  # noqa: BLE001
                return self._send(500, json.dumps({"ok": False, "error": f"解密失败: {e}"}))
            try:
                res = asyncio.run(inject(port, item["origin"], secret, (q.get("url") or [""])[0],
                                         item.get("identifier", "")))
            except Exception as e:  # noqa: BLE001
                res = {"ok": False, "error": str(e)[:200]}
            finally:
                del secret
            return self._send(200, json.dumps(res, ensure_ascii=False))
        self._send(404, json.dumps({"ok": False, "error": "not found"}))

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        if not _auth.check(self.headers, urllib.parse.parse_qs(u.query)):
            return self._send(401, json.dumps({"ok": False, "code": "unauthorized",
                                               "error": "缺少或错误的 X-Hermes-Token"}))
        if u.path != "/save":
            return self._send(404, json.dumps({"ok": False, "error": "not found"}))
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
            origin = normalize_origin(payload.get("origin", ""))
            secret = payload.get("secret") or ""
            identifier = (payload.get("identifier") or "").strip()
            label = (payload.get("label") or origin).strip()[:60]
            if len(secret) < 4:
                raise ValueError("内容太短")
        except Exception as e:  # noqa: BLE001
            return self._send(400, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))

        handle = "cred_" + uuid.uuid4().hex[:12]
        with LOCK:
            data = load_store()
            data[handle] = {"label": label, "origin": origin, "blob": dpapi_encrypt(secret),
                            "identifier": identifier}
            save_store(data)
        self._send(200, json.dumps({"ok": True, "handle": handle}, ensure_ascii=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("HERMES_BROKER_PORT", "8899")))
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    srv.daemon_threads = True
    print(f"[broker] http://127.0.0.1:{a.port}/  存储: {STORE}", flush=True)
    srv.serve_forever()
