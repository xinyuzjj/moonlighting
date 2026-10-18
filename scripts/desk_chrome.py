r"""在指定的私有 Win32 桌面上启动一个独立 Chrome。

每个桌面 = 一个独立 profile 目录，互不共享 cookie / 登录态 / 历史：
    D:\Hermes浏览器\<桌面名>\

用法:
  python desk_chrome.py --desktop HermesDesk  --url https://www.bilibili.com --port 18763
  python desk_chrome.py --desktop HermesDesk2 --url https://www.youtube.com  --port 18764
"""
import argparse
import ctypes
import ctypes.wintypes as w
import json
import os
import subprocess
import time

try:
    import ports as _ports
    BASE = _ports.profiles()
except Exception:  # noqa: BLE001
    BASE = os.environ.get("HERMES_PROFILES") or ('D:\\Hermes浏览器' if os.path.isdir('D:\\Hermes浏览器') else os.path.join(os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "hermes-browser"), "profiles"))
CHROME = os.environ.get("HERMES_CHROME") or next((p for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe", os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"), "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/usr/bin/google-chrome", "/usr/bin/chromium") if os.path.exists(p)), "chrome")
GENERIC_ALL = 0x10000000

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.OpenDesktopW.restype = w.HANDLE
user32.OpenDesktopW.argtypes = [w.LPCWSTR, w.DWORD, w.BOOL, w.DWORD]
user32.CreateDesktopW.restype = w.HANDLE
user32.CreateDesktopW.argtypes = [w.LPCWSTR, w.LPCWSTR, ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.c_void_p]
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


def desk_handle(name):
    h = user32.OpenDesktopW(name, 0, False, GENERIC_ALL)
    if not h:
        h = user32.CreateDesktopW(name, None, None, 0, GENERIC_ALL, None)
    if not h:
        raise OSError(ctypes.get_last_error(), f"无法打开/创建桌面 {name}")
    return h


def windows(h, in_thread=False):
    out = []
    proc = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

    def cb(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        b = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, b, n + 1)
        if b.value:
            out.append((hwnd, b.value))
        return True

    if in_thread:
        user32.SetThreadDesktop(h)
    user32.EnumDesktopWindows(h, proc(cb), 0)
    return out


def kill_chrome_on(h):
    """只杀这个桌面上 Chrome 窗口的 owner 进程，不碰别的桌面。"""
    targets = [x for x in windows(h, in_thread=True) if "Chrome" in x[1]]
    pids = set()
    for hwnd, _ in targets:
        pid = w.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value:
            pids.add(pid.value)
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    return [t for _, t in targets], sorted(pids)


STILL_ACTIVE = 259


def _tabs_file(profile):
    return os.path.join(profile, "hermes-tabs.json")


def save_tabs(profile, cdp_port):
    """关之前把这些标签页的 URL 记下来（不依赖 Chrome 自己的 session 恢复）。"""
    if not cdp_port:
        return []
    try:
        import urllib.request
        raw = urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json", timeout=4).read()
        pages = [t for t in json.loads(raw)
                 if t.get("type") == "page" and t.get("url", "").startswith(("http", "file"))]
        urls = [t["url"] for t in pages]
        if urls:
            with open(_tabs_file(profile), "w", encoding="utf-8") as fh:
                json.dump(urls, fh, ensure_ascii=False, indent=2)
        return urls
    except Exception:  # noqa: BLE001
        return []


def load_tabs(profile):
    try:
        with open(_tabs_file(profile), encoding="utf-8") as fh:
            return [u for u in json.load(fh) if isinstance(u, str)]
    except (OSError, ValueError):
        return []


def clear_crash_flag(profile):
    """Chrome 未运行时把它自己的崩溃标记改回 Normal，否则启动会弹「要恢复页面吗？」。"""
    p = os.path.join(profile, "Default", "Preferences")
    try:
        with open(p, encoding="utf-8") as fh:
            j = json.load(fh)
    except (OSError, ValueError):
        return False
    pr = j.setdefault("profile", {})
    if pr.get("exit_type") == "Normal" and pr.get("exited_cleanly") is True:
        return False
    pr["exit_type"] = "Normal"
    pr["exited_cleanly"] = True
    try:
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(j, fh, ensure_ascii=False)
        return True
    except OSError:
        return False


def _alive(pid):
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = w.DWORD()
    ok = kernel32.GetExitCodeProcess(h, ctypes.byref(code))
    kernel32.CloseHandle(h)
    return bool(ok) and code.value == STILL_ACTIVE


