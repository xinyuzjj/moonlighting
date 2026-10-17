"""用 CDP 驱动私有桌面上的 Chrome，并把光标位置同步给推流（/cursor）。

固化的坑：
  * --remote-debugging-port 绑不上时**不报错**，只是悄悄失效。启动后必须
    curl /json/version 核对 Browser/User-Agent 是不是自己的 Chrome，否则会
    操纵到别人占着该端口的 Electron 应用（TradingView 之类）。
  * 截图取的是整个窗口（GetWindowRect），CDP 给的是视口坐标，必须标定偏移。

用法:
  python desk_browser.py --port 18763 info
  python desk_browser.py --port 18763 click "input.nav-search-input"
  python desk_browser.py --port 18763 type  "input.nav-search-input" "原神"
  python desk_browser.py --port 18763 press Enter

依赖: websockets（Hermes venv 自带）。
环境变量 DESK_DESKTOP 换桌面名，DESK_STREAM 换推流地址。
"""
import argparse
import asyncio
import ctypes
import ctypes.wintypes as w
import json
import os
import urllib.parse
import urllib.request

import websockets

DESKTOP = os.environ.get("HERMES_DESK", os.environ.get("DESK_DESKTOP", "HermesDesk"))
STREAM = os.environ.get("HERMES_STREAM", os.environ.get("DESK_STREAM", "http://127.0.0.1:8765"))

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.OpenDesktopW.restype = w.HANDLE
user32.OpenDesktopW.argtypes = [w.LPCWSTR, w.DWORD, w.BOOL, w.DWORD]
user32.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]


def window_rect():
    """私有桌面上最大的 Chrome 窗口的 GetWindowRect（屏幕坐标）。"""
    h = user32.OpenDesktopW(DESKTOP, 0, False, 0x10000000)
    if not h:
        raise RuntimeError(f"打不开桌面 {DESKTOP}")
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
                best[0] = (area, r.left, r.top, r.right, r.bottom)
        return True

    user32.SetThreadDesktop(h)
    user32.EnumDesktopWindows(h, proc(cb), 0)
    if not best[0]:
        raise RuntimeError(f"{DESKTOP} 上找不到 Chrome 窗口")
    return best[0][1:]


def check_port(port):
    """关键校验：这个端口的 CDP 到底是不是我们的 Chrome。"""
    raw = urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5).read()
    ver = json.loads(raw)
    print(f"[cdp] Browser={ver.get('Browser')}")
    ua = ver.get("User-Agent", "")
    if "Electron" in ua or "Headless" in ua:
        raise SystemExit(
            f"[cdp] 端口 {port} 属于别的程序（{ua[:60]}…），不是我们的 Chrome。"
            "换一个空闲端口重启 Chrome，别在这个连接上操作。"
        )
    return ver


async def page_ws(port):
    raw = urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5).read()
    pages = [t for t in json.loads(raw) if t.get("type") == "page"]
    if not pages:
        raise RuntimeError("没有 page 目标（Chrome 可能还没开窗口）")
    return pages[0]["webSocketDebuggerUrl"]


class CDP:
    def __init__(self, ws):
        self.ws = ws
        self.n = 0

    async def call(self, method, **params):
        self.n += 1
        mid = self.n
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    async def js(self, expr):
        r = await self.call("Runtime.evaluate", expression=expr,
                            returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")


def set_cursor(x, y, click=False):
    """把光标位置告诉推流端，用户才看得到 AI 在点哪儿。推流没开也不能中断操作。"""
    try:
        q = urllib.parse.urlencode({"x": int(x), "y": int(y), "click": 1 if click else 0})
        urllib.request.urlopen(f"{STREAM}/cursor?{q}", timeout=3).read()
    except OSError:
        pass


async def calibrate(cdp):
    left, top, _r, _b = window_rect()
    sx, sy = await cdp.js("window.screenX"), await cdp.js("window.screenY")
    dx = await cdp.js("window.outerWidth - window.innerWidth")
    dy = await cdp.js("window.outerHeight - window.innerHeight")
    ox = (sx - left) + max(0, dx // 2)
    oy = (sy - top) + dy          # dy = 标题栏+标签栏+工具栏，内容从那里开始
    return ox, oy


async def element_center(cdp, selector):
    expr = ("(() => { const e = document.querySelector(%s);"
            " if (!e) return null; const r = e.getBoundingClientRect();"
            " return {x: r.left + r.width/2, y: r.top + r.height/2,"
            " w: r.width, h: r.height, tag: e.tagName}; })()" % json.dumps(selector))
    return await cdp.js(expr)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("HERMES_CDP_PORT", "18763")))
    ap.add_argument("cmd", nargs="?", default="info")
    ap.add_argument("arg1", nargs="?", default="")
    ap.add_argument("arg2", nargs="?", default="")
    a = ap.parse_args()

    check_port(a.port)
    ws_url = await page_ws(a.port)
    async with websockets.connect(ws_url, max_size=20_000_000) as ws:
        cdp = CDP(ws)
        await cdp.call("Runtime.enable")
        await cdp.call("Page.enable")
        ox, oy = await calibrate(cdp)
        print(f"[cal] 视口在窗口内偏移 = ({ox}, {oy})")

        if a.cmd == "info":
            print("[title]", await cdp.js("document.title"))
            print("[url]", await cdp.js("location.href"))
            print("[viewport]", await cdp.js("`${innerWidth}x${innerHeight}`"))
            return

        if a.cmd in ("click", "type"):
            sel = a.arg1
            box = await element_center(cdp, sel)
            if not box:
                print("找不到元素:", sel)
                return
            wx, wy = box["x"] + ox, box["y"] + oy
            print(f"[el] {sel} -> 视口({box['x']:.0f},{box['y']:.0f}) 窗口({wx:.0f},{wy:.0f}) {box['tag']}")
            set_cursor(wx, wy)                 # 先把光标停到目标上
            await asyncio.sleep(0.25)
            set_cursor(wx, wy, click=True)     # 再打点击涟漪
            await cdp.call("Input.dispatchMouseEvent", type="mouseMoved",
                           x=box["x"], y=box["y"], button="none", clickCount=0)
            await cdp.call("Input.dispatchMouseEvent", type="mousePressed",
                           x=box["x"], y=box["y"], button="left", clickCount=1)
            await cdp.call("Input.dispatchMouseEvent", type="mouseReleased",
                           x=box["x"], y=box["y"], button="left", clickCount=1)
            print("[click] 已派发真实点击")
            if a.cmd == "type":
                await cdp.call("Input.insertText", text=a.arg2)
                print("[type] 已输入:", a.arg2)
            await asyncio.sleep(0.5)
            print("[focused]", await cdp.js(
                "document.activeElement && document.activeElement.tagName + '.' +"
                " (document.activeElement.className||'').slice(0,40)"))
            return

        if a.cmd == "press":
            key = a.arg1
            vk = {"Enter": 13, "Tab": 9, "Escape": 27}.get(key, 0)
            await cdp.call("Input.dispatchKeyEvent", type="keyDown", key=key, code=key,
                           windowsVirtualKeyCode=vk)
            await cdp.call("Input.dispatchKeyEvent", type="keyUp", key=key, code=key,
                           windowsVirtualKeyCode=vk)
            print("[press]", key)
            return


if __name__ == "__main__":
    asyncio.run(main())
