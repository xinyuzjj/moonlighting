"""本地服务的访问令牌：挡住同机其他进程乱调。

问题：8899 上跑着凭据代理，任何本机进程都能 `GET /inject_sel?handle=...` 让浏览器
把密码填进一个 origin 相同的页面（同源伪造），8897/8765 同理能驱动浏览器和注入按键。

做法：
  * 首次启动生成 32 字节随机 token，写到 `<home>/token.txt`
  * 所有**会改变状态**的接口要求 `X-Hermes-Token` 头（或 `?token=`）
  * 只读接口（/snapshot、/status、/list 的元信息）不强制，方便看画面
  * 客户端（cred_dialog、login_flow）自己从同一个文件读，用户无感

环境变量 `HERMES_TOKEN` 优先，便于 CI/临时覆盖。
"""
import os
import secrets

HOME = os.environ.get("HERMES_BROWSER_HOME") or os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "hermes-browser")
try:                                    # 装了 setup.py 就跟着它走
    import ports as _ports
    HOME = _ports.home()
except Exception:  # noqa: BLE001
    pass
TOKEN_FILE = os.environ.get("HERMES_TOKEN_FILE") or os.path.join(HOME, "token.txt")


def token():
    env = os.environ.get("HERMES_TOKEN")
    if env:
        return env
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            t = f.read().strip()
        if t:
            return t
    except OSError:
        pass
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    t = secrets.token_urlsafe(24)
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(t)
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    return t


def check(headers, query):
    """headers: email.Message；query: dict[str, list[str]] 或 dict[str,str]"""
    want = token()
    got = headers.get("X-Hermes-Token") if headers is not None else None
    if not got and query:
        v = query.get("token")
        got = (v[0] if isinstance(v, list) else v) if v else None
    return bool(got) and secrets.compare_digest(str(got), want)


def headers_for_client():
    return {"X-Hermes-Token": token()}


if __name__ == "__main__":
    print("token 文件:", TOKEN_FILE)
    print("token:", token()[:8] + "…（已写入文件，不完整打印）")
