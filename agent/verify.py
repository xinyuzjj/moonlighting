"""断言 DSL：完成判定只认这里，不认模型的自然语言声明。

四种断言：
  {"type":"url",  "op":"~=", "value":"^https://github\\.com/.*issues"}
  {"type":"text", "value":"Issues", "state":"visible"|"absent"}
  {"type":"eval", "js":"document.title", "op":"contains", "expect":"Issues"}
  {"type":"count","value":".Box-row", "op":">=", "expect":1}
"""
import json
import re

import obs

OPS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "~=": lambda a, b: bool(re.search(b, str(a))),
    "contains": lambda a, b: b in str(a),
    ">=": lambda a, b: float(a) >= float(b),
    "<=": lambda a, b: float(a) <= float(b),
}


def _eval(js):
    out = obs.call("/eval", js=f"() => ({js})")
    # daemon 返回 "### Result\n<值>\n### Ran Playwright code..."：只取值那一行
    m = re.search(r"### Result\n(.*?)(?:\n###|\Z)", out, re.S)
    v = (m.group(1) if m else out).strip()
    if v.startswith('"') and v.endswith('"'):
        try:
            v = json.loads(v)
        except ValueError:
            v = v[1:-1]
    return v


def check(a):
    """返回 (ok, 实际值)。异常一律算失败，绝不当成通过。"""
    try:
        t = a["type"]
        if t == "url":
            actual = obs.page()["url"]
            return OPS[a.get("op", "~=")](actual, a["value"]), actual
        if t == "text":
            tree = obs.snapshot(limit=200000)["tree"]
            found = a["value"] in tree
            want = a.get("state", "visible") == "visible"
            return found == want, f"{a['value']} {'在' if found else '不在'}页面上"
        if t == "eval":
            actual = _eval(a["js"])
            return OPS[a.get("op", "contains")](actual, a["expect"]), actual
        if t == "count":
            n = _eval(f"document.querySelectorAll({json.dumps(a['value'])}).length")
            return OPS[a.get("op", ">=")](n, a["expect"]), n
        return False, f"未知断言类型 {t}"
    except Exception as e:  # noqa: BLE001
        return False, f"断言异常: {e}"


def all_ok(asserts):
    detail = []
    for a in asserts or []:
        ok, actual = check(a)
        detail.append({"assert": a, "ok": ok, "actual": str(actual)[:200]})
        if not ok:
            return False, detail
    return True, detail


if __name__ == "__main__":
    print(check({"type": "url", "op": "~=", "value": "github"}))
    print(check({"type": "text", "value": "这个文本肯定不存在zzz", "state": "absent"}))
    print(check({"type": "count", "value": "body", "op": ">=", "expect": 1}))
