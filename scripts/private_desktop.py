"""在自己的 Win32 桌面上开窗口、消息级读写、并截帧 —— 不碰用户桌面。

已验证：add→WM_SETTEXT 写 34 字符 → WM_GETTEXT 读回 34 字符完全一致；
用户桌面 list_windows 看不到这些窗口。

用法:
  python private_desktop.py                    # 默认 notepad 演示
  python private_desktop.py --exe "C:/.../foo.exe" --wait 3
"""
import argparse
import ctypes
import ctypes.wintypes as w
import threading
import time

import os
from PIL import Image

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

GENERIC_ALL = 0x10000000
PW_RENDERFULLCONTENT = 0x00000002
DESKTOP = os.environ.get("HERMES_DESK", "HermesDesk")
WM_SETTEXT = 0x000C
WM_GETTEXT = 0x000D

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
user32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]


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


def desktop():
    """同名桌面对象会复用（上轮的窗口还在），所以先 Open 再 Create。"""
    h = user32.OpenDesktopW(DESKTOP, 0, False, GENERIC_ALL)
    if not h:
        h = user32.CreateDesktopW(DESKTOP, None, None, 0, GENERIC_ALL, None)
    if not h:
        raise OSError(ctypes.get_last_error(), "open/create desktop failed")
    return h


def spawn(cmdline):
    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    si.lpDesktop = DESKTOP
    pi = PROCESS_INFORMATION()
    ok = kernel32.CreateProcessW(None, ctypes.c_wchar_p(cmdline), None, None, False,
                                 0, None, None, ctypes.byref(si), ctypes.byref(pi))
    if not ok:
        raise OSError(ctypes.get_last_error(), "CreateProcessW failed")
    return pi


def enum_windows(hdesk, in_worker_thread=False):
    """SetThreadDesktop 只能在一个没建过窗口的线程里调一次。"""
    out = []
    proc = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

    def cb(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        b = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, b, n + 1)
        r = w.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        pid = w.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        out.append({"hwnd": hwnd, "title": b.value, "pid": pid.value,
                    "vis": bool(user32.IsWindowVisible(hwnd)),
                    "w": r.right - r.left, "h": r.bottom - r.top})
        return True

    if in_worker_thread:
        user32.SetThreadDesktop(hdesk)
    user32.EnumDesktopWindows(hdesk, proc(cb), 0)
    return out


def useful(wins, min_w=50, min_h=50):
    """系统窗口（Tooltip / CiceroUIWndFrame / Default IME）也会被枚举到，必须过滤。"""
    skip = {"Tooltip", "CiceroUIWndFrame", "Program Manager", "Default IME",
            "MSCTFIME UI", "TF_FloatingLangBar_WndTitle"}
    return [x for x in wins if x["title"] and x["title"] not in skip
            and x["w"] > min_w and x["h"] > min_h]


def grab(hwnd, width, height):
    hdc = user32.GetWindowDC(hwnd)
    if not hdc:
        return None
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, width, height)
    gdi32.SelectObject(mdc, bmp)
    user32.PrintWindow(hwnd, mdc, PW_RENDERFULLCONTENT)
    buf = ctypes.create_string_buffer(width * height * 4)
    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(bi)
    bi.biWidth = width
    bi.biHeight = -height  # top-down
    bi.biPlanes = 1
    bi.biBitCount = 32
    gdi32.GetDIBits(mdc, bmp, 0, height, buf, ctypes.byref(bi), 0)
    img = Image.frombuffer("RGB", (width, height), buf, "raw", "BGRX", 0, 1)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mdc)
    user32.ReleaseDC(hwnd, hdc)
    return img


def set_text(edit_hwnd, text):
    """消息级写 —— 零像素、零焦点，窗口最小化也能写。"""
    fn = ctypes.WINFUNCTYPE(w.LPARAM, w.HWND, w.UINT, w.WPARAM, w.LPCWSTR)(("SendMessageW", user32))
    return fn(edit_hwnd, WM_SETTEXT, 0, text)


def get_text(edit_hwnd, cap=1024):
    """读回验证：不走截图。"""
    buf = ctypes.create_unicode_buffer(cap)
    fn = ctypes.WINFUNCTYPE(w.LPARAM, w.HWND, w.UINT, w.WPARAM, ctypes.c_void_p)(("SendMessageW", user32))
    n = fn(edit_hwnd, WM_GETTEXT, cap, ctypes.cast(buf, ctypes.c_void_p))
    return n, buf.value


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default=r"C:\Windows\System32\notepad.exe")
    ap.add_argument("--wait", type=float, default=2.5)
    ap.add_argument("--text", default="Hermes 私有桌面\r\n消息级输入，没有截图、没有抢焦点。")
    ap.add_argument("--shot", default="private_desktop.png")
    args = ap.parse_args()

    hdesk = desktop()
    pi = spawn(args.exe)
    print(f"[1] 桌面={DESKTOP} pid={pi.dwProcessId}")
    time.sleep(args.wait)

    box = {}

    def worker():
        try:
            wins = enum_windows(hdesk, in_worker_thread=True)
            box["wins"] = wins
            cand = useful(wins)
            if not cand:
                box["err"] = "没有可用窗口"
                return
            tgt = max(cand, key=lambda x: x["w"] * x["h"])
            box["tgt"] = tgt
            edit = user32.FindWindowExW(tgt["hwnd"], None, "Edit", None)
            if edit:
                set_text(edit, args.text)
                box["readback"] = get_text(edit)
        except Exception as e:  # noqa: BLE001
            box["err"] = repr(e)

    t = threading.Thread(target=worker)
    t.start()
    t.join(15)

    if "err" in box:
        print("[!] ", box["err"])
        return
    for x in box["wins"]:
        print(f"    hwnd={x['hwnd']} pid={x['pid']} {x['w']}x{x['h']} {x['title']!r}")
    tgt = box["tgt"]
    print(f"[2] 目标 {tgt['title']!r} {tgt['w']}x{tgt['h']}")
    if "readback" in box:
        n, txt = box["readback"]
        print(f"[3] WM_GETTEXT 读回 {n} 字符: {txt!r}")
    img = grab(tgt["hwnd"], tgt["w"], tgt["h"])
    if img:
        img.save(args.shot)
        print(f"[4] PrintWindow -> {args.shot}")


if __name__ == "__main__":
    main()
