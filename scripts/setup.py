"""首次安装：选浏览器数据装在哪里。

浏览器 profile 里有登录态、钱包扩展、凭据库——装错地方以后迁移很麻烦，
所以安装时**必须先问用户**，而不是默认塞进某个盘。

两种用法：

1. 给聊天界面用的候选清单（agent 拿去渲染成选择项）：
       python setup.py --options
   输出 JSON：[{id,label,path,writable,free_gb,note}]

2. 用户选好后落盘：
       python setup.py --home "D:\\hermes-browser"
   写入 %LOCALAPPDATA%\\hermes-browser\\settings.json（指针文件，位置固定），
   之后所有脚本按这个 home 找 profile / vault / ports.json。

3. 人工交互（终端里直接跑）：
       python setup.py
"""
import argparse
import json
import os
import shutil
import string
import sys
import time

PTR_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "hermes-browser")
SETTINGS = os.environ.get("HERMES_SETTINGS") or os.path.join(PTR_DIR, "settings.json")


def candidates():
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(here)          # 代码目录本身不该被推荐成数据目录
    out = []
    out.append(("localappdata", "系统用户数据目录（推荐，默认）",
                os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "hermes-browser")))
    out.append(("userprofile", "用户主目录",
                os.path.join(os.path.expanduser("~"), "hermes-browser")))
    for drive in string.ascii_uppercase:
        root = f"{drive}:\\"
        if os.path.isdir(root):
            p = os.path.join(root, "hermes-browser")
            if os.path.normcase(p) not in {os.path.normcase(c[2]) for c in out}:
                out.append((f"drive_{drive.lower()}", f"{drive} 盘根目录", p))
    res = []
    for cid, label, path in out:
        if os.path.normcase(os.path.abspath(path)) in (
                os.path.normcase(repo), os.path.normcase(here)):
            continue                      # 代码放在哪，就别建议装到哪
        res.append({"id": cid, "label": label, "path": path,
                    "writable": _writable(path), "free_gb": _free_gb(path),
                    "note": "已有数据" if _has_data(path) else ""})
    return res


def _writable(path):
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write_test")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except Exception:  # noqa: BLE001
        return False


def _free_gb(path):
    try:
        root = os.path.splitdrive(os.path.abspath(path))[0] or path
        while root and not os.path.isdir(root):
            root = os.path.dirname(root)
        return round(shutil.disk_usage(root).free / 1024 ** 3, 1)
    except Exception:  # noqa: BLE001
        return None


def _has_data(path):
    return any(os.path.isdir(os.path.join(path, sub)) for sub in ("profiles", "vault"))


def load():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def save(home, desk=None, force=False):
    """写指针文件。已存在且指向别处时**先备份并要求 --force**，绝不静默覆盖。

    这个文件是"浏览器数据装在哪"的唯一指针：静默改掉它，用户原有的 profile 根和凭据库
    就不再被解析，下次跑起来是空环境（真实踩过）。
    """
    home = os.path.abspath(home)
    os.makedirs(home, exist_ok=True)
    cur = load()
    prev = cur.get("home")
    backup = ""
    if prev and os.path.normcase(os.path.abspath(prev)) != os.path.normcase(home):
        if not force:
            raise SystemExit(
                f"拒绝覆盖：{SETTINGS} 已指向 {prev}\n"
                f"  要改到 {home}，加 --force（原文件会自动备份）；\n"
                f"  想完全不动全局指针，就把 HERMES_SETTINGS 指向你自己的文件。")
        if os.path.exists(SETTINGS):
            backup = SETTINGS + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
            shutil.copy2(SETTINGS, backup)
    data = dict(cur)
    data.update({"home": home, "installed_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    if desk:
        data["desk"] = desk
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    with open(SETTINGS, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    if backup:
        data["backup"] = backup
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--options", action="store_true", help="输出候选位置（JSON，给聊天界面用）")
    ap.add_argument("--home", default="", help="用户选定的安装目录")
    ap.add_argument("--desk", default="", help="私有桌面名（可选）")
    ap.add_argument("--show", action="store_true", help="显示当前配置")
    ap.add_argument("--force", action="store_true", help="指针已指向别处时仍然覆盖（自动备份）")
    a = ap.parse_args()

    if a.options:
        print(json.dumps(candidates(), ensure_ascii=False, indent=2))
        return 0

    if a.show:
        print(json.dumps({"settings_file": SETTINGS, **load()}, ensure_ascii=False, indent=2))
        return 0

    if a.home:
        if not _writable(a.home):
            print(json.dumps({"ok": False, "error": f"目录不可写：{a.home}"}, ensure_ascii=False))
            return 2
        data = save(a.home, a.desk or None, force=a.force)
        print(json.dumps({"ok": True, **data, "settings_file": SETTINGS}, ensure_ascii=False, indent=2))
        return 0

    # 交互模式
    opts = candidates()
    print("浏览器数据装在哪里？\n")
    for i, o in enumerate(opts, 1):
        free = f"{o['free_gb']}GB 可用" if o["free_gb"] is not None else "空间未知"
        print(f"  {i}) {o['label']}  {o['path']}  ({free}{'，' + o['note'] if o['note'] else ''})")
    print(f"  {len(opts) + 1}) 自定义路径")
    raw = input("\n输入编号: ").strip()
    if raw.isdigit() and 1 <= int(raw) <= len(opts):
        home = opts[int(raw) - 1]["path"]
    elif raw.isdigit() and int(raw) == len(opts) + 1:
        home = input("输入完整路径: ").strip().strip('"')
    else:
        home = raw.strip().strip('"')
    if not home:
        print("没有选择，退出。")
        return 1
    if not _writable(home):
        print(f"目录不可写：{home}")
        return 2
    data = save(home, force=True)
    print(f"\n已安装到: {data['home']}\n配置: {SETTINGS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


def cli():
    """pip 安装后的命令行入口。"""
    raise SystemExit(main())
