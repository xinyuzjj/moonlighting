"""把私有桌面 (HermesDesk) 上的窗口实时推成 MJPEG，方便在聊天窗口里看。

用法:
  python desk_stream.py --spawn "https://example.com"     # 在私有桌面开 Chrome
  python desk_stream.py --port 8765                       # 只推流

HTTP:
  /            简单页面
  /snapshot    单帧 JPEG
  /stream.mjpg 连续 MJPEG
"""
import os
import argparse
import asyncio
import base64
import ctypes
import ctypes.wintypes as w
import io
import json
import threading
import time
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image
try:
    import auth as _auth
except ImportError:
    class _auth:   # 单独拷走也能跑
        @staticmethod
        def check(_h, _q): return True
        @staticmethod
        def token(): return ""

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

GENERIC_ALL = 0x10000000
PW_RENDERFULLCONTENT = 0x00000002
DESKTOP = os.environ.get("HERMES_DESK", "HermesDesk")

user32.CreateDesktopW.restype = w.HANDLE
user32.CreateDesktopW.argtypes = [w.LPCWSTR, w.LPCWSTR, ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.c_void_p]
user32.OpenDesktopW.restype = w.HANDLE
user32.OpenDesktopW.argtypes = [w.LPCWSTR, w.DWORD, w.BOOL, w.DWORD]
user32.EnumDesktopWindows.restype = w.BOOL
user32.SetThreadDesktop.restype = w.BOOL
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [w.HWND]
user32.PrintWindow.argtypes = [w.HWND, w.HDC, w.UINT]
user32.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
user32.GetWindowDC.restype = w.HDC


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", w.DWORD), ("lpReserved", w.LPWSTR), ("lpDesktop", w.LPWSTR),
                ("lpTitle", w.LPWSTR), ("dwX", w.DWORD), ("dwY", w.DWORD),
                ("dwXSize", w.DWORD), ("dwYSize", w.DWORD),
                ("dwXCountChars", w.DWORD), ("dwYCountChars", w.DWORD),
                ("dwFillAttribute", w.DWORD), ("dwFlags", w.DWORD),
                ("wShowWindow", w.WORD), ("cbReserved2", w.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", w.HANDLE),
                ("hStdOutput", w.HANDLE), ("hStdError", w.HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", w.HANDLE), ("hThread", w.HANDLE),
                ("dwProcessId", w.DWORD), ("dwThreadId", w.DWORD)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", w.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", w.WORD), ("biBitCount", w.WORD), ("biCompression", w.DWORD),
                ("biSizeImage", w.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", w.DWORD),
                ("biClrImportant", w.DWORD)]


def desktop_handle():
    h = user32.OpenDesktopW(DESKTOP, 0, False, GENERIC_ALL)
    if not h:
        h = user32.CreateDesktopW(DESKTOP, None, None, 0, GENERIC_ALL, None)
    if not h:
        raise OSError(ctypes.get_last_error(), "open/create desktop failed")
    return h


def spawn_on_desktop(cmdline):
    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    si.lpDesktop = DESKTOP
    pi = PROCESS_INFORMATION()
    ok = kernel32.CreateProcessW(None, ctypes.c_wchar_p(cmdline), None, None, False,
                                 0, None, None, ctypes.byref(si), ctypes.byref(pi))
    if not ok:
        raise OSError(ctypes.get_last_error(), "CreateProcessW failed")
    return pi


def list_windows(hdesk):
    out = []
    proc = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

    def _cb(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        b = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, b, n + 1)
        r = w.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        out.append({"hwnd": hwnd, "title": b.value,
                    "vis": bool(user32.IsWindowVisible(hwnd)),
                    "w": r.right - r.left, "h": r.bottom - r.top})
        return True

    user32.SetThreadDesktop(hdesk)
    user32.EnumDesktopWindows(hdesk, proc(_cb), 0)
    return out


try:
    dwmapi = ctypes.WinDLL("dwmapi")
except OSError:                      # 老系统没有 DWM
    dwmapi = None


def grab(hwnd, width, height, quality=70):
    # 私有桌面上的窗口没有前台焦点，Chrome 会停止重绘，PrintWindow 只能拿到旧帧。
    # 先把它在自己桌面上置前，再等合成器刷新，画面才会跟着页面走。
    user32.ShowWindow(hwnd, 5)                  # SW_SHOW
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    if dwmapi:
        try:
            dwmapi.DwmFlush()
        except Exception:  # noqa: BLE001
            pass
    hdc_screen = user32.GetWindowDC(hwnd)
    if not hdc_screen:
        return None
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    bmp = gdi32.CreateCompatibleBitmap(hdc_screen, width, height)
    gdi32.SelectObject(hdc_mem, bmp)
    user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT)
    buf = ctypes.create_string_buffer(width * height * 4)
    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(bi)
    bi.biWidth = width
    bi.biHeight = -height
    bi.biPlanes = 1
    bi.biBitCount = 32
    gdi32.GetDIBits(hdc_mem, bmp, 0, height, buf, ctypes.byref(bi), 0)
    img = Image.frombuffer("RGB", (width, height), buf, "raw", "BGRX", 0, 1)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(hwnd, hdc_screen)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=quality)
    return out.getvalue()


