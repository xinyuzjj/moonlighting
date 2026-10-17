# Moonlighting

**Invisible hands for your AI agent.** A real Chrome, on its own hidden Windows desktop,
driven by element references instead of coordinates — with credentials that never reach
the model.

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

```bash
git clone https://github.com/xinyuzjj/moonlighting.git
cd moonlighting
pip install -r requirements.txt

python scripts/setup.py --options          # where should browser data live?
python scripts/setup.py --home "D:\moonlighting"
python scripts/svc.py                      # starts stream + broker + driver
```

`svc.py` prints a preview URL. Open it in the Hermes preview pane (or any browser) to watch
and even click into the hidden desktop:

```
preview: http://127.0.0.1:8765/ctl
```

First run creates `<home>/token.txt` — every state-changing endpoint requires it.

## Use it from an agent

### Any MCP client (Claude Desktop, Claude Code, Codex CLI, Hermes)

`scripts/mcp_server.py` is a standard MCP stdio server exposing **27 tools**
(25 `browser_*` plus `cred_list` / `cred_fill`).

Claude Desktop / Claude Code (`mcpServers`):

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

## License

MIT. This project delegates browsing to [@playwright/mcp](https://github.com/microsoft/playwright-mcp)
(Apache-2.0) and Playwright (Apache-2.0); those licenses apply to those components.