def close_chrome_on(h, timeout=20):
    """优雅关闭：发 WM_CLOSE 让 Chrome 把 cookie / session 刷盘，超时才强杀。"""
    targets = [x for x in windows(h, in_thread=True) if "Chrome" in x[1]]
    pids = set()
    for hwnd, _ in targets:
        pid = w.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value:
            pids.add(pid.value)
    for hwnd, _ in targets:
        user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE

    waited = 0.0
    while waited < timeout and any(_alive(p) for p in pids):
        time.sleep(0.5)
        waited += 0.5

    leftover = [p for p in pids if _alive(p)]
    for pid in leftover:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    return [t for _, t in targets], sorted(pids), sorted(leftover), round(waited, 1)


def spawn(cmdline, desktop):
    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    si.lpDesktop = desktop
    pi = PROCESS_INFORMATION()
    ok = kernel32.CreateProcessW(None, ctypes.c_wchar_p(cmdline), None, None, False,
                                 0, None, None, ctypes.byref(si), ctypes.byref(pi))
    if not ok:
        raise OSError(ctypes.get_last_error(), "CreateProcessW failed")
    return pi.dwProcessId


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--desktop", required=True)
    ap.add_argument("--url", default="about:blank")
    ap.add_argument("--port", type=int, default=0, help="CDP 调试端口，0 = 不启用")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=860)
    ap.add_argument("--no-kill", action="store_true")
    ap.add_argument("--load-ext", action="append", default=[],
                    help="以解包方式加载的扩展目录（可重复，自动加 --disable-extensions-except）")
    ap.add_argument("--browser", default="",
                    help="浏览器 exe 路径（默认系统 Chrome；品牌版 Chrome 137+ 已废 --load-extension，需换 Chromium）")
    ap.add_argument("--restore", action="store_true",
                    help="恢复上次会话（不强制打开 --url，等价于关掉再手动打开）")
    args = ap.parse_args()

    # CDP 端口被别的程序占着就自动换一个，避免连到别人的 Electron 应用上
    if args.port:
        try:
            import ports as _ports
            if _ports.occupied(args.port) and not _ports.is_ours("cdp", args.port):
                newp = _ports.free_port(args.port + 1)
                print(f"[port] {args.port} 已被占用，改用 {newp}")
                args.port = newp
        except ImportError:
            pass

    profile = os.path.join(BASE, args.desktop)
    os.makedirs(profile, exist_ok=True)
    print(f"[profile] {args.desktop} -> {profile}")

    h = desk_handle(args.desktop)
    if not args.no_kill:
        saved = save_tabs(profile, args.port)
        if saved:
            print(f"[tabs] 已记录 {len(saved)} 个标签: {saved[:3]}")
        titles, pids, killed, waited = close_chrome_on(h)
        if titles:
            print(f"[close] 优雅关闭 {titles} pid={pids} 等待 {waited}s"
                  + (f" 强杀残留={killed}" if killed else " 正常退出"))
            time.sleep(1.0)
        if clear_crash_flag(profile):
            print("[fix] 已把崩溃标记改回 Normal（不会再弹「要恢复页面吗？」）")

    if args.restore:
        tabs = load_tabs(profile)
        tail = " ".join(f'"{u}"' for u in tabs) if tabs else "about:blank"
        label = f"restore {len(tabs)} tab(s)" if tabs else "restore(空)"
    else:
        tail = f'"{args.url}"'
        label = f"url={args.url}"
    exe = args.browser or CHROME
    cmd = (f'"{exe}" --user-data-dir="{profile}" --new-window '
           + (f'--remote-debugging-port={args.port} ' if args.port else "")
           + (f'--load-extension={",".join(args.load_ext)} ' if args.load_ext else "")
           + (f'--disable-extensions-except={",".join(args.load_ext)} ' if args.load_ext else "")
           + ('--disable-features=DisableLoadExtensionCommandLineSwitch ' if args.load_ext else "")
           + f'--no-first-run --no-default-browser-check --disable-gpu '
             f'--window-size={args.width},{args.height} --window-position=0,0 {tail}')
    pid = spawn(cmd, args.desktop)
    print(f"[spawn] {args.desktop} pid={pid} {label}")
    time.sleep(7)
    print("[windows]", [t for _, t in windows(h)])


if __name__ == "__main__":
    main()