STATE = {"frame": None, "title": "", "at": 0.0,
         "cursor": None, "cursor_at": 0.0, "click_at": 0.0}

# ---- 反向输入通道：预览页 -> 私有桌面 Chrome（CDP） ----
import queue as _q

IN_Q = _q.Queue()
_cdp_id = [0]


def _cdp_ws_url(port):
    import urllib.request as _u
    raw = _u.urlopen(f"http://127.0.0.1:{port}/json", timeout=4).read()
    targets = json.loads(raw)
    # 输入必须发给顶层 page：Chrome 会把页面坐标正确路由进 OOPIF 里的输入框。
    # （发给 iframe 目标反而失效——坐标被当成 iframe 自己的视口坐标）
    for t in targets:
        if t.get("type") == "page" and "popup.html" in t.get("url", ""):
            return t["webSocketDebuggerUrl"]
    pages = [t for t in targets if t.get("type") == "page"]
    if not pages:
        raise RuntimeError("没有可驱动的标签页")
    return pages[0]["webSocketDebuggerUrl"]


def input_worker(cdp_port):
    """后台线程：把队列里的输入指令发到 CDP。"""
    from websockets.sync.client import connect

    def loop():
        ws = None
        while True:
            msg = IN_Q.get()
            if msg is None:
                return
            try:
                if ws is None:
                    ws = connect(_cdp_ws_url(cdp_port), max_size=20000000)
                _cdp_id[0] += 1
                mid = _cdp_id[0]
                payload = dict(msg, id=mid)
                ws.send(json.dumps(payload))
                while True:
                    reply = json.loads(ws.recv())
                    if reply.get("id") == mid:
                        break
            except Exception:  # noqa: BLE001
                try:
                    ws.close()
                except Exception:  # noqa: BLE001
                    pass
                ws = None

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


def draw_cursor(img, pos, since_move, click_age):
    """在帧上画一个假光标 + 点击涟漪，让人看清 AI 在点哪儿。"""
    from PIL import ImageDraw
    d = ImageDraw.Draw(img, "RGBA")
    x, y = pos
    # 停留超过 2.5 秒就淡出，免得挡住内容
    alpha = 255 if since_move < 2.5 else max(0, int(255 * (1 - (since_move - 2.5) / 1.5)))
    if alpha <= 0:
        return img
    if click_age is not None and 0 <= click_age < 0.7:
        t = click_age / 0.7
        r = int(12 + 46 * t)
        d.ellipse([x - r, y - r, x + r, y + r],
                  outline=(255, 70, 70, int(220 * (1 - t))), width=3)
    s = 1.7  # 箭头缩放，1280 宽窗口里看得清
    pts = [(x, y), (x, y + 23 * s), (x + 6.5 * s, y + 17 * s),
           (x + 11.5 * s, y + 27 * s), (x + 16 * s, y + 25 * s),
           (x + 11 * s, y + 15.5 * s), (x + 18 * s, y + 15.5 * s)]
    d.polygon(pts, fill=(255, 255, 255, alpha), outline=(0, 0, 0, alpha))
    return img


