"""在 chrome://extensions 里用真实鼠标点击把扩展开关打开。

目标：OKX Wallet。做法：shadow DOM 里找到它的 #enableToggle，
拿到视口坐标 -> 换算成窗口坐标 -> /cursor 画光标 -> CDP 派发真实点击。
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

CDP_PORT = int(os.environ.get("HERMES_CDP_PORT", "18763"))
STREAM = os.environ.get("HERMES_STREAM", "http://127.0.0.1:8765")
DESKTOP = os.environ.get("HERMES_DESK", "HermesDesk")
TARGET_NAME = sys.argv[1] if len(sys.argv) > 1 else "OKX Wallet"

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.OpenDesktopW.restype = w.HANDLE
user32.OpenDesktopW.argtypes = [w.LPCWSTR, w.DWORD, w.BOOL, w.DWORD]
user32.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]

FIND_JS = r"""
(() => {
  const out = [];
  const seen = new Set();
  const rec = (root) => {
    if (!root || seen.has(root)) return;
    seen.add(root);
    const els = root.querySelectorAll('*');
    for (const el of els) {
      if (el.shadowRoot) rec(el.shadowRoot);
      if (el.tagName === 'EXTENSIONS-ITEM' && el.shadowRoot) {
        const nm = el.shadowRoot.querySelector('#name');
        const tgl = el.shadowRoot.querySelector('#enableToggle');
        if (nm && tgl) {
          const r = tgl.getBoundingClientRect();
          out.push({name: (nm.textContent || '').trim(), checked: tgl.checked,
                    x: r.left + r.width / 2, y: r.top + r.height / 2});
        }
      }
    }
  };
  rec(document);
  return out;
})()
"""


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
    urllib.request.urlopen(f"{STREAM}/cursor?{q}", timeout=5).read()


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
    raw = urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json", timeout=5).read()
    tabs = [t for t in json.loads(raw)
            if t.get("type") == "page" and t.get("url", "").startswith("chrome://extensions")]
    if not tabs:
        print("没找到 chrome://extensions 标签页")
        return
    print("[tab]", tabs[0]["url"])

    async with websockets.connect(tabs[0]["webSocketDebuggerUrl"], max_size=20_000_000) as ws:
        cdp = CDP(ws)
        await cdp.call("Runtime.enable")
        await cdp.call("Page.enable")

        left, top = window_rect()
        sx, sy = await cdp.js("window.screenX"), await cdp.js("window.screenY")
        dx = await cdp.js("window.outerWidth - window.innerWidth")
        dy = await cdp.js("window.outerHeight - window.innerHeight")
        ox, oy = (sx - left) + max(0, dx // 2), (sy - top) + dy
        print(f"[cal] 偏移 ({ox}, {oy})")

        items = await cdp.js(FIND_JS)
        print("[extensions]", items)

        target = next((i for i in items if TARGET_NAME.lower() in i["name"].lower()), None)
        if not target:
            print(f"没有找到 {TARGET_NAME}")
            return

        wx, wy = target["x"] + ox, target["y"] + oy
        print(f"[click] {target['name']} checked={target['checked']} 视口({target['x']:.0f},{target['y']:.0f}) 窗口({wx:.0f},{wy:.0f})")

        # 先让光标停在开关上，再点
        set_cursor(wx, wy)
        await asyncio.sleep(0.4)
        set_cursor(wx, wy, click=True)
        for t in ("mouseMoved", "mousePressed", "mouseReleased"):
            await cdp.call("Input.dispatchMouseEvent", type=t,
                           x=target["x"], y=target["y"],
                           button="none" if t == "mouseMoved" else "left",
                           clickCount=0 if t == "mouseMoved" else 1)
            await asyncio.sleep(0.08)

        await asyncio.sleep(1.0)
        after = await cdp.js(FIND_JS)
        print("[after]", after)


if __name__ == "__main__":
    asyncio.run(main())
