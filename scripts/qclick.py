"""跨 iframe 按文字点击（含同进程的跨域 iframe，如 QQ 登录框）。

wclick.py 只能处理「独立 target」的 iframe；QQ / 很多老站点把登录框放在同进程的
跨域 iframe 里，/json 里看不到，必须走 execution context。

用法: python qclick.py <port> "<文字>" [--page 关键字]
"""
import argparse
import asyncio
import json
import urllib.request
from urllib.parse import urlsplit

import websockets

FIND = """(() => {
  const t = %s;
  const vis = (e) => { const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return r.width > 4 && r.height > 4 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const out = [];
  const rec = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) rec(el.shadowRoot);
      const txt = (el.textContent || '').trim();
      if (!txt || !txt.includes(t)) continue;
      if (!vis(el)) continue;
      const r = el.getBoundingClientRect();
      out.push({x: r.left + r.width / 2, y: r.top + r.height / 2,
                w: Math.round(r.width), h: Math.round(r.height)});
    }
  };
  rec(document);
  out.sort((a, b) => a.w * a.h - b.w * b.h);
  return out.slice(0, 6);
})()"""

IFRAMES = """(() => [...document.querySelectorAll('iframe')].map(f => {
  const r = f.getBoundingClientRect();
  return {src: f.src || '', x: r.left, y: r.top};
}))()"""


class C:
    def __init__(self, ws):
        self.ws = ws
        self.n = 0
        self.events = []

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
            self.events.append(m)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("port", type=int)
    ap.add_argument("text")
    ap.add_argument("--page", default="")
    a = ap.parse_args()

    targets = json.loads(urllib.request.urlopen(
        f"http://127.0.0.1:{a.port}/json", timeout=5).read())
    pages = [t for t in targets if t.get("type") == "page"]
    page = next((t for t in pages if a.page and a.page in (t.get("url") or "")), None) or pages[0]
    print("[page]", page.get("url", "")[:90])

    ws = await websockets.connect(page["webSocketDebuggerUrl"], max_size=20000000)
    c = C(ws)
    await c.call("Runtime.enable")
    await c.call("Page.enable")
    await asyncio.sleep(0.6)          # 收 executionContextCreated 事件

    frames = await c.call("Runtime.evaluate", expression=IFRAMES, returnByValue=True)
    frame_rects = frames.get("result", {}).get("value") or []

    ctxs = []
    for ev in c.events:
        if ev.get("method") == "Runtime.executionContextCreated":
            ctx = ev["params"]["context"]
            ctxs.append({"id": ctx.get("id"), "origin": ctx.get("origin") or "",
                         "aux": ctx.get("auxData") or {}})
    # 默认上下文也算（Some 页面不使用 auxData）
    seen = {x["id"] for x in ctxs}
    print(f"[ctx] 共 {len(ctxs)} 个执行上下文")

    hits = []
    for ctx in ctxs:
        try:
            r = await c.call("Runtime.evaluate", expression=FIND % json.dumps(a.text),
                             contextId=ctx["id"], returnByValue=True)
            found = r.get("result", {}).get("value") or []
        except Exception:  # noqa: BLE001
            continue
        if not found:
            continue
        origin = ctx["origin"]
        host = (urlsplit(origin).hostname or "").lower() if "://" in origin else ""
        off = next((f for f in frame_rects if host and host in f["src"]), None)
        ox = off["x"] if off else 0.0
        oy = off["y"] if off else 0.0
        for h in found:
            hits.append({"x": h["x"] + ox, "y": h["y"] + oy, "w": h["w"], "h": h["h"],
                         "via": origin or "main"})
        break

    if not hits:
        print(f"[{a.text}] 没找到")
        await ws.close()
        return

    hits.sort(key=lambda h: h["w"] * h["h"])
    h = hits[0]
    print(f"[click] 页面坐标({h['x']:.0f},{h['y']:.0f}) {h['w']}x{h['h']} via {h['via']}")
    for t in ("mouseMoved", "mousePressed", "mouseReleased"):
        await c.call("Input.dispatchMouseEvent", type=t, x=h["x"], y=h["y"],
                     button="none" if t == "mouseMoved" else "left",
                     clickCount=0 if t == "mouseMoved" else 1)
        await asyncio.sleep(0.1)
    print("[done]")
    await ws.close()


if __name__ == "__main__":
    asyncio.run(main())
