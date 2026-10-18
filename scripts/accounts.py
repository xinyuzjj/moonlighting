"""账号台账：哪套桌面 = 哪个平台 = 哪个号。

一个桌面一套 Chrome profile、一套凭据库，所以「号」的边界就是桌面名。
这里只记元数据，**不存任何密码**——密码留在各自的 vault 里。

用法：
  python accounts.py add --desktop junjunni --platform github --account me@example.com
  python accounts.py list
  python accounts.py remove --desktop junjunni
  python accounts.py --selftest
"""
import argparse
import json
import os
import sys
import time

DEFAULT_PATH = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
    "moonlighting", "accounts.json")


def store():
    return os.environ.get("MOONLIGHTING_ACCOUNTS") or DEFAULT_PATH


def load():
    try:
        with open(store(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(data):
    p = store()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, p)          # 半写完的台账比没有台账更糟
    return p


def add(desktop, platform, account, home="", profiles="", cdp=0,
        actions=None, note=""):
    data = load()
    if desktop in data:
        raise SystemExit(f"桌面 {desktop} 已在台账里；先 remove 再加")
    data[desktop] = {
        "desktop": desktop,
        "platform": platform,
        "account": account,
        "home": home,
        "profiles": profiles,
        "cdp": cdp,
        "actions": actions or ["read"],
        "state": "unknown",          # 登录态要等 check 才知道，不在这里编
        "note": note,
        "added": time.strftime("%Y-%m-%d %H:%M:%S"),
        "last_ok": "",
    }
    save(data)
    return data[desktop]


def remove(desktop):
    data = load()
    if desktop not in data:
        raise SystemExit(f"台账里没有 {desktop}")
    row = data.pop(desktop)
    save(data)
    return row


def rows():
    return sorted(load().values(), key=lambda r: (r.get("platform", ""), r.get("desktop", "")))


def table():
    rs = rows()
    if not rs:
        return "（台账是空的）"
    head = f"{'桌面':<16}{'平台':<12}{'账号':<28}{'权限':<12}{'状态':<10}新增"
    lines = [head, "-" * len(head)]
    for r in rs:
        lines.append(f"{r['desktop']:<16}{r['platform']:<12}{r['account'][:26]:<28}"
                     f"{','.join(r.get('actions', []))[:10]:<12}"
                     f"{r.get('state', '?'):<10}{r.get('added', '')}")
    lines.append(f"\n共 {len(rs)} 个号 · 台账文件 {store()}")
    return "\n".join(lines)


def selftest():
    """自带冒烟测试：增删查在临时文件上跑一遍。"""
    import tempfile
    old = os.environ.get("MOONLIGHTING_ACCOUNTS")
    os.environ["MOONLIGHTING_ACCOUNTS"] = os.path.join(tempfile.mkdtemp(), "a.json")
    try:
        assert rows() == [], "空台账应为空"
        add("d1", "github", "a@b.c", actions=["read", "post"])
        add("d2", "qq", "x@y.z")
        assert len(rows()) == 2, "两条应该都写进去"
        assert load()["d1"]["actions"] == ["read", "post"]
        try:
            add("d1", "github", "dup")
            raise AssertionError("重复桌面应该被拒绝")
        except SystemExit:
            pass
        remove("d1")
        assert [r["desktop"] for r in rows()] == ["d2"], "删完只剩 d2"
        assert "共 1 个号" in table()
        print("selftest: 通过（增 / 删 / 查 / 重复拒绝 / 渲染）")
    finally:
        if old is None:
            os.environ.pop("MOONLIGHTING_ACCOUNTS", None)
        else:
            os.environ["MOONLIGHTING_ACCOUNTS"] = old


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")

    a = sub.add_parser("add", help="登记一个号")
    a.add_argument("--desktop", required=True)
    a.add_argument("--platform", required=True)
    a.add_argument("--account", required=True)
    a.add_argument("--home", default="")
    a.add_argument("--profiles", default="")
    a.add_argument("--cdp", type=int, default=0)
    a.add_argument("--actions", default="read", help="逗号分隔：read,post,delete")
    a.add_argument("--note", default="")

    sub.add_parser("list", help="看全部")
    r = sub.add_parser("remove", help="删一个")
    r.add_argument("--desktop", required=True)

    args = ap.parse_args()
    if args.selftest:
        selftest()
        return 0
    if args.cmd == "add":
        row = add(args.desktop, args.platform, args.account, args.home,
                  args.profiles, args.cdp, [x.strip() for x in args.actions.split(",") if x.strip()],
                  args.note)
        print(f"已登记 {row['desktop']} → {row['platform']} / {row['account']}")
        return 0
    if args.cmd == "remove":
        row = remove(args.desktop)
        print(f"已删除 {row['desktop']}（{row['platform']} / {row['account']}）")
        return 0
    print(table())
    return 0


def cli():
    raise SystemExit(main())


if __name__ == "__main__":
    sys.exit(main())
