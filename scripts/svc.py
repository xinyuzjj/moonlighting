"""启动三个辅助服务（无控制台窗口、脱离当前进程树）。

为什么要这个而不是 .bat：从 git-bash 里跑 `cmd /c xxx.bat` 会弹出一个真实的
cmd 控制台窗口（Windows 给 cmd.exe 分配的控制台），用户会看到黑框闪一下甚至
一直留着。这里用 DETACHED_PROCESS + CREATE_NO_WINDOW 起 pythonw，既不弹窗，
也不会被 Hermes 收尾时的进程树清理带走。

用法:
  python svc.py            # 缺哪个起哪个
  python svc.py --status   # 只看状态
  python svc.py --stop     # 停掉三个
"""
import argparse
import os
import re
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY_EXE = sys.executable if sys.executable.endswith("python.exe") else sys.executable + "w"
DESKTOP = os.environ.get("HERMES_DESK", "HermesDesk")
try:
    import ports as _ports
except ImportError:                     # 单独拷走这个脚本时也能跑
    _ports = None

# 端口不写死：默认值被别的程序占了就自动换（见 ports.py）
if _ports:
    P = _ports.claim_all()
else:
    P = {"stream": 8765, "broker": 8899, "driver": 8897, "cdp": 18763}
CDP, STREAM, BROKER, DRIVER = P["cdp"], P["stream"], P["broker"], P["driver"]

DETACHED = 0x00000008 | 0x08000000          # DETACHED_PROCESS | CREATE_NO_WINDOW

SERVICES = [
    ("desk_stream.py", ["--port", str(STREAM), "--desktop", DESKTOP, "--cdp", str(CDP)], STREAM),
    ("vault_broker.py", ["--port", str(BROKER)], BROKER),
    ("browser_daemon.py", ["--cdp", str(CDP), "--port", str(DRIVER)], DRIVER),
]


HIDDEN_VBS = os.path.join(HERE, "_hidden.vbs")
VBS_LINES = [
    'Set fso = CreateObject("Scripting.FileSystemObject")',
    'cmd = fso.OpenTextFile(WScript.Arguments(0)).ReadAll',
    'CreateObject("WScript.Shell").Run cmd, 0, False',
]


def _ensure_vbs():
    if not os.path.exists(HIDDEN_VBS):
        with open(HIDDEN_VBS, "w", encoding="utf-8", newline=chr(13)+chr(10)) as f:
            f.write((chr(13)+chr(10)).join(VBS_LINES) + chr(13)+chr(10))
    return HIDDEN_VBS


def _env_passthrough():
    """所有会改变隔离行为的变量，都要显式写进包装脚本。

    WMI 创建进程时用的是 WmiPrvSE 自己的环境，调用者的 export 全部丢失——
    这是"设了 home 却仍然写到默认目录"的根因，必须靠包装脚本补回来。
    """
    allow = {"HERMES_BROWSER_HOME", "HERMES_PROFILES", "HERMES_VAULT", "HERMES_DESK",
             "HERMES_SETTINGS", "HERMES_STREAM_PORT", "HERMES_BROKER_PORT",
             "HERMES_DRIVER_PORT", "HERMES_CDP_PORT"}
    keep = {k: v for k, v in os.environ.items()
            if k in allow or k.startswith("MOONLIGHTING_")}
    keep.setdefault("HERMES_BROWSER_HOME", _ports.home())
    keep.setdefault("HERMES_DESK", os.environ.get("HERMES_DESK", "HermesDesk"))
    return keep


