# Moonlighting

**你把号建好，AI agent 替你管。** 一个桌面 = 一个平台 = 一个账号：agent 在属于自己的隐藏 Windows
桌面里登录、操作、切换标签页，账号密码存进该桌面独立的凭据库，**永远不进模型上下文**。
邮箱也能交给它——用它去各平台把号申请下来，验证码自己收、自己填。

[![站点](https://img.shields.io/badge/%E5%AE%98%E7%BD%91-xinyuzjj.github.io%2Fmoonlighting-2ea043)](https://xinyuzjj.github.io/moonlighting/)
[![PyPI](https://img.shields.io/pypi/v/moonlighting.svg)](https://pypi.org/project/moonlighting/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows-lightgrey.svg)](#平台要求)

[English](README.en.md) · **中文**

```
┌─ 你的桌面 ─────────────────────────────────┐
│  Hermes / Claude / Codex / 任何 MCP 客户端  │
└───────────────┬────────────────────────────┘
                │ MCP (stdio)  或  本地 HTTP
┌───────────────▼────────────────────────────┐
│  隐藏桌面 "HermesDesk"                      │
│  ┌──────────────────────────────────────┐   │
│  │ 真实 Chrome / Edge + 持久化 profile   │   │
│  │ （登录态、钱包、扩展都在里面）          │   │
│  └──────────────────────────────────────┘   │
│  凭据代理（DPAPI 加密、绑定 origin）         │
└─────────────────────────────────────────────┘
```

你看不到任何窗口闪出来，agent 也看不到任何密码。

## 它解决什么

让 agent 操作浏览器，通常死在三个地方：

| 翻车点 | 常见做法 | 这里的做法 |
|---|---|---|
| **找元素** | 截图 + 猜坐标，差 11px 就点空 | 一次无障碍树快照，按 `[ref=e42]` 精确点击 |
| **丢状态** | 每次都是全新 profile，登录态归零 | 私有桌面上的持久 profile，关掉重开还在 |
| **传密码** | 把密码贴进 prompt | 你自己在掩码窗里输 → DPAPI 加密 → 代理注入，模型只拿到长度 |

42KB 的页面快照压到 6KB 再喂模型，20 步不会烧掉 20 万 token。

## 快速开始

需要 Windows 10/11、Python 3.10+、Node 18+，以及**任意 Chromium 系浏览器（Chrome 或 Edge）**——
Windows 自带 Edge，所以干净的机器直接能用。浏览器不打包，用你系统里那个。

```bash
pip install moonlighting
moonlighting-setup --home "D:\moonlighting"   # 浏览器数据放哪
moonlighting                                  # 起推流 / 凭据代理 / 浏览器驱动
```

启动后会打印预览地址并**自动打开窗口**：

```
preview: http://127.0.0.1:8765/ctl
```

画面是实时的，可以直接在上面点、在上面打字——输入会反向送进隐藏桌面。

浏览器查找顺序：`HERMES_CHROME` → 注册表 `App Paths` → 常见安装路径 → `PATH`；
一个都没有时给一句看得懂的话，不抛 traceback。

## 接进 agent

### 一行配置（pip 安装后）

```json
{ "mcpServers": { "browser": { "command": "moonlighting-mcp" } } }
```

`moonlighting-mcp`、`moonlighting-setup`、`moonlighting-session`、`moonlighting`
都是装包时生成的命令，**没有任何绝对路径要填**。

暴露 **27 个工具**：25 个 `browser_*`（快照、点击、输入、导航、标签页、求值…）
加 `cred_list` / `cred_fill`。Codex CLI 用 `~/.codex/config.toml`：

```toml
[mcp_servers.browser]
command = "moonlighting-mcp"
startup_timeout_sec = 60
tool_timeout_sec = 120
```

### 从源码跑

```bash
git clone https://github.com/xinyuzjj/moonlighting.git
cd moonlighting && pip install -r requirements.txt
python scripts/svc.py

python scripts/install_mcp.py                 # 打印带真实路径的配置
python scripts/install_mcp.py --write all     # 直接写进 Claude / Codex（自动备份原文件）
```

## 凭据怎么走

```
用户 ──掩码弹窗──► DPAPI 加密存储 ──handle──► agent
                                              │
                             cred_fill(handle) │ origin 校验 + 读回校验
                                              ▼
                                           填进页面
```

- **密码不进模型上下文。** agent 只拿到 `{ok, verified, submitted, id_len, pwd_len}`。
- **绑定 origin。** 给 `https://github.com` 存的凭据，在别的 origin 上会被直接拒绝。
- **每次注入都有审计。** 往 `<home>/audit.jsonl` 追加时间、动作、origin、handle、结果码，永不记明文。
- **token 门禁。** 首个请求前生成 `token.txt`；`/health`、`/status` 保持开放，预览不会被卡住。
- **不绕人机识别。** 驱动的是真实浏览器，不改指纹、不打码、不硬闯风控。

## 多套实例并存

默认值（`HermesDesk`、8765/8897/8899/18763、一个全局指针文件）是按「一台机器一套」设计的。
机器上已经有一套在跑时，第二套给个自己的身份——全是环境变量：

```bash
export HERMES_BROWSER_HOME="E:\\browser\\junjunni"    # 端口表 / token / 凭据库（每套必须独立）
export HERMES_PROFILES="D:\\Hermes浏览器"             # profile 根，可共用，每个桌面一个子目录
export HERMES_DESK=junjunni
export HERMES_STREAM_PORT=8766 HERMES_BROKER_PORT=8900 HERMES_DRIVER_PORT=8898
```

代码靠这几条把它们隔开：

- **home 每套独立。** `ports.json` 和 `token.txt` 是按 home 命名的单文件，共用会互相覆盖端口表。
- **认领端口要验身份。** 服务在 `/health`、`/status` 里报 `home` + `desk`；端口上的服务报的是另一套身份，就当别人的跳过，而不是「它能回答所以是我的」。
- **指针文件绝不静默改写。** `setup.py --home` 发现 `settings.json` 已指向别处会直接拒绝，`--force` 先备份再覆盖。
- **环境变量穿得过 detach。** 服务用 WMI 起（为了活过 agent 收尾），而 WMI 不继承调用者环境，所以 `svc.py` 把隔离变量写进 `.cmd` 包装脚本。缺这一步，隔离配置会静默丢失，数据写回默认目录。
- **选过 home 就不回退旧路径。** `profiles/`、`vault/` 都在它下面解析。
- **凭据库跟着 home 走。** A 桌面的 handle 在 B 桌面的 broker 上会返回 not found，跨实例填不进去。

## 账号台账

一个桌面 = 一个平台 = 一个号。台账把这件事记下来，省得号多了靠人脑记：

```bash
python scripts/accounts.py add --desktop junjunni --platform github        --account me@example.com --cdp 18764 --actions read,post
python scripts/accounts.py list
python scripts/accounts.py remove --desktop junjunni
```

```
桌面            平台        账号                  权限      状态      新增
junjunni        github      me@example.com        read,post unknown   2026-10-18 11:15
```

**台账只存元数据，不存密码**——密码在各桌面自己的 vault 里。状态列现在是 `unknown`，
等登录态体检做出来才会填 `ok` / `expired`。文件默认在
`%LOCALAPPDATA%\moonlightingccounts.json`（`MOONLIGHTING_ACCOUNTS` 可覆盖）。

## Agent 闭环（实验，`agent/`）

`scripts/` 是手，`agent/` 是套在手上的闭环——**不在 wheel 里**，从 clone 里跑。

```bash
cd agent
python run.py ../tasks/github-login-replay.json     # 步骤 + 断言 + 重试 + events.jsonl
```

| 文件 | 作用 |
|---|---|
| `obs.py` | `/snap` → 压到 ≤6KB（只留可操作节点），`shot()` 抓像素兜底 |
| `verify.py` | 断言 DSL：`url` / `text` / `eval` / `count` |
| `act.py` | 11 个原子动作，硬白名单 |
| `run.py` | 每步 3 次重试、连败截图、milestone 回滚、`forbid` / `ask_user` / `done_when` |
| `mem.py` | 站点记忆：host+path → 成功动作序列 |

**每条步骤都必须带断言**，没有 check 的任务会被拒跑——这是「点了没反应还继续走」的唯一根治点。
完成判定只认 `done_when`，不认模型的自然语言声明。还没有 planner：任务是显式步骤列表。

## 实测数字

| 项 | 数字 |
|---|---|
| 快照压缩 | GitHub 仓库页 56.2KB → 6.0KB（-89%）；MDN 42.4KB → 6.0KB（-86%） |
| 单步耗时 | 快照 83–250ms、click 39–1136ms、goto 586–1797ms |
| 空点击检出 | 故意点不存在的元素 → 动作 FAIL + 断言 FAIL，10/10 命中 |
| 失败恢复 | 重试 3 次 → 第 2 次自动截图 → 断言失败 → 回滚 milestone（URL + 表单值） |
| 内核 | Chrome 155 与 Edge 148 均实测通过，CDP 同一套协议 |
| 隔离 | 两套实例并存：8765/8899/8897 与 8766/8900/8898，`/health` 各报各的 home+desk |

## 目录结构

```
scripts/
  mcp_server.py      标准 MCP stdio server（browser_* + cred_*）
  browser_daemon.py  常驻驱动：快照 / 点击 / 输入 / 标签页 / 求值
  desk_stream.py     隐藏桌面实时画面（CDP screencast）+ 点击键盘反向通道
  vault_broker.py    凭据代理：DPAPI、origin 校验、选择器填充 + 读回校验
  cred_dialog.py     用户输入用的掩码弹窗
  login_flow.py      完整登录闭环：弹窗 → 填充 → 校验 → 提交 → 失败重弹
  desk_chrome.py     在指定私有桌面启动/关闭浏览器（自动找 Chrome 或 Edge）
  private_desktop.py Win32 桌面原语（CreateDesktopW、WM_SETTEXT、PrintWindow）
  svc.py             静默启停三个服务，启动后自动打开预览
  ports.py           端口分配 + 身份校验（默认端口被占就自动换）
  auth.py            token 文件与请求头
  install_mcp.py     给 Claude / Codex / Hermes 写 MCP 配置
  templates/         预览页（/ctl 可操作、/view 只读）
agent/               闭环层（obs / act / verify / run / mem）
tasks/               示例任务，含故意失败用例
docs/SKILL.md        长篇实战手册：每个坑都带实测数字
```

## 平台要求

只支持 Windows，这是设计选择：私有桌面（`CreateDesktopW`）、DPAPI、静默启动都是 Win32 原语。
浏览器控制这一层本身是可移植的——移植等于重写 `private_desktop.py` 和 DPAPI 那几处。

## 鸣谢

站在这些项目上：

| 项目 | 用在哪 |
|---|---|
| [microsoft/playwright-mcp](https://github.com/microsoft/playwright-mcp) | 无障碍树快照引擎。`browser_*` 工具全部转发给它；没有它的 `[ref=...]` 模型，就不可能做到不碰坐标的点击。（Apache-2.0） |
| [microsoft/playwright](https://github.com/microsoft/playwright) | playwright-mcp 底层的浏览器驱动。（Apache-2.0） |
| [python-websockets/websockets](https://github.com/python-websockets/websockets) | 驱动、推流、凭据代理三处的 CDP 传输。 |
| [python-pillow/Pillow](https://github.com/python-pillow/Pillow) | 实时画面的 JPEG 处理，以及画在画面上的光标。 |

借鉴了设计思路（未拷贝代码）：

- [browserbase/stagehand](https://github.com/browserbase/stagehand) — `observe()` 返回真实选择器而不是像素。这正是本项目坚持用 ref 定位、绝不用坐标的原因。
- [browser-use/browser-use](https://github.com/browser-use/browser-use) — agent 循环的形态：快照、动作、校验。
- [web-infra-dev/midscene](https://github.com/web-infra-dev/midscene) — 纯 canvas 界面的视觉兜底思路（刻意不默认开启，因为慢）。

## 支持本项目

Moonlighting 免费、无广告、不收集任何数据，也不会因为没打赏就少更新一次。

如果它帮你省下了来回登录、切换平台、复制验证码的功夫，可以请作者喝杯咖啡 👇

<p align="center">
  <img src="docs/reward.png" width="300" alt="峻峻尼 的赞赏码">
</p>

<p align="center"><sub>微信扫码即可 · 完全自愿 · 感谢每一位使用者</sub></p>

## 许可证

MIT。playwright-mcp 与 Playwright 的 Apache-2.0 许可适用于对应组件。
