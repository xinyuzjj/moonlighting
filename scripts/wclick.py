"""按文字点击扩展页面里的元素（跨 iframe 坐标修正版）。

OKX 钱包的 UI 在不同 iframe 之间来回搬，所以不能只盯一个 target：
1. 在顶层 page 里量出每个 iframe 的位置
2. 逐个 iframe target 找文字，拿到 iframe 内坐标
3. 页面坐标 = iframe 位置 + 内部坐标
4. 输入事件统一发给顶层 page（实测只有这条路能穿过 OOPIF）

用法: python wclick.py <port> "<文字>"
"""
import asyncio
import json
import sys
import urllib.request

import websockets

PORT = int(sys.argv[1])
TEXT = sys.argv[2]

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

IFRAME_JS = """
(() => [...document.querySelectorAll('iframe')].map(f => {
  const r = f.getBoundingClientRect();
  return {src: f.src || '', x: r.left, y: r.top, w: r.width, h: r.height};
}))()
"""


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
    targets = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=5).read())
    page = next((t for t in targets
                 if t.get("type") == "page" and "chrome-extension" in t.get("url", "")), None)
    if not page:
        print("没有扩展页面")
        return

    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=20000000) as pw:
        p = CDP(pw)
        await p.call("Runtime.enable")
        frames = await p.js(IFRAME_JS) or []
        print("[frames]", [(f["src"].split("/")[-1][:28], round(f["x"]), round(f["y"])) for f in frames])

    found = []
    for t in targets:
        if not t.get("webSocketDebuggerUrl"):
            continue
        if t.get("type") not in ("page", "iframe") or t["id"] == page["id"]:
            continue
        try:
            async with websockets.connect(t["webSocketDebuggerUrl"], max_size=20000000) as w:
                c = CDP(w)
                hits = await c.js(FIND_JS % json.dumps(TEXT))
        except Exception:  # noqa: BLE001
            continue
        if not hits:
            continue
        name = t["url"].split("/")[-1].split("?")[0]
        from urllib.parse import urlsplit as _us
        _host = (_us(t["url"]).hostname or "").lower()
        off = next((f for f in frames if (_host and _host in f["src"])
                    or (name and name in f["src"])), None)
        ox = off["x"] if off else 0
        oy = off["y"] if off else 0
        for h in hits:
            found.append({"tag": h["tag"], "x": h["x"] + ox, "y": h["y"] + oy,
                          "w": h["w"], "h": h["h"], "from": name})

    if not found:
        print(f"[{TEXT}] 任何 iframe 里都没找到")
        return
    found.sort(key=lambda h: h["w"] * h["h"])
    hit = found[0]
    print(f"[click] {hit['from']} {hit['tag']} 页面坐标({hit['x']:.0f},{hit['y']:.0f}) {hit['w']}x{hit['h']}")

    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=20000000) as pw:
        p = CDP(pw)
        for t in ("mouseMoved", "mousePressed", "mouseReleased"):
            await p.call("Input.dispatchMouseEvent", type=t, x=hit["x"], y=hit["y"],
                         button="none" if t == "mouseMoved" else "left",
                         clickCount=0 if t == "mouseMoved" else 1)
            await asyncio.sleep(0.08)
    print("[done] 已派发")


if __name__ == "__main__":
    asyncio.run(main())
