# Moonlighting

**给你的 AI agent 一双看不见的手。** 在它自己的隐藏 Windows 桌面上跑一个真实 Chrome，
按元素引用操作而不是算坐标——密码永远不进模型上下文。

[English](README.md) · **中文**

[![PyPI](https://img.shields.io/pypi/v/moonlighting.svg)](https://pypi.org/project/moonlighting/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows-lightgrey.svg)](#平台)

```
┌─ 你的桌面 ─────────────────────────────────┐
│  Hermes / Claude / Codex / 任何 MCP 客户端  │
└───────────────┬────────────────────────────┘
                │ MCP (stdio)  或  本地 HTTP
┌───────────────▼────────────────────────────┐
│  隐藏桌面 "HermesDesk"                      │
│  ┌──────────────────────────────────────┐   │
│  │ 真实 Chrome + 持久化 profile          │   │
│  │ （登录态、钱包、扩展都在里面）          │   │
│  └──────────────────────────────────────┘   │
│  凭据代理（DPAPI 加密，绑定 origin）         │
└─────────────────────────────────────────────┘
```

你看不到任何窗口闪出来，agent 也看不到任何密码。

## 解决什么问题

让 agent 操作浏览器，通常会在三个地方翻车：

| 问题 | 常见做法 | 这里的做法 |
|---|---|---|
| **找元素** | 截图 + 猜坐标 | 一次无障碍树快照，按 `[ref=e42]` 操作 |
| **丢状态** | 每次跑都是全新 profile | 私有桌面上的持久 profile，重开还在 |
| **处理密码** | 把密码贴进 prompt | 用户自己在掩码弹窗里输入 → 本地 DPAPI 加密 → 代理注入，不返回给模型 |

## 安装

需要 Windows 10/11、Python 3.10+、Node 18+（Playwright MCP 子进程要用）、Chrome。

### 方式 A —— pip 安装（不用改任何路径）

```bash
pip install moonlighting
moonlighting-setup --options          # 先问浏览器数据装哪里
moonlighting-setup --home "D:\moonlighting"
moonlighting                          # 启动推流 / 凭据代理 / 浏览器驱动
```

接进 MCP 客户端只要一行，**没有任何绝对路径**：

```json
{ "mcpServers": { "browser": { "command": "moonlighting-mcp" } } }
```

`moonlighting-mcp`、`moonlighting-setup`、`moonlighting-session`、`moonlighting`
都是装包时自动生成的命令行入口。

### 方式 B —— clone 源码

```bash
git clone https://github.com/xinyuzjj/moonlighting.git
cd moonlighting
pip install -r requirements.txt

python scripts/setup.py --options          # 先问浏览器数据装哪里
python scripts/setup.py --home "D:\moonlighting"
python scripts/svc.py                      # 启动三个服务
```

不管哪种方式，`install_mcp.py` 都会帮你把配置写对——它检测到 `moonlighting-mcp`
就用零路径写法，没装就回落到脚本的绝对路径：

```bash
python scripts/install_mcp.py                 # 打印带真实路径的配置
python scripts/install_mcp.py --write claude  # 直接写入（自动备份原文件）
python scripts/install_mcp.py --write codex
python scripts/install_mcp.py --write all
```

`svc.py` / `moonlighting` 会打印预览地址。把它放进 Hermes 的预览面板（或任意浏览器），
就能看画面、甚至直接在画面上点击：

```
preview: http://127.0.0.1:8765/ctl
```

首次运行会生成 `<home>/token.txt`——所有会改变状态的接口都要带这个 token。

## 给 agent 用

### 任何 MCP 客户端（Claude Desktop、Claude Code、Codex CLI、Hermes）

`scripts/mcp_server.py` 是一个标准 MCP stdio server，暴露 **27 个工具**
（25 个 `browser_*` + `cred_list` / `cred_fill`）。

pip 装过的话，配置就一行，**没有任何路径**：

```json
{ "mcpServers": { "browser": { "command": "moonlighting-mcp" } } }
```

是 clone 的？让 `install_mcp.py` 把真实路径填好：

```bash
python scripts/install_mcp.py --write all
```

或者手写（Claude Desktop / Claude Code，`mcpServers`）：

```json
{
  "mcpServers": {
    "browser": {
      "command": "python",
      "args": ["C:\\path\\to\\moonlighting\\scripts\\mcp_server.py",
               "--cdp-endpoint", "http://127.0.0.1:18763"]
    }
  }
}
```

Codex CLI（`~/.codex/config.toml`——TOML 格式、snake_case，启动超时要给足）：

```toml
[mcp_servers.browser]
command = "python"
args = ["C:\\path\\to\\moonlighting\\scripts\\mcp_server.py", "--cdp-endpoint", "http://127.0.0.1:18763"]
startup_timeout_sec = 60
tool_timeout_sec = 120
```

### 直接走 HTTP

```bash
TOKEN=$(python scripts/auth.py --print | sed 's/^token: //')

curl -H "X-Hermes-Token: $TOKEN" http://127.0.0.1:8897/snap        # 整页快照 + ref
curl -H "X-Hermes-Token: $TOKEN" "http://127.0.0.1:8897/click?target=e42&element=Sign%20in"
curl -H "X-Hermes-Token: $TOKEN" "http://127.0.0.1:8899/list"      # 已存凭据（不含密码）
```

## 多 agent 互不干扰

每个 session 有独立的桌面、profile 和端口——两个 agent 不会抢标签，也不会让对方刚拿到的
ref 失效。

```bash
python scripts/session.py new --name a1 --url https://example.com
python scripts/session.py list
python scripts/session.py stop --name a1
```

```
a1   cdp=18764  stream=8766  driver=8898  UP  http://127.0.0.1:8766/ctl
```

## 安全模型

- **密码不进模型上下文。** 用户在掩码弹窗里输入，代理用 Windows DPAPI 加密、绑定页面 origin、
  自己注入。工具返回值只有 `{ok, verified, submitted, id_len, pwd_len}`。
- **绑定 origin。** 为 `https://github.com` 保存的凭据，在别的 origin 上会被直接拒绝。
- **token 门禁。** 首次运行生成 `token.txt`（权限 600）。只读探针（`/health`、`/status`、
  `/snapshot`）保持开放，预览才不会被卡住。
- **有审计。** 每次注入往 `<home>/audit.jsonl` 追加一行：时间、动作、origin、handle、结果码。
  永远不记明文。
- **不绕人机识别。** 这里驱动的是真实浏览器，不改指纹、不打码、不硬闯风控。

## 目录结构

```
scripts/
  mcp_server.py      标准 MCP stdio server（browser_* + cred_*）
  browser_daemon.py  常驻驱动：快照 / 点击 / 输入 / 标签页 / 求值
  desk_stream.py     隐藏桌面的实时画面（CDP screencast）+ 点击键盘反向通道
  vault_broker.py    凭据代理：DPAPI、origin 校验、选择器填充 + 读回校验
  cred_dialog.py     用户输入用的掩码弹窗
  login_flow.py      完整登录闭环：弹窗 → 填充 → 校验 → 提交 → 失败重弹
  desk_chrome.py     在指定私有桌面上启动 / 关闭 Chrome
  private_desktop.py Win32 桌面原语（CreateDesktopW、WM_SETTEXT、PrintWindow）
  session.py         多 agent 隔离（桌面 + profile + 端口）
  svc.py             静默启动/停止三个服务（不弹控制台窗口）
  ports.py           端口分配：默认端口被占就自动换一个
  auth.py            token 文件与请求头
templates/           预览页（/ctl 可操作、/view 只读）
docs/SKILL.md        长篇实战手册：我们踩过的每个坑，都带实测数字
```

## 平台

只支持 Windows，这是设计选择：私有桌面（`CreateDesktopW`）、DPAPI、静默启动都是 Win32 原语。
浏览器控制这一层本身是可移植的——移植等于重写 `private_desktop.py` 和 DPAPI 那几处。

## 鸣谢

站在这些项目上：

| 项目 | 用在哪 |
|---|---|
| [microsoft/playwright-mcp](https://github.com/microsoft/playwright-mcp) | 无障碍树快照引擎。`mcp_server.py` 把 `browser_*` 工具转发给它；没有它的 `[ref=...]` 模型，就不可能做到不碰坐标的点击。（Apache-2.0） |
| [microsoft/playwright](https://github.com/microsoft/playwright) | playwright-mcp 底层的浏览器驱动。（Apache-2.0） |
| [python-websockets/websockets](https://github.com/python-websockets/websockets) | 驱动、推流、凭据代理三处的 CDP 传输。 |
| [python-pillow/Pillow](https://github.com/python-pillow/Pillow) | 实时画面的 JPEG 处理，以及画在画面上的光标。 |

借鉴了设计思路（未拷贝代码）：

- [browserbase/stagehand](https://github.com/browserbase/stagehand) — `observe()` 返回真实选择器而不是像素。
  这正是本项目坚持用选择器/ref 定位、绝不用坐标的原因。
- [browser-use/browser-use](https://github.com/browser-use/browser-use) — agent 循环的形态：快照、动作、校验。
- [web-infra-dev/midscene](https://github.com/web-infra-dev/midscene) — 针对纯 canvas 界面的视觉兜底思路
  （刻意不默认开启，因为慢）。

## 许可证

MIT。playwright-mcp 与 Playwright 的 Apache-2.0 许可适用于对应组件。
