"""端口分配：默认端口被别的程序占了就自动换一个，并把实际端口写进 ports.json。

为什么需要：8765/8899/8897/18763 只是默认值，别人机器上很可能已经被占用。
硬写死的结果是服务起不来、或者更糟——连到别人的进程上去。

规则：
  1. 默认端口空着 → 直接用。
  2. 被占用，但是**我们自己的服务**（能通过特征接口认出来）→ 继续用。
  3. 被别的程序占着 → 从默认值往上找第一个空闲端口。
实际端口落到 `ports.json`，客户端（凭据弹窗、登录脚本）读它，不靠猜。

命令行：
  python ports.py            # 打印当前分配
  python ports.py --free     # 重新分配并写入
"""
import json
import os
import socket
import sys
import urllib.request

_DEFAULT_HOME = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "hermes-browser")
SETTINGS_FILE = os.environ.get("HERMES_SETTINGS") or os.path.join(_DEFAULT_HOME, "settings.json")


def _settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _settings_home():
    return _settings().get("home") or ""


# 安装目录优先级：环境变量 > setup.py 写入的 settings.json > 默认值
HOME = os.environ.get("HERMES_BROWSER_HOME") or _settings_home() or _DEFAULT_HOME
PORTS_FILE = os.environ.get("HERMES_PORTS_FILE") or os.path.join(HOME, "ports.json")

_LEGACY_PROFILES = r"D:\Hermes浏览器"
_LEGACY_VAULT = r"E:\hermes_app\vault\broker.json"


def home():
    return HOME


def _explicit_home():
    """用户显式指定过 home（环境变量或 settings.json），legacy 回退就不该再生效。"""
    return bool(os.environ.get("HERMES_BROWSER_HOME") or _settings_home())


def profiles():
    """profile 根目录：显式配置优先；用户没选过位置时才兼容旧路径。"""
    env = os.environ.get("HERMES_PROFILES")
    if env:
        return env
    if _explicit_home():
        return os.path.join(HOME, "profiles")
    if os.path.isdir(_LEGACY_PROFILES):
        return _LEGACY_PROFILES
    return os.path.join(HOME, "profiles")


def vault_path():
    env = os.environ.get("HERMES_VAULT")
    if env:
        return env
    if _explicit_home():
        return os.path.join(HOME, "vault", "broker.json")
    if os.path.exists(_LEGACY_VAULT):
        return _LEGACY_VAULT
    return os.path.join(HOME, "vault", "broker.json")

DEFAULTS = {"stream": 8765, "broker": 8899, "driver": 8897, "cdp": 18763}
# 认领特征：能返回这些响应就认为端口上跑的是我们自己的服务
FINGERPRINTS = {
    "broker": ("/health", b'"ok"'),
    "driver": ("/status", b'"ready"'),
    "stream": ("/health", b'"desk-stream"'),
    "cdp": ("/json/version", b'"Browser"'),   # Chrome 的调试端点
}


def occupied(port, host="127.0.0.1"):
    with socket.socket() as s:
        s.settimeout(0.35)
        return s.connect_ex((host, port)) == 0


def identity():
    """这套实例的身份：home + 桌面名。服务把它写进 /health 与 /status。"""
    return {"home": os.path.normcase(os.path.abspath(HOME)),
            "desk": os.environ.get("HERMES_DESK", "HermesDesk")}


def is_ours(name, port):
    """端口上跑的到底是不是**这一套**实例。

    只看"能回 /health"不够：同一台机器上两套 moonlighting 都会回答，
    于是第二套会认领第一套的端口，隔离静默失效（真实踩过：服务起在别人的桌面上）。
    """
    fp = FINGERPRINTS.get(name)
    if not fp:
        return False
    path, needle = fp
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=1.2) as r:
            body = r.read(8192)
    except Exception:  # noqa: BLE001
        return False
    if needle is None:
        return body[:2] == b"\xff\xd8"          # JPEG 头
    if needle not in body:
        return False
    try:
        info = json.loads(body)
    except Exception:  # noqa: BLE001
        return True                            # 老版本没带身份，按自己人处理
    if "home" not in info:
        return True
    mine = identity()
    same_home = os.path.normcase(os.path.abspath(info.get("home", ""))) == mine["home"]
    same_desk = (info.get("desk") or "") == mine["desk"]
    return same_home and same_desk


def free_port(start, tries=200, exclude=()):
    skip = set(exclude)
    for p in range(start, start + tries):
        if p in skip:
            continue
        if not occupied(p):
            return p
    raise RuntimeError(f"从 {start} 起找不到空闲端口")


def load():
    try:
        with open(PORTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001
        data = {}
    out = {}
    for name, default in DEFAULTS.items():
        env = os.environ.get(f"HERMES_{name.upper()}_PORT")
        out[name] = int(env or data.get(name) or default)
    return out


def save(mapping):
    os.makedirs(os.path.dirname(PORTS_FILE), exist_ok=True)
    with open(PORTS_FILE, "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2)


def port_of(name, default=None):
    """客户端用：拿到该服务实际端口（没记录就回默认值）。"""
    return load().get(name, default or DEFAULTS.get(name, 0))


def find_existing(name, span=20):
    """在配置端口附近扫一遍，看**这一套**服务是不是已经在跑（避免重复启动）。"""
    base = port_of(name)          # 用配置值，不是 DEFAULTS：env 覆盖过端口时才对得上
    if not base:
        return None
    for p in range(base, base + span):
        if occupied(p) and is_ours(name, p):
            return p
    return None


def claim(name, default=None):
    """服务端用：返回可用的端口（自己人占着就复用，别人占着就换）。"""
    want = port_of(name, default)
    if not occupied(want) or is_ours(name, want):
        return want
    return free_port(want + 1)


def claim_all():
    """一次性分配三个端口。

    必须在本函数内做"预留"：逐个调 claim() 时，先分到的端口还没被 bind，
    对后一个来说就是"空闲"，于是两个服务分到同一个端口（真实踩过：
    stream 和 broker 都拿到 18871，两个服务抢一个监听口）。
    """
    taken, got = set(), {}
    cdp = port_of("cdp")          # cdp 是已存在浏览器的端口，只能沿用，绝不自动改
    taken.add(cdp)                # 但必须预留：否则 stream 可能被分到 cdp 同一个号
    for name in ("stream", "broker", "driver"):
        want = port_of(name)
        if want in taken or (occupied(want) and not is_ours(name, want)):
            want = free_port(want + 1, exclude=taken)
        taken.add(want)
        got[name] = want
    got["cdp"] = cdp
    save(got)
    return got


if __name__ == "__main__":
    if "--free" in sys.argv:
        print(json.dumps(claim_all(), indent=2))
    else:
        cur = load()
        for k, v in cur.items():
            state = "空闲" if not occupied(v) else ("我们自己的" if is_ours(k, v) else "被别人占用")
            print(f"  {k:<8} {v:<6} {state}")
        print(f"  记录文件: {PORTS_FILE}")
