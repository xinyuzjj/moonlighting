"""主循环：任务 → 步骤 → 动作 + 断言 → events.jsonl。

P1 阶段没有 planner：任务是显式步骤列表（人工当 planner，这是基线对照线）。
每步都必须带 check；没有 check 的步骤直接拒跑——"点了没反应也继续走"就是从这里断掉的。
"""
import argparse
import json
import os
import sys
import time

import act
import mem
import obs
import verify

HERE = os.path.dirname(os.path.abspath(__file__))
RETRIES = 3


def _milestone():
    """回滚点：URL + 所有输入框的当前值。"""
    try:
        raw = verify._eval(
            "JSON.stringify([...document.querySelectorAll('input,textarea')]"
            ".filter(e=>e.value).map(e=>[e.id||e.name,e.value]))")
        return {"url": obs.page()["url"], "values": json.loads(raw)}
    except Exception:  # noqa: BLE001
        return {"url": obs.page()["url"], "values": []}


def _restore(m):
    """回跳 + 重填。表单填错一错到底的解药。"""
    act.act("goto", url=m["url"])
    for sel, val in m["values"]:
        js = ("() => {const e=document.getElementById(%s)||document.querySelector('[name=\"%s\"]');"
              "if(e){e.value=%s;e.dispatchEvent(new Event('input',{bubbles:true}))}return 1}"
              % (json.dumps(sel), sel, json.dumps(val)))
        try:
            obs.call("/eval", js=js)
        except Exception:  # noqa: BLE001
            pass


def run(task, runs_dir=None, verbose=True, confirm=False):
    if not task.get("steps"):
        raise SystemExit("P1 只跑显式步骤任务（没有 planner）；steps 不能为空")
    allow = set(task.get("actions_allow") or act.ALLOWED)
    forbid = set(task.get("forbid") or [])
    for i, s in enumerate(task["steps"]):
        a = s["do"]["action"]
        if "check" not in s and not s.get("expect_fail") and a not in ("ask_user", "done"):
            raise SystemExit(f"第 {i+1} 步没有 check：不允许无校验的动作")
        if a in forbid:
            raise SystemExit(f"第 {i+1} 步用了任务禁止的动作: {a}")
        if a not in allow:
            raise SystemExit(f"第 {i+1} 步的动作 {a} 不在 actions_allow 里")

    ts = time.strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join(runs_dir or os.path.join(os.path.dirname(HERE), "runs"), ts)
    os.makedirs(out_dir, exist_ok=True)
    events = open(os.path.join(out_dir, "events.jsonl"), "w", encoding="utf-8")

    def log(ev):
        ev["t"] = round(time.time(), 3)
        events.write(json.dumps(ev, ensure_ascii=False) + "\n")
        events.flush()
        if verbose:
            print(f"  {ev['step']:>2} {ev['action']:<6} {'OK ' if ev['ok'] else 'FAIL'} "
                  f"{ev['ms']:>5}ms  {str(ev.get('detail'))[:90]}")

    # 站点记忆：上次成功的序列还在不在
    start = task.get("start") or obs.page()["url"]
    h = mem.hint(start)
    if h:
        log({"step": 0, "action": "memory", "ok": True, "ms": 0,
             "detail": f"命中 {h['host']}{h['path']}（{len(h['steps'])} 步，{h['ts']}）"})


    ok_all, fails, steps, done, last_shot = True, 0, 0, [], 0
    max_steps = task.get("max_steps", len(task["steps"]) * RETRIES)
    milestone = _milestone()

    for i, s in enumerate(task["steps"], 1):
        d = s["do"]
        passed, detail = False, []
        for attempt in range(1, RETRIES + 1):
            if steps >= max_steps:
                break
            if d["action"] == "ask_user":
                if not confirm:
                    log({"step": i, "action": "ask_user", "ok": False, "ms": 0,
                         "detail": f"阻塞：{d.get('question', '')}（确认后加 --yes 重跑）"})
                    ok_all = False
                    break
                log({"step": i, "action": "ask_user", "ok": True, "ms": 0,
                     "detail": f"已人工确认：{d.get('question', '')}"})
                passed = True
                break
            r = act.act(d["action"], **{k: v for k, v in d.items() if k != "action"})
            steps += 1
            log({"step": i, "action": d["action"], "args": d, "try": attempt,
                 "ok": r["ok"], "ms": r["ms"], "detail": r["detail"]})
            passed, detail = verify.all_ok(s.get("check", []))
            if not passed and s.get("check"):
                # 断言失败先重取快照再判一次，避免动画/懒加载中途误判
                time.sleep(0.6)
                passed, detail = verify.all_ok(s.get("check", []))
            if passed != s.get("expect_fail", False):
                break
            # 同一元素连试两次还不中 → 截图，让人或多模态模型直接看
            if attempt >= 2 and steps - last_shot >= 2:
                try:
                    info = obs.shot(os.path.join(out_dir, "shots", f"step{i}-try{attempt}.jpg"))
                    last_shot = steps
                    log({"step": i, "action": "shot", "ok": True, "ms": 0, "detail": info})
                except Exception as e:  # noqa: BLE001
                    log({"step": i, "action": "shot", "ok": False, "ms": 0, "detail": str(e)})

        if steps >= max_steps:
            log({"step": i, "action": "budget", "ok": False, "ms": 0,
                 "detail": f"超过 max_steps={max_steps}"})
            ok_all = False
            break

        log({"step": i, "action": "assert", "ok": passed != s.get("expect_fail", False),
             "ms": 0, "detail": detail})
        if passed == s.get("expect_fail", False):
            fails += 1
            ok_all = False
            log({"step": i, "action": "recover", "ok": True, "ms": 0,
                 "detail": f"回滚到 {milestone['url']}（{len(milestone['values'])} 个字段）"})
            _restore(milestone)
            if not task.get("continue_on_fail"):
                break
        else:
            done.append(d)
            milestone = _milestone()

    # done_when 才是完成判定；步骤全过但目标没达成，任务依然算失败
    if ok_all and task.get("done_when"):
        ok_all, dd = verify.all_ok(task["done_when"])
        log({"step": 0, "action": "done_when", "ok": ok_all, "ms": 0, "detail": dd})

    result = {"id": task.get("id"), "ok": ok_all, "steps": steps, "fails": fails,
              "url": obs.page()["url"], "run_dir": out_dir}
    if ok_all:
        mem.record(task.get("start") or result["url"], done, task.get("id", ""))
    with open(os.path.join(out_dir, "result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    events.close()
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task", help="任务 JSON 路径")
    ap.add_argument("--runs", default=None)

    ap.add_argument("--yes", action="store_true", help="确认 ask_user 闸门（commit 类动作）")
    a = ap.parse_args()
    task = json.load(open(a.task, encoding="utf-8"))
    if task.get("start"):
        act.act("goto", url=task["start"])
    r = run(task, a.runs, confirm=a.yes)
    print(f"\n{'通过' if r['ok'] else '失败'} · {r['steps']} 步 · {r['fails']} 次断言失败 · {r['run_dir']}")
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