def spawn_detached(name, args):
    """用 WMI 起进程：父进程是 WmiPrvSE.exe，不在 Hermes 的作业对象里。

    DETACHED_PROCESS 只解决控制台窗口，解决不了"收尾时杀进程树"——
    如果 Hermes 用 Job Object 并设了 KILL_ON_JOB_CLOSE，detached 的子进程照样被杀。
    WMI 创建出来的进程直接挂在 WmiPrvSE 下面，彻底脱离。
    """
    script = os.path.join(HERE, name)
    # python.exe 是控制台程序：直接用 WMI 起它一定会闪黑框。
    # 走 wscript（GUI 子系统，本身不建控制台）→ 它用隐藏窗口启动真正的命令。
    if os.name == "nt":
        try:
            # 1) 先把环境写进一个 .cmd（cmd 里 set 完再起 python，环境就跟着走了）
            runfile = os.path.join(HERE, f"_run_{name}.cmd")
            lines = ["@echo off"]
            for k, v in sorted(_env_passthrough().items()):
                lines.append(f"set {k}={v}")
            lines.append(f'"{PY_EXE}" "{script}" ' + " ".join(args))
            with open(runfile, "w", encoding="utf-8", newline="\r\n") as f:
                f.write("\r\n".join(lines) + "\r\n")

            # 2) wscript 读的是"命令行"文件，让它去起那个 .cmd
            cmdfile = os.path.join(HERE, f"_cmd_{name}.txt")
            with open(cmdfile, "w", encoding="utf-8") as f:
                f.write(f'cmd.exe /c ""{runfile}""')
            vbs = _ensure_vbs()
            wmi_cmd = f'wscript.exe //nologo "{vbs}" "{cmdfile}"'
            r = subprocess.run(["wmic", "process", "call", "create", wmi_cmd],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", creationflags=0x08000000)
            out = (r.stdout or "") + (r.stderr or "")
            if r.returncode == 0 and "ReturnValue = 0" in out:
                m = re.search(r"ProcessId = (\d+)", out)
                return int(m.group(1)) if m else 0
        except Exception:  # noqa: BLE001
            pass
    p = subprocess.Popen([PY_EXE, script] + args, cwd=HERE, creationflags=DETACHED,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
    return p.pid


def up(port):
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--no-open", action="store_true", help="不要自动打开预览窗口")
    a = ap.parse_args()

    if a.stop:
        for name, _, port in SERVICES:
            pid = _pid_on(port)
            if pid:
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
                print(f"  {name:<20} port {port} stopped (pid {pid})")
        return

    started = []
    if not a.status:
        _ensure_token()
        for name, args, port in SERVICES:
            if up(port):
                continue
            # 上次没退干净：端口漂到别处了也要认出来，别再起一个
            if _ports:
                found = _ports.find_existing(name.replace(".py", "").split("_")[0]
                                             if name.startswith("vault") else
                                             ("stream" if name.startswith("desk") else "driver"))
                if found:
                    print(f"  {name:<20} 已在 {found} 运行，跳过")
                    continue
            pid = spawn_detached(name, args)
            started.append((name, pid))
            time.sleep(0.4)
        time.sleep(6)

    for name, _, port in SERVICES:
        ok = up(port)
        real = _pid_on(port) if ok else None
        print(f"  {name:<20} port {port:<6} {'UP ' if ok else 'DOWN'} "
              + (f"(pid {real})" if real else ""))
    if started:
        # 报出去的必须是真正监听的进程；wscript 的 pid 用户 taskkill 了也没用
        print("  started:", ", ".join(
            f"{n}(wrapper pid {p})" for n, p in started),
              "  <- 真正监听的是上面 UP 那行的 pid")
    url = f"http://127.0.0.1:{STREAM}/ctl"
    print(f"  preview: {url}")
    if not a.no_open:
        # 建好桌面就把画面开出来，别让人再去翻日志找地址
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass


def _ensure_token():
    """服务启动时就把 token 落盘。之前是懒生成，用户 cat token.txt 拿到空串然后 401。"""
    try:
        import auth
        t = auth.token()
        print(f"  token: {t[:8]}…  ({auth.TOKEN_FILE if hasattr(auth, 'TOKEN_FILE') else 'home'})")
    except Exception as e:  # noqa: BLE001
        print(f"  token: 生成失败 {e}")


def _pid_on(port):
    out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stdout
    for line in out.splitlines():
        if f":{port} " in line and "LISTENING" in line:
            return line.split()[-1]
    return None


if __name__ == "__main__":
    main()


def cli():
    """pip 安装后的命令行入口。"""
    raise SystemExit(main())
