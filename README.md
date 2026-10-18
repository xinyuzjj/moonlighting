# Moonlighting

**Invisible hands for your AI agent.** A real Chrome, on its own hidden Windows desktop,
driven by element references instead of coordinates — with credentials that never reach
the model.

**English** · [中文](README.zh-CN.md)

[![PyPI](https://img.shields.io/pypi/v/moonlighting.svg)](https://pypi.org/project/moonlighting/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows-lightgrey.svg)](#platform)

```
┌─ your desktop ─────────────────────────────┐
│  Hermes / Claude / Codex / any MCP client   │
└───────────────┬────────────────────────────┘
                │ MCP (stdio)  or  local HTTP
┌───────────────▼────────────────────────────┐
│  hidden desktop "HermesDesk"                │
│  ┌──────────────────────────────────────┐   │
│  │ real Chrome + persistent profile     │   │
│  │ (logins, wallet, extensions)         │   │
│  └──────────────────────────────────────┘   │
│  credential broker (DPAPI, origin-bound)    │
└─────────────────────────────────────────────┘
```

You never see a window flash. The agent never sees a password.

## Why

Driving a browser as an agent usually goes wrong in three places:

| Problem | What most setups do | What this does |
|---|---|---|
| **Finding elements** | screenshot + guess coordinates | one accessibility snapshot, act on `[ref=e42]` |
| **Losing state** | fresh profile every run | persistent profile on a private desktop; reopen and the session is still there |
| **Handling passwords** | paste into the prompt | user types into a masked popup → DPAPI-encrypted locally → injected by the broker, never returned to the model |

## Install

Requires Windows 10/11, Python 3.10+, Node 18+ (for the Playwright MCP child), Chrome.

### Option A — pip (no paths to edit)

```bash
pip install moonlighting
moonlighting-setup --options          # where should browser data live?
moonlighting-setup --home "D:\moonlighting"
moonlighting                          # starts stream + broker + driver
```

Then wire it into your MCP client with **no absolute paths at all**:

```json
{ "mcpServers": { "browser": { "command": "moonlighting-mcp" } } }
```

`moonlighting-mcp`, `moonlighting-setup`, `moonlighting-session` and `moonlighting`
are console scripts installed by the package.

### Option B — clone

```bash
git clone https://github.com/xinyuzjj/moonlighting.git
cd moonlighting
pip install -r requirements.txt

python scripts/setup.py --options          # where should browser data live?
python scripts/setup.py --home "D:\moonlighting"
python scripts/svc.py                      # starts stream + broker + driver
```

Either way, `install_mcp.py` writes the right config for you — it detects whether
`moonlighting-mcp` is installed and falls back to absolute script paths if not:

```bash
python scripts/install_mcp.py                 # print configs with real paths
python scripts/install_mcp.py --write claude  # write it (backs up the original)
python scripts/install_mcp.py --write codex
python scripts/install_mcp.py --write all
```

`svc.py` / `moonlighting` prints a preview URL. Open it in the Hermes preview pane (or any
browser) to watch and even click into the hidden desktop:

```
preview: http://127.0.0.1:8765/ctl
```

First run creates `<home>/token.txt` — every state-changing endpoint requires it.

## Use it from an agent

### Any MCP client (Claude Desktop, Claude Code, Codex CLI, Hermes)

`scripts/mcp_server.py` is a standard MCP stdio server exposing **27 tools**
(25 `browser_*` plus `cred_list` / `cred_fill`).

If you installed with pip, the whole config is one line — no paths:

```json
{ "mcpServers": { "browser": { "command": "moonlighting-mcp" } } }
```

Cloned instead? Let `install_mcp.py` fill in the real paths:

```bash
python scripts/install_mcp.py --write all
```

Or write it by hand (Claude Desktop / Claude Code, `mcpServers`):

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

Codex CLI (`~/.codex/config.toml` — TOML, snake_case, and give it more startup time):

```toml
[mcp_servers.browser]
command = "python"
args = ["C:\\path\\to\\moonlighting\\scripts\\mcp_server.py", "--cdp-endpoint", "http://127.0.0.1:18763"]
startup_timeout_sec = 60
tool_timeout_sec = 120
```

### Plain HTTP

```bash
TOKEN=$(python scripts/auth.py --print | sed 's/^token: //')

curl -H "X-Hermes-Token: $TOKEN" http://127.0.0.1:8897/snap        # whole page + refs
curl -H "X-Hermes-Token: $TOKEN" "http://127.0.0.1:8897/click?target=e42&element=Sign%20in"
curl -H "X-Hermes-Token: $TOKEN" "http://127.0.0.1:8899/list"      # saved credentials (no secrets)
```

## Multiple agents, no interference

Each session gets its own desktop, profile, and ports — two agents cannot fight over tabs
or invalidate each other's element refs.

```bash
python scripts/session.py new --name a1 --url https://example.com
python scripts/session.py list
python scripts/session.py stop --name a1
```

```
a1   cdp=18764  stream=8766  driver=8898  UP  http://127.0.0.1:8766/ctl
```

## Running more than one instance

The defaults (`HermesDesk`, ports 8765/8897/8899/18763, one global pointer file) are meant for
a single installation. If something is already running on the machine, give the second instance
its own identity — all of it is environment variables:

```bash
export HERMES_BROWSER_HOME="D:\mb-agent"          # browser data (profiles, vault, token)
export HERMES_SETTINGS="D:\mb-agent\settings.json"  # don't touch the global pointer at all
export HERMES_DESK=MBAgentDesk
export HERMES_STREAM_PORT=18770 HERMES_BROKER_PORT=18771
export HERMES_DRIVER_PORT=18772 HERMES_CDP_PORT=18773
```

What the code does to keep them apart:

- **The pointer file is never silently rewritten.** `setup.py --home` refuses when
  `settings.json` already points somewhere else; `--force` overwrites *after* backing it up.
- **Ports are claimed with an identity check.** Services report `home` + `desk` on
  `/health` and `/status`; a port whose owner reports a different identity is treated as
  foreign and skipped, instead of being adopted ("it answered, so it must be mine").
- **Environment survives the detach.** Services are spawned through WMI so they outlive the
  agent, and WMI does *not* inherit your environment — so `svc.py` writes the isolation
  variables into a `.cmd` wrapper that starts the process. Without that, an isolated install
  silently falls back to the default home and ports.
- **No legacy fallback once you've chosen a home.** With `HERMES_BROWSER_HOME` set,
  `profiles/` and `vault/` resolve under it, never under a previous install's paths.
- **The token exists before you need it.** `svc.py` generates it at startup, so reading
  `token.txt` right after boot returns a value instead of an empty file.

`MOONLIGHTING_NPX` pins the `npx` binary when the MCP client starts you with a clean `PATH`.

## Security model

- **Passwords never enter the model context.** The user types into a masked popup; the
  broker encrypts with Windows DPAPI, binds the secret to the page origin, and injects it.
  Tool results only contain `{ok, verified, submitted, id_len, pwd_len}`.
- **Origin-bound.** A credential saved for `https://github.com` is refused on any other origin.
- **Token-gated.** `token.txt` is generated on first run (mode 600). Read-only probes
  (`/health`, `/status`, `/snapshot`) stay open so the preview keeps working.
- **Audited.** Every injection appends one line to `<home>/audit.jsonl`: timestamp, action,
  origin, handle, result code. No plaintext, ever.
- **No CAPTCHA evasion.** This drives a real browser; it does not spoof fingerprints,
  solve puzzles, or defeat anti-bot systems.

## Layout

```
scripts/
  mcp_server.py      standard MCP stdio server (browser_* + cred_*)
  browser_daemon.py  persistent driver: snapshot / click / type / tabs / eval
  desk_stream.py     live view of the hidden desktop (CDP screencast) + click/type back-channel
  vault_broker.py    credential broker: DPAPI, origin check, selector fill with read-back verify
  cred_dialog.py     masked popup the user types into
  login_flow.py      full login loop: popup → fill → verify → submit → retry on failure
  desk_chrome.py     launch / close Chrome on a named private desktop
  private_desktop.py Win32 desktop primitives (CreateDesktopW, WM_SETTEXT, PrintWindow)
  session.py         per-agent isolation (desktop + profile + ports)
  svc.py             start/stop the three services silently (no console windows)
  ports.py           port allocation: if the default is taken, pick a free one
  auth.py            token file + header helper
templates/           preview pages (/ctl interactive, /view read-only)
docs/SKILL.md        the long-form playbook: every pitfall we hit, with measurements
```

## Platform

Windows only, by design: private desktops (`CreateDesktopW`), DPAPI, and silent process
launch are Win32 primitives. The browser-control layer itself is portable — porting means
replacing `private_desktop.py` and the DPAPI calls.

## Agent loop (experimental, `agent/`)

`scripts/` is the hands; `agent/` is a thin closed loop on top of it — not packaged in the
wheel, run it from a clone.

```bash
cd moonlighting/agent
python run.py ../tasks/github-login-replay.json     # steps + assertions + retries + events.jsonl
```

| file | what it does |
|---|---|
| `obs.py` | `/snap` → compressed to ≤6 KB (actionable nodes only), `shot()` for pixels |
| `verify.py` | assertion DSL: `url` / `text` / `eval` / `count` |
| `act.py` | 11 atomic actions, hard allow-list |
| `run.py` | per-step retries, milestone rollback, `forbid` / `ask_user` / `done_when` |
| `mem.py` | site memory: host+path → successful action sequence |

Measured: 42 KB snapshot → 6.0 KB (-86%), empty click caught 10/10, failed step retries 3×
then screenshots and rolls back. There is **no planner** yet: a task is an explicit step list.


## Acknowledgements

Standing on these projects:

| Project | How it is used |
|---|---|
| [microsoft/playwright-mcp](https://github.com/microsoft/playwright-mcp) | The accessibility-snapshot engine. `mcp_server.py` proxies `browser_*` tools to it; its `[ref=...]` model is what makes coordinate-free clicking possible. (Apache-2.0) |
| [microsoft/playwright](https://github.com/microsoft/playwright) | The browser driver underneath playwright-mcp. (Apache-2.0) |
| [python-websockets/websockets](https://github.com/python-websockets/websockets) | CDP transport for the driver, the stream and the broker. |
| [python-pillow/Pillow](https://github.com/python-pillow/Pillow) | JPEG handling for the live preview and the drawn cursor. |

Design ideas borrowed, no code vendored:

- [browserbase/stagehand](https://github.com/browserbase/stagehand) — `observe()` returning real
  selectors instead of pixels; that is why this project resolves elements by selector or ref
  and never by coordinates.
- [browser-use/browser-use](https://github.com/browser-use/browser-use) — the agent loop shape:
  snapshot, act, verify.
- [web-infra-dev/midscene](https://github.com/web-infra-dev/midscene) — the vision-based fallback
  idea for canvas-only UIs (deliberately not enabled by default; it is slower).

## License

MIT. The Apache-2.0 licenses of playwright-mcp and Playwright apply to those components.
