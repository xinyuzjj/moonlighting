"""按文字点击页面元素（CDP 真实鼠标事件 + 光标同步）。

用法: python cdp_click.py <port> "<文字>"
先跑 AX/DOM 找到元素 → 换算窗口坐标 → /cursor 画光标 → 派发 mouseMoved/Pressed/Released
"""
import os
import asyncio
import ctypes
import ctypes.wintypes as w
import json
import sys
import urllib.parse
import urllib.request

import websockets

PORT = int(sys.argv[1])
TEXT = sys.argv[2]
STREAM = os.environ.get("HERMES_STREAM", "http://127.0.0.1:8765")
DESKTOP = os.environ.get("HERMES_DESK", "HermesDesk")

FIND_JS = """
(() => {
  const t = %s;
  const hits = [];
  const rec = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) rec(el.shadowRoot);
      const txt = (el.textContent || '').trim();
      if (!txt || !txt.includes(t)) continue;
      const r = el.getBoundingClientRect();
      const st = getComputedStyle(el);
      if (r.width < 8 || r.height < 8) continue;
      if (st.visibility === 'hidden' || st.display === 'none' || st.pointerEvents === 'none') continue;
      hits.push({tag: el.tagName, x: r.left + r.width / 2, y: r.top + r.height / 2,
                 w: Math.round(r.width), h: Math.round(r.height)});
    }
  };
  rec(document);
  hits.sort((a, b) => (a.w * a.h) - (b.w * b.h));
  return hits;
})()
"""

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.OpenDesktopW.restype = w.HANDLE
user32.OpenDesktopW.argtypes = [w.LPCWSTR, w.DWORD, w.BOOL, w.DWORD]
user32.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]


def window_rect():
    h = user32.OpenDesktopW(DESKTOP, 0, False, 0x10000000)
    best = [None]
    proc = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

    def cb(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        b = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, b, n + 1)
        if b.value and "Chrome" in b.value:
            r = w.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            area = (r.right - r.left) * (r.bottom - r.top)
            if best[0] is None or area > best[0][0]:
                best[0] = (area, r.left, r.top)
        return True

    user32.SetThreadDesktop(h)
    user32.EnumDesktopWindows(h, proc(cb), 0)
    return best[0][1], best[0][2]


def set_cursor(x, y, click=False):
    q = urllib.parse.urlencode({"x": int(x), "y": int(y), "click": 1 if click else 0})
    try:
        urllib.request.urlopen(f"{STREAM}/cursor?{q}", timeout=4).read()
    except Exception:  # noqa: BLE001
        pass


class CDP:
    def __init__(self, ws):
        self.ws = ws
        self.n = 0

    async def call(self, method, **params):
        self.n += 1
        mid = self.n
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            m = json.loads(await self.ws.recv())
            if m.get("id") == mid:
                if "error" in m:
                    raise RuntimeError(f"{method}: {m['error']}")
                return m.get("result", {})

    async def js(self, expr):
        r = await self.call("Runtime.evaluate", expression=expr,
                            returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")


async def main():
    raw = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=5).read()
    targets = json.loads(raw)
    tab = None
    for kw in ("ses.html", "popup.html"):
        for t in targets:
            if kw in t.get("url", "") and t.get("webSocketDebuggerUrl"):
                tab = t
                break
        if tab:
            break
    if not tab:
        print("没有扩展页面标签")
        return
    print(f"[tab] {tab['url'][:90]}")

    async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=20000000) as ws:
        cdp = CDP(ws)
        await cdp.call("Runtime.enable")
        await cdp.call("Page.enable")

        left, top = window_rect()
        ox = (await cdp.js("window.screenX") - left) + max(0, (await cdp.js("window.outerWidth - window.innerWidth")) // 2)
        oy = (await cdp.js("window.screenY") - top) + await cdp.js("window.outerHeight - window.innerHeight")

        hits = await cdp.js(FIND_JS % json.dumps(TEXT))
        print(f"[find] {TEXT!r} -> {hits}")
        if not hits:
            print("没找到可点元素")
            return
        h = hits[0]
        wx, wy = h["x"] + ox, h["y"] + oy
        print(f"[click] {h['tag']} 视口({h['x']:.0f},{h['y']:.0f}) 窗口({wx:.0f},{wy:.0f})")
        set_cursor(wx, wy)
        await asyncio.sleep(0.4)
        set_cursor(wx, wy, click=True)
        for t in ("mouseMoved", "mousePressed", "mouseReleased"):
            await cdp.call("Input.dispatchMouseEvent", type=t, x=h["x"], y=h["y"],
                           button="none" if t == "mouseMoved" else "left",
                           clickCount=0 if t == "mouseMoved" else 1)
            await asyncio.sleep(0.08)

        await asyncio.sleep(2.0)
        print("[url]", await cdp.js("location.href"))
        ax = await cdp.call("Accessibility.getFullAXTree")
        names = []
        for n in ax.get("nodes", []):
            if n.get("ignored"):
                continue
            nm = (n.get("name", {}) or {}).get("value") or ""
            role = (n.get("role", {}) or {}).get("value") or ""
            if nm.strip() and role not in ("StaticText", "InlineTextBox", "generic"):
                names.append(f"{role}:{nm.strip()[:40]}")
        print("[screen]", names[:22])


if __name__ == "__main__":
    asyncio.run(main())
