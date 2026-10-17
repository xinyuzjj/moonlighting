"""登录闭环：弹窗取凭据 → 注入 → 提交 → 检查失败 → 自动再弹窗。

用户输错了要能立刻重输，而不是干等着。判定失败靠页面错误文案，
成功则看 URL 是否离开登录页。

用法:
  python login_flow.py --cdp 18763 --url github.com --origin https://github.com \
      --fields "用户名或邮箱:text,密码:password" \
      --id 632,207 --pwd 632,288 --submit 632,344 [--ok-url /login 之外的关键字]
"""
import argparse
import os
import asyncio
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import websockets

ERR_WORDS = [
    "incorrect username or password", "invalid username or password",
    "密码错误", "账号或密码", "用户名或密码", "登录失败", "验证失败",
    "wrong password", "authentication failed",
]


def page_state(port, url_hint):
    targets = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5).read())
    pages = [t for t in targets if t.get("type") == "page"]
    pg = next((t for t in pages if url_hint in (t.get("url") or "")), None) or pages[0]

    async def read():
        async with websockets.connect(pg["webSocketDebuggerUrl"], max_size=20000000) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                      "params": {"expression": "document.body.innerText.slice(0,3000)",
                                                 "returnByValue": True}}))
            while True:
                m = json.loads(await ws.recv())
                if m.get("id") == 1:
                    return m.get("result", {}).get("result", {}).get("value") or ""

    return pg.get("url", ""), asyncio.run(read())


def popup(cdp, origin, fields):
    """弹凭据窗，返回 handle；用户取消返回 None。"""
    p = subprocess.run([sys.executable, "cred_dialog.py", "--cdp", str(cdp),
                        "--origin", origin, "--fields", fields],
                       capture_output=True, text=True, cwd=".")
    out = (p.stdout or "").strip()
    for line in out.splitlines():
        if line.startswith("HANDLE="):
            return line.split("=", 1)[1]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp", type=int, required=True)
    ap.add_argument("--url", required=True, help="页面 URL 关键字")
    ap.add_argument("--origin", required=True)
    ap.add_argument("--fields", required=True)
    ap.add_argument("--id-sel", default="", help="账号框选择器，如 #login_field")
    ap.add_argument("--pwd-sel", default="input[type=password]", help="密码框选择器")
    ap.add_argument("--submit-sel", default="", help="提交按钮选择器，如 input[type=submit]")
    ap.add_argument("--tries", type=int, default=3)
    a = ap.parse_args()


    for attempt in range(1, a.tries + 1):
        print(f"[第 {attempt} 次] 弹窗等待输入…", flush=True)
        handle = popup(a.cdp, a.origin, a.fields)
        if not handle:
            print("[取消] 用户关闭了弹窗", flush=True)
            return 1
        print(f"[handle] {handle}", flush=True)

        q = urllib.parse.urlencode({
            "handle": handle, "cdp": a.cdp, "url": a.url,
            "id_sel": a.id_sel, "pwd_sel": a.pwd_sel, "submit_sel": a.submit_sel})
        try:
            import ports as _ports
            _broker = "http://127.0.0.1:%d" % _ports.port_of("broker")
        except Exception:  # noqa: BLE001
            _broker = os.environ.get("HERMES_BROKER", "http://127.0.0.1:8899")
        try:
            import auth as _a
            _hdrs = _a.headers_for_client()
        except Exception:  # noqa: BLE001
            _hdrs = {}
        res = json.loads(urllib.request.urlopen(
            urllib.request.Request(_broker + f"/inject_sel?{q}", headers=_hdrs),
            timeout=60).read())
        print("[填充]", res, flush=True)
        if not res.get("ok"):
            print("[放弃本次] 填充/读回校验没通过，没有提交", flush=True)
            continue
        # 轮询代替死等：出现错误文案或 URL 离开登录页就立刻继续，最多等 8s
        deadline = time.time() + 8
        bad = None
        url = ""
        while time.time() < deadline:
            time.sleep(0.4)
            url, text = page_state(a.cdp, a.url)
            low = text.lower()
            bad = next((w for w in ERR_WORDS if w in low), None)
            if bad or ("/login" not in url and "/session" not in url):
                break
        if bad:
            print(f"[登录失败] 页面提示「{bad}」→ 重新弹窗", flush=True)
            continue
        print(f"[可能成功] 当前 URL: {url}", flush=True)
        return 0
    print("[放弃] 连续失败，交给人工", flush=True)
    return 2


if __name__ == "__main__":
    sys.exit(main())
