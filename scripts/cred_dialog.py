"""凭据弹窗（美化版）—— 置顶小窗，按目标页面字段现读现生成。

1. 启动时 GET /describe 拿到目标页面有几个框、叫什么
2. 按页面实际字段生成掩码输入框（标签用页面自己的文案）
3. 填写 -> 校验一致 -> POST /save -> 拿到 handle -> 自动关闭
4. handle 打到 stdout（agent 读它，不读密码）

用法: python cred_dialog.py --cdp 18763
"""
import argparse
import json
import threading
import tkinter as tk
import urllib.request
import os
from tkinter import font as tkfont

try:
    import ports as _ports
    BROKER = "http://127.0.0.1:%d" % _ports.port_of("broker")
except Exception:  # noqa: BLE001
    BROKER = os.environ.get("HERMES_BROKER", "http://127.0.0.1:8899")

BG = "#f4f4f5"
CARD = "#ffffff"
BORDER = "#e4e4e7"
TEXT = "#18181b"
MUTED = "#71717a"
ACCENT = "#16a34a"
DARK = "#111113"
DANGER = "#dc2626"


def _auth_headers():
    try:
        import auth as _a
        return _a.headers_for_client()
    except Exception:  # noqa: BLE001
        return {}


def post_json(path, payload, timeout=8):
    h = {"Content-Type": "application/json"}
    h.update(_auth_headers())
    req = urllib.request.Request(BROKER + path, data=json.dumps(payload).encode(),
                                 headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def get_json(path, timeout=10):
    req = urllib.request.Request(BROKER + path, headers=_auth_headers())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


class Field:
    """带边框的输入框：外层 Frame 画 1px 边，内层扁平 Entry。"""

    def __init__(self, parent, on_enter, mask=True):
        self.wrap = tk.Frame(parent, bg=BORDER, padx=1, pady=1)
        self.inner = tk.Frame(self.wrap, bg=CARD)
        self.inner.pack(fill="both", expand=True)
        self.entry = tk.Entry(self.inner, bd=0, relief="flat", bg=CARD, fg=TEXT,
                              insertbackground=TEXT, show="●" if mask else "",
                              font=("Consolas", 12), highlightthickness=0)
        self.entry.pack(fill="x", padx=12, pady=9)
        self.entry.bind("<Return>", lambda _e: on_enter())
        self.entry.bind("<FocusIn>", lambda _e: self.wrap.config(bg="#a1a1aa"))
        self.entry.bind("<FocusOut>", lambda _e: self.wrap.config(bg=BORDER))

    def get(self):
        return self.entry.get()

    def clear(self):
        self.entry.delete(0, "end")


class Popup:
    WIDTH = 404

    def __init__(self, cdp_port: int, fields: str = "", origin: str = ""):
        self.handle = None
        self.win = tk.Tk()
        self.win.title("凭据代理")
        self.win.configure(bg=BORDER)
        self.win.attributes("-topmost", True)
        self.win.resizable(False, False)
        self.win.overrideredirect(True)          # 无系统标题栏：不要羽毛图标和标题文字
        self.root = tk.Frame(self.win, bg=BG)    # 1px 描边靠外层 bg 露出来
        self.root.pack(fill="both", expand=True, padx=1, pady=1)
        self._dx = self._dy = 0

        try:
            if fields:      # 手动指定（跨域 iframe 里的表单 /describe 看不到）
                spec = []
                for part in fields.split(","):
                    name, _, kind = part.partition(":")
                    spec.append({"type": kind or "password", "label": name.strip()})
                self.spec = {"ok": True, "origin": origin or "manual", "title": origin or "手动填写",
                             "fields": spec}
            else:
                self.spec = get_json(f"/describe?cdp={cdp_port}")
        except Exception as e:  # noqa: BLE001
            self.spec = {"ok": False, "error": f"读不到目标页面：{e}"}

        self.f_title = tkfont.Font(family="Microsoft YaHei UI", size=11, weight="bold")
        self.f_body = tkfont.Font(family="Microsoft YaHei UI", size=9)
        self.f_small = tkfont.Font(family="Microsoft YaHei UI", size=8)
        self.f_btn = tkfont.Font(family="Microsoft YaHei UI", size=10, weight="bold")

        self._build()
        self.win.bind("<Escape>", lambda _e: self.win.destroy())
        self.win.after(10, self._center)
        self.win.after(150, lambda: self.win.focus_force())

    def _flat_button(self, parent, text, command, primary=True):
        bg = DARK if primary else BG
        fg = "#ffffff" if primary else MUTED
        hover = "#27272a" if primary else "#e4e4e7"
        b = tk.Label(parent, text=text, bg=bg, fg=fg,
                     font=self.f_btn if primary else self.f_body,
                     padx=14, pady=10 if primary else 8, cursor="hand2")
        b.bind("<Button-1>", lambda _e: command())
        b.bind("<Enter>", lambda _e: b.config(bg=hover))
        b.bind("<Leave>", lambda _e: b.config(bg=bg))
        return b

    def _build(self):
        pad = 20
        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", padx=pad, pady=(18, 0))
        dot = tk.Canvas(head, width=8, height=8, bg=BG, highlightthickness=0)
        dot.create_oval(1, 1, 7, 7, fill=ACCENT, outline="")
        dot.pack(side="left", pady=(4, 0), padx=(0, 7))
        title = tk.Label(head, text="凭据代理", bg=BG, fg=TEXT, font=self.f_title)
        title.pack(side="left")

        # 无标题栏也能拖动：在顶部区域按住拖
        for w in (self.root, head, dot, title):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)

        if not self.spec.get("ok"):
            tk.Label(self.root, text=self.spec.get("error", "未知错误"), bg=BG, fg=DANGER,
                     font=self.f_body, wraplength=self.WIDTH - 2 * pad,
                     justify="left").pack(anchor="w", padx=pad, pady=(10, 4))
            self._flat_button(self.root, "关闭", self.win.destroy).pack(
                fill="x", padx=pad, pady=(8, 18))
            return

        self.origin = self.spec.get("origin", "")
        target = self.spec.get("title") or self.origin
        tk.Label(self.root, text=target, bg=BG, fg=MUTED, font=self.f_small,
                 anchor="w", wraplength=self.WIDTH - 2 * pad).pack(
            fill="x", padx=pad, pady=(3, 0))

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="x", padx=pad, pady=(14, 0))

        fields = self.spec.get("fields") or []
        self.fields = []
        self.types = [f.get("type") or "text" for f in fields]
        for i, f in enumerate(fields):
            label = (f.get("label") or "").strip()
            if not label or len(label) > 30:
                if f.get("type") == "password":
                    label = "密码" if len(fields) == 1 else ("确认密码" if i == 1 else f"密码 {i + 1}")
                elif f.get("type") in ("text", "tel", "email"):
                    label = "账号"
                else:
                    label = f"输入框 {i + 1}"
            tk.Label(body, text=label, bg=BG, fg="#3f3f46", font=self.f_body,
                     anchor="w").pack(fill="x", pady=(0 if i == 0 else 12, 5))
            fl = Field(body, self.submit, mask=(f.get("type") == "password"))
            fl.wrap.pack(fill="x")
            self.fields.append(fl)

        self.msg = tk.Label(self.root, text="", bg=BG, fg=MUTED, font=self.f_small,
                            anchor="w", justify="left", wraplength=self.WIDTH - 2 * pad)
        self.msg.pack(fill="x", padx=pad, pady=(10, 0))

        tk.Label(self.root, text="DPAPI 加密 · 仅本机可解 · 值不离开这台电脑",
                 bg=BG, fg="#a1a1aa", font=self.f_small, anchor="w").pack(
            fill="x", padx=pad, pady=(6, 0))

        self.primary = self._flat_button(self.root, "加密保存", self.submit, primary=True)
        self.primary.pack(fill="x", padx=pad, pady=(12, 0))
        self._flat_button(self.root, "取消", self.win.destroy, primary=False).pack(
            fill="x", padx=pad, pady=(4, 16))

        if self.fields:
            self.fields[0].entry.focus_set()

    def _drag_start(self, e):
        self._dx = e.x_root - self.win.winfo_x()
        self._dy = e.y_root - self.win.winfo_y()

    def _drag_move(self, e):
        self.win.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _center(self):
        self.win.update_idletasks()
        h = self.win.winfo_reqheight()
        self.win.geometry(f"{self.WIDTH}x{h}")   # 固定宽度，内部 fill="x" 才会撑开
        self.win.update_idletasks()
        h = self.win.winfo_height()
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        self.win.geometry(f"{self.WIDTH}x{h}+{(sw - self.WIDTH) // 2}+{max(0, (sh - h) // 2 - 80)}")

    def _say(self, text, color=MUTED):
        self.msg.config(text=text, fg=color)

    def _ui(self, fn):
        self.win.after(0, fn)

    def submit(self):
        if not self.fields:
            return
        vals = [f.get().strip() for f in self.fields]   # 前后空格会让密码永远不对
        pwd_vals = [v for v, t in zip(vals, self.types) if t == "password"]
        id_vals = [v for v, t in zip(vals, self.types) if t != "password"]
        if any(len(v) < 4 for v in vals):
            self._say("每栏至少 4 个字符。", DANGER)
            return
        if len(pwd_vals) > 1 and len(set(pwd_vals)) != 1:
            self._say("两次密码不一致，请重新输入。", DANGER)
            return
        secret = pwd_vals[0] if pwd_vals else vals[0]
        identifier = id_vals[0] if id_vals else ""
        for f in self.fields:
            f.clear()
        self.primary.config(bg="#52525b")
        self._say("加密中…", MUTED)

        def work():
            try:
                res = post_json("/save", {"origin": self.origin, "label": "页面凭据",
                                          "secret": secret, "identifier": identifier})
            except Exception as exc:  # noqa: BLE001
                self._ui(lambda: (self._say(f"保存失败：{exc}", DANGER),
                                  self.primary.config(bg=DARK)))
                return
            if res.get("ok"):
                self.handle = res["handle"]
                self._ui(lambda: self._say(f"已加密保存 · {self.handle}", ACCENT))
                self._ui(lambda: self.win.after(650, self.win.destroy))
            else:
                self._ui(lambda: (self._say(res.get("error", "失败"), DANGER),
                                  self.primary.config(bg=DARK)))

        threading.Thread(target=work, daemon=True).start()

    def run(self):
        self.win.mainloop()
        print(f"HANDLE={self.handle}" if self.handle else "CANCELLED", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp", type=int, required=True)
    ap.add_argument("--fields", default="", help="账号:text,密码:password")
    ap.add_argument("--origin", default="", help="凭据绑定的 origin")
    a = ap.parse_args()
    Popup(a.cdp, a.fields, a.origin).run()