def grab_cdp(port, quality=70, prefer=""):
    """用 CDP 截「当前可见标签页」。

    私有桌面从不切到前台，DWM 不会合成它，PrintWindow 只能拿到上一次合成的旧帧
    （画面卡住不动）。渲染进程自己截图不依赖 DWM，永远是最新的。
    """
    import urllib.request as _u
    try:
        targets = json.loads(_u.urlopen(f"http://127.0.0.1:{port}/json", timeout=3).read())
    except Exception:  # noqa: BLE001
        return None
    import websockets

    pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if prefer:                      # 点名要看哪个标签，就不管可见性
        pages = [t for t in pages if prefer in (t.get("url") or "")] or pages

    for t in pages:

        async def shot(url):
            async with websockets.connect(url, max_size=40000000) as ws:
                await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                          "params": {"expression": "document.visibilityState",
                                                     "returnByValue": True}}))
                while True:
                    m = json.loads(await ws.recv())
                    if m.get("id") == 1:
                        if not prefer and (m.get("result", {}).get("result", {}).get("value")) != "visible":
                            return None          # 没点名时只截前台标签页
                        break
                await ws.send(json.dumps({"id": 2, "method": "Page.captureScreenshot",
                                          "params": {"format": "jpeg", "quality": quality}}))
                while True:
                    m = json.loads(await ws.recv())
                    if m.get("id") == 2:
                        d = m.get("result", {}).get("data")
                        return base64.b64decode(d) if d else None

        try:
            jpg = asyncio.run(shot(t["webSocketDebuggerUrl"]))
        except Exception:  # noqa: BLE001
            continue
        if jpg:
            return jpg
    return None


def screencast_worker(stop):
    """事件驱动的实时画面：一条常驻 WebSocket + Page.startScreencast。

    之前每帧都新建 WS 再 Page.captureScreenshot，0.3s 一次，等于每秒重连三次，
    页面一大就卡。screencast 是渲染进程主动推：没变化不发，有变化才来一帧。
    """
    import websockets

    cur = {"url": None, "ws": None}

    async def run(url_filter):
        targets = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{STATE['cdp']}/json", timeout=4).read())
        pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        if url_filter:
            pages = [t for t in pages if url_filter in (t.get("url") or "")] or pages
        if not pages:
            return False
        tgt = pages[0]
        async with websockets.connect(tgt["webSocketDebuggerUrl"], max_size=40000000) as ws:
            cur["ws"] = ws
            n = 0

            async def call(method, **params):
                nonlocal n
                n += 1
                await ws.send(json.dumps({"id": n, "method": method, "params": params}))
                while True:
                    m = json.loads(await ws.recv())
                    if m.get("id") == n:
                        return m.get("result", {})
                    if m.get("method") == "Page.screencastFrame":
                        await handle(m["params"])

            async def handle(prm):
                import base64
                STATE["frame"] = base64.b64decode(prm["data"])
                STATE["at"] = time.time()
                STATE["title"] = "screencast"
                try:
                    await ws.send(json.dumps({"id": 900000 + prm["sessionId"],
                                              "method": "Page.screencastFrameAck",
                                              "params": {"sessionId": prm["sessionId"]}}))
                except Exception:  # noqa: BLE001
                    pass

            await call("Page.enable")
            await call("Page.startScreencast", format="jpeg", quality=70,
                       maxWidth=1600, maxHeight=1200, everyNthFrame=1)
            cur["url"] = url_filter
            # 只收事件，直到需要换目标或进程结束
            while not stop.is_set():
                if STATE.get("shot_url", "") != cur["url"]:
                    try:
                        await call("Page.stopScreencast")
                    except Exception:  # noqa: BLE001
                        pass
                    return True
                try:
                    m = json.loads(await asyncio.wait_for(ws.recv(), timeout=1.0))
                except asyncio.TimeoutError:
                    continue
                if m.get("method") == "Page.screencastFrame":
                    await handle(m["params"])
            return True

    while not stop.is_set():
        if not STATE.get("cdp"):
            time.sleep(1)
            continue
        try:
            asyncio.run(run(STATE.get("shot_url", "")))
        except Exception:  # noqa: BLE001
            STATE["title"] = "screencast 重连中"
            time.sleep(1.0)


