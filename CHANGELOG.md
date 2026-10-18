# Changelog

## 0.1.3 — 2026-10-18

- **Works without Chrome.** The launcher hard-coded three Chrome paths and then fell back to
  `"chrome"` on `PATH`, so a machine without Chrome died with a `FileNotFoundError`. It now
  resolves `HERMES_CHROME` → registry `App Paths` (chrome.exe, msedge.exe) → common install
  locations → `PATH`, and Edge counts: verified end-to-end on Edge 148 (CDP, snapshot 408ms,
  two isolated instances on separate user-data-dirs).
- `desk_stream --spawn` uses the same resolver instead of its own shorter list.

## 0.1.2 — 2026-10-18

Fixes from a second test pass on 0.1.1.

- **MCP failed to start under a clean `PATH`.** `_npx()` resolved the `npx` shim but never put
  `node` on the child's `PATH`, so `npx` died with `'"node"' is not recognized` and the failure
  surfaced as `初始化失败: None`. `node`'s directory (next to `npx`, `NVM_SYMLINK`,
  `Program Files
odejs`, …) is now prepended to the child environment.
- **Child stderr was discarded.** Initialization failures printed `None` with no clue why.
  stderr is now drained into a bounded buffer and printed with the error.
- **`/click` and `/type` with an empty `target`** passed `""` straight to playwright-mcp, which
  parsed it as a CSS selector and returned a syntax error. They now resolve the ref by accessible
  name from a fresh snapshot and, if there is none, return `元素不存在：<name>`.
- **`agent/run.py` truncated failures at 90 chars**, cutting error messages mid-word. Failures
  print in full; successes stay truncated.
- `agent/act.py` only re-locates a ref when the failure looks like a stale ref, not on
  "element does not exist".

## 0.1.1 — 2026-10-18

Fixes for every issue found while installing a second, isolated instance on a machine that
already had one running.

**Safety**

- `setup.py --home` no longer silently rewrites the global pointer (`settings.json`). It
  refuses when the pointer already targets another directory, and `--force` backs the file up
  first. Previously a second install would leave the first one's profiles and vault unreachable.
- Port claiming now verifies *identity*: services report `home` + `desk` on `/health` and
  `/status`, and a port owned by a different identity is treated as foreign instead of being
  adopted. Previously the second instance claimed the first one's ports and connected to its
  Chrome on its desktop.
- `claim_all()` reserves ports within the call. Two services could be handed the same number
  (`stream` and `broker` both got `18871`) because an assigned-but-not-yet-bound port still
  looked free.
- `cdp` is reserved before `stream`/`broker`/`driver` are allocated, so a service can no longer
  be assigned the browser's debugging port.
- `HERMES_BROWSER_HOME` now counts as an explicit choice: `profiles/` and `vault/` no longer
  fall back to a previous installation's legacy paths once it is set.

**Isolation**

- `svc.py` writes the `HERMES_*` / `MOONLIGHTING_*` isolation variables into a `.cmd` wrapper
  before the WMI-detached launch. WMI does not inherit the caller's environment, so an isolated
  install used to come up on the default home and default ports while still reporting `UP`.
  Only isolation-relevant variables are passed through — earlier builds leaked the whole
  `HERMES_*` namespace, including agent runtime variables.

**Correctness / UX**

- Token is generated at startup, so `token.txt` is never empty right after boot.
- `svc.py` reports the real listening PID (`netstat`) instead of the `wscript.exe` wrapper PID,
  which was useless for `taskkill`.
- The preview page title is `__DESK__ · … · :__PORT__`, injected by the server, so multiple
  instances are distinguishable. It used to hard-code `HermesDesk`.
- `desk_stream` now exposes `/health` too, so port ownership is decided by identity on all
  three services rather than by "does it return a JPEG" (a stream with no browser attached
  used to look foreign and lose its own port).
- `desk_stream` strips `data-page-node-id` attributes from templates before serving, so the
  preview pane's DOM instrumentation does not end up committed to the repository.
- `mcp_server` resolves `npx` via `MOONLIGHTING_NPX` → `PATH` → common install locations, instead
  of assuming `npx.cmd` is on `PATH` (MCP clients often start children with a clean environment).
- `browser_daemon` retries once when a tool call returns empty content (observed on the first
  `/snap` after a cold start).
- `desk_chrome.py` docstring is a raw string, removing the `SyntaxWarning: invalid escape sequence`.

## 0.1.0 — 2026-10-18

First public release: hidden-desktop Chrome, ref-based control through `@playwright/mcp`,
credential broker with origin binding and read-back verification, 27 MCP tools, live preview,
multi-session isolation, PyPI trusted publishing.
