"""一键把 moonlighting 接进各个 MCP 客户端——不用手改路径。

优先用 pip 安装后的命令 `moonlighting-mcp`（零路径）；没装则回落到
`python <本文件同目录>/mcp_server.py` 的绝对路径。

    python install_mcp.py                 # 只打印，不写文件
    python install_mcp.py --write claude  # 写进 Claude Desktop 配置（自动备份）
    python install_mcp.py --write codex   # 写进 ~/.codex/config.toml
    python install_mcp.py --write all
"""
import argparse
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "mcp_server.py")


def command():
    """返回 (command, args) —— 优先用已安装的 console script。"""
    exe = shutil.which("moonlighting-mcp")
    if exe:
        return exe, []
    return sys.executable, [SERVER]


def claude_path():
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "Claude", "claude_desktop_config.json")
    return os.path.expanduser("~/Library/Application Support/Claude/claude_desktop_config.json")


def codex_path():
    return os.path.join(os.path.expanduser("~"), ".codex", "config.toml")


def snippet(cmd, args):
    entry = {"command": cmd}
    if args:
        entry["args"] = args
    return {"mcpServers": {"browser": entry}}


def write_claude(cmd, args):
    p = claude_path()
    data = {}
    if os.path.exists(p):
        shutil.copy2(p, p + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:  # noqa: BLE001
            data = {}
    data.setdefault("mcpServers", {})["browser"] = snippet(cmd, args)["mcpServers"]["browser"]
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return p


def write_codex(cmd, args):
    p = codex_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    body = ""
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            body = f.read()
        if "[mcp_servers.browser]" in body:
            return p + "（已存在，未覆盖）"
        shutil.copy2(p, p + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
    args_toml = ", ".join(f'"{a}"' for a in args)
    block = (
        "\n[mcp_servers.browser]\n"
        f'command = "{cmd}"\n'
        + (f"args = [{args_toml}]\n" if args else "")
        + "startup_timeout_sec = 60\ntool_timeout_sec = 120\n"
    )
    with open(p, "a", encoding="utf-8") as f:
        f.write(block)
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", choices=["claude", "codex", "all"], default="")
    a = ap.parse_args()

    cmd, args = command()
    installed = shutil.which("moonlighting-mcp") is not None

    print("检测到的启动方式：", "pip 安装的 moonlighting-mcp" if installed else "本地脚本（绝对路径）")
    print()
    print("== Claude Desktop / Claude Code ==")
    print(json.dumps(snippet(cmd, args), indent=2, ensure_ascii=False))
    print()
    print("== Codex CLI (~/.codex/config.toml) ==")
    args_toml = ", ".join(f'"{x}"' for x in args)
    print("[mcp_servers.browser]")
    print(f'command = "{cmd}"')
    if args:
        print(f"args = [{args_toml}]")
    print("startup_timeout_sec = 60")
    print("tool_timeout_sec = 120")
    print()
    print("== Hermes ==")
    print(f'hermes mcp add browser --command "{cmd}"'
          + (f' --args {" ".join(args)}' if args else ""))
    print()

    if a.write in ("claude", "all"):
        print("已写入 Claude:", write_claude(cmd, args))
    if a.write in ("codex", "all"):
        print("已写入 Codex:", write_codex(cmd, args))
    if not a.write:
        print("（只打印。要真正写配置：--write claude|codex|all，会自动备份原文件）")
    return 0


def cli():
    raise SystemExit(main())


if __name__ == "__main__":
    sys.exit(main())
