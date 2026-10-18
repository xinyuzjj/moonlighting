"""执行层：原子动作白名单 + ref 失效自动重定位。

白名单只有 7 个（P1 够跑通搜索/表单/翻页）：
goto click type key wait read assert
browser_run_code_unsafe / file_upload 之类永远进不来。
"""
import time

import obs

ALLOWED = {"goto", "click", "type", "key", "wait", "read", "assert",
           "scroll", "select", "ask_user", "done"}


def _find_ref(name):
    """按可访问名在最新快照里找回 ref（页面刷新后旧 ref 全废）。"""
    import re
    tree = obs.snapshot(limit=200000)["tree"]
    for line in tree.splitlines():
        if f'"{name}"' in line and "[ref=" in line:
            m = re.search(r"\[ref=([a-z0-9]+)\]", line)
            if m:
                return m.group(1)
    return ""


def act(action, **kw):
    """执行一个动作，返回 {ok, ms, detail}。失败不代表没生效，调用方要自己断言。"""
    if action not in ALLOWED:
        return {"ok": False, "detail": f"动作不在白名单: {action}", "ms": 0}
    t0 = time.time()
    try:
        if action == "goto":
            out = obs.call("/goto", url=kw["url"])
        elif action == "click":
            out = obs.call("/click", target=kw.get("ref", ""), element=kw.get("name", ""))
            if _bad(out) and kw.get("name") and "元素不存在" not in out:
                # 只有 ref 过期才重定位；元素确实不存在时重试是白费一次快照
                ref = _find_ref(kw["name"])
                if ref:
                    out = obs.call("/click", target=ref, element=kw["name"])
                    kw["ref"] = ref
        elif action == "type":
            ref = kw.get("ref") or (_find_ref(kw["name"]) if kw.get("name") else "")
            out = obs.call("/type", target=ref, text=kw["text"], element=kw.get("name", ""))
            if kw.get("submit"):
                obs.call("/key", key="Enter")
        elif action == "key":
            out = obs.call("/key", key=kw.get("key", "Enter"))
        elif action == "wait":
            out = _wait(kw.get("until", {}), kw.get("timeout", 15))
        elif action == "read":
            out = obs.call("/eval", js=kw.get("js", "() => document.title"))
        elif action == "scroll":
            px = int(kw.get("px", 800))
            sign = 1 if kw.get("direction", "down") == "down" else -1
            times = int(kw.get("times", 1))
            obs.call("/eval", js=f"() => {{window.scrollBy(0,{sign * px * times});return 1}}")
            out = "已滚动"
        elif action == "select":
            ref = kw.get("ref") or (_find_ref(kw["name"]) if kw.get("name") else "")
            out = obs.call("/eval", js=(
                "() => {const s=document.querySelector('[data-ref=\"%s\"],#%s');"
                "if(!s)return 0;s.value=%s;s.dispatchEvent(new Event('change',{bubbles:true}));return 1}"
                % (ref, ref, __import__("json").dumps(kw.get("option", "")))))
        elif action == "ask_user":
            # 人工闸门：commit 类动作（支付/发送/删除）必须走这里，没有确认就阻塞
            return {"ok": False, "ms": 0,
                    "detail": f"需要人工确认：{kw.get('question', '')}（确认后重跑并加 --yes）"}
        elif action == "done":
            return {"ok": True, "ms": 0, "detail": kw.get("result", "")}
        elif action == "assert":
            import verify
            ok, detail = verify.all_ok(kw.get("checks", []))
            return {"ok": ok, "detail": detail, "ms": int((time.time() - t0) * 1000)}
        return {"ok": not _bad(out), "detail": out[-300:], "ms": int((time.time() - t0) * 1000)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": f"{type(e).__name__}: {e}",
                "ms": int((time.time() - t0) * 1000)}


def _bad(out):
    return (not out) or ("### Error" in out) or ("工具报错" in out) or ("Error:" in out)


def _wait(until, timeout=15):
    """轮询而不是 sleep：0.4s 一次，命中就返回。"""
    import verify
    deadline = time.time() + timeout
    while time.time() < deadline:
        if until and verify.all_ok([until])[0]:
            return "条件已满足"
        if not until:
            return "无 until 条件，直接返回"
        time.sleep(0.4)
    return f"等待超时 {timeout}s"