def pump(stop):
    hdesk = desktop_handle()

    def loop():
        while not stop.is_set():
            try:
                # screencast 在跑就别抢：只有它 3 秒没出帧才兜底截一张
                if STATE.get("at") and time.time() - STATE["at"] < 3:
                    time.sleep(1.0)
                    continue
                jpg = grab_cdp(STATE["cdp"], prefer=STATE.get("shot_url", "")) if STATE.get("cdp") else None
                if jpg:
                    STATE["frame"] = jpg
                    STATE["title"] = "CDP 兜底截图"
                    STATE["at"] = time.time()
                    time.sleep(0.3)
                    continue
                wins = [x for x in list_windows(hdesk)
                        if x["w"] > 200 and x["h"] > 150
                        and x["title"] not in ("Program Manager", "Tooltip")]
                if wins:
                    tgt = max(wins, key=lambda x: x["w"] * x["h"])
                    jpg = grab(tgt["hwnd"], tgt["w"], tgt["h"])
                    if jpg:
                        cur = STATE.get("cursor")
                        if cur:
                            import io as _io
                            im = Image.open(_io.BytesIO(jpg))
                            now = time.time()
                            ca = (now - STATE["click_at"]) if STATE.get("click_at") else None
                            draw_cursor(im, cur, now - STATE.get("cursor_at", now), ca)
                            buf = _io.BytesIO()
                            im.save(buf, "JPEG", quality=70)
                            jpg = buf.getvalue()
                        STATE["frame"] = jpg
                        STATE["title"] = tgt["title"]
                        STATE["size"] = (tgt["w"], tgt["h"])
                        STATE["at"] = time.time()
            except Exception as e:  # noqa: BLE001
                STATE["title"] = f"capture error: {e}"
            time.sleep(0.35)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # 注入按键/点击的接口必须带 token（同机任何进程都能调 8765）
        if self.path.startswith(("/c?", "/t?", "/k?", "/focus")):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if not _auth.check(self.headers, q):
                body = b"401 unauthorized"
                self.send_response(401)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        if self.path.startswith("/c?"):
            from urllib.parse import urlparse as _up, parse_qs as _pq
            q = _pq(_up(self.path).query)
            try:
                nx = float(q.get("nx", ["0"])[0])
                ny = float(q.get("ny", ["0"])[0])
            except ValueError:
                self.send_error(400, "bad nx/ny")
                return
            sz = STATE.get("size") or (1280, 860)
            wx, wy = nx * sz[0], ny * sz[1]
            vx, vy = wx - self.server.off_x, wy - self.server.off_y
            STATE["cursor"] = (wx, wy)
            STATE["cursor_at"] = time.time()
            STATE["click_at"] = time.time()
            for t in ("mouseMoved", "mousePressed", "mouseReleased"):
                IN_Q.put({"method": "Input.dispatchMouseEvent",
                          "params": {"type": t, "x": vx, "y": vy,
                                     "button": "none" if t == "mouseMoved" else "left",
                                     "clickCount": 0 if t == "mouseMoved" else 1}})
            self._json({"ok": True, "window": [wx, wy], "viewport": [vx, vy]})
        elif self.path.startswith("/t?"):
            from urllib.parse import urlparse as _up, parse_qs as _pq
            q = _pq(_up(self.path).query)
            text = q.get("text", [""])[0]
            if text:
                IN_Q.put({"method": "Input.insertText", "params": {"text": text}})
            self._json({"ok": True, "chars": len(text)})
        elif self.path.startswith("/k?"):
            from urllib.parse import urlparse as _up, parse_qs as _pq
            q = _pq(_up(self.path).query)
            key = q.get("key", [""])[0]
            if key:
                for t in ("keyDown", "keyUp"):
                    IN_Q.put({"method": "Input.dispatchKeyEvent",
                              "params": {"type": t, "key": key, "code": key,
                                         "windowsVirtualKeyCode": 13 if key == "Enter" else 0}})
            self._json({"ok": True, "key": key})
        elif self.path.startswith("/focus"):
            from urllib.parse import urlparse as _u2, parse_qs as _q2
            q = _q2(_u2(self.path).query)
            STATE["shot_url"] = (q.get("url") or [""])[0]
            body = json.dumps({"ok": True, "shot_url": STATE["shot_url"]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/cursor"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            try:
                x = int(float(q.get("x", ["0"])[0]))
                y = int(float(q.get("y", ["0"])[0]))
            except (TypeError, ValueError):
                self.send_error(400, "bad x/y")
                return
            STATE["cursor"] = (x, y)
            STATE["cursor_at"] = time.time()
            clicked = q.get("click", ["0"])[0] in ("1", "true", "yes")
            if clicked:
                STATE["click_at"] = time.time()
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/stream"):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=f")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            last = None
            while True:
                f = STATE["frame"]
                if f and f is not last:
                    last = f
                    try:
                        self.wfile.write(b"--f\r\nContent-Type: image/jpeg\r\n"
                                         + f"Content-Length: {len(f)}\r\n\r\n".encode()
                                         + f + b"\r\n")
                        self.wfile.flush()
                    except OSError:  # 客户端断开:BrokePipe/ConnectionReset/ConnectionAborted 都是 OSError
                        return
                time.sleep(0.3)
        elif self.path.startswith("/ctl") or self.path.startswith("/view"):
            # 页面由服务自己吐出，端口从请求里来——模板里不再写死 8765
            name = "desk_ctl.html" if self.path.startswith("/ctl") else "desk_view.html"
            # 模板可能在这几个位置：环境变量、同目录 templates、上一级 templates、
            # 或 pip 安装后的 share/moonlighting/templates
            cands = [
                os.environ.get("MOONLIGHTING_TEMPLATES", ""),
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates"),
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "templates"),
                os.path.join(sys.prefix, "share", "moonlighting", "templates"),
            ]
            path = next((os.path.join(c, name) for c in cands
                         if c and os.path.exists(os.path.join(c, name))), "")
            try:
                with open(path, encoding="utf-8") as f:
                    html = f.read()
            except OSError:
                self.send_error(404, "template not found")
                return
            host = self.headers.get("Host") or f"127.0.0.1:{self.server.server_port}"
            html = html.replace("127.0.0.1:8765", host)
            try:
                html = html.replace("__TOKEN__", _auth.token())
            except Exception:  # noqa: BLE001
                pass
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/snapshot"):
            f = STATE["frame"]
            if not f:
                self.send_error(503, "no frame yet")
                return
            cur = STATE.get("cursor")
            if cur:                                  # 光标叠在出帧时画，避免每帧都重编码
                try:
                    im = Image.open(io.BytesIO(f))
                    now = time.time()
                    ca = (now - STATE["click_at"]) if STATE.get("click_at") else None
                    draw_cursor(im, cur, now - STATE.get("cursor_at", now), ca)
                    buf = io.BytesIO()
                    im.save(buf, "JPEG", quality=70)
                    f = buf.getvalue()
                except Exception:  # noqa: BLE001
                    pass
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(f)
        else:
            body = (b"<html><body style='margin:0'><img src='/stream.mjpg' "
                    b"style='width:100%'></body></html>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("HERMES_STREAM_PORT", "8765")))
    ap.add_argument("--desktop", default=DESKTOP)
    ap.add_argument("--cdp", type=int, default=0, help="私有桌面 Chrome 的 CDP 端口，给了才开反向输入")
    ap.add_argument("--off-x", type=int, default=8)
    ap.add_argument("--off-y", type=int, default=95)
    ap.add_argument("--spawn", default="")
    ap.add_argument("--seconds", type=float, default=0, help="0 = 一直跑")
    args = ap.parse_args()

    DESKTOP = args.desktop
    print(f"[desk] 推流目标桌面 = {DESKTOP}", flush=True)

    desktop_handle()
    if args.spawn:
        cmd = args.spawn
        if cmd.startswith("http"):
            # Chrome 路径自动探测（Windows/macOS/Linux），可用 HERMES_CHROME 覆盖
            chrome = os.environ.get("HERMES_CHROME") or next(
                (p for p in (
                    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    "/usr/bin/google-chrome", "/usr/bin/chromium") if os.path.exists(p)), "chrome")
            cmd = (f'"{chrome}" '
                   r'--no-first-run --no-default-browser-check --disable-gpu '
                   f'--window-size=1200,800 --window-position=0,0 "{cmd}"')
        print("[spawn]", cmd, flush=True)
        pi = spawn_on_desktop(cmd)
        print("[spawn] pid", pi.dwProcessId, flush=True)

    stop = threading.Event()
    threading.Thread(target=screencast_worker, args=(stop,), daemon=True).start()
    pump(stop)

    # 端口被占就自动换（直接启动也要有这层，不能只在 svc.py 里做）
    port = args.port
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        import socket as _s
        with _s.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        print(f"[port] {args.port} 已被占用，改用 {port}", flush=True)
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    srv.off_x = args.off_x
    srv.off_y = args.off_y
    if args.cdp:
        STATE["cdp"] = args.cdp          # 截图优先走 CDP，PrintWindow 只做兜底
        input_worker(args.cdp)
        print(f"[input] 反向输入已开启 -> CDP {args.cdp}", flush=True)
    print(f"[http] http://127.0.0.1:{port}/  (/ctl 控制页 /view 只读页 /snapshot /stream.mjpg)", flush=True)

    if args.seconds:
        threading.Timer(args.seconds, srv.shutdown).start()
    srv.serve_forever()
    stop.set()
