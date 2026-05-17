# OpsBridge

SSH into a server and land in an AI sysadmin TUI instead of a shell.

```
$ ssh agent@my-server
                                                                ┌──────────┐
                                                                │OpsBridge │
                                                                └──────────┘
> install nginx
[search] "nginx install ubuntu" → 5 results
[visit] https://nginx.org/en/docs/install.html
$ apt-get update && apt-get install -y nginx
…
▶ Run `sudo apt install nginx -y`? Downloads ~150 MB.
    ( ) yes
    (•) no  ← default
```

OpsBridge is a per-session agent daemon. `sshd` matches the `agent` user
and replaces their login shell with a smolagents `CodeAgent` running in a
full-screen [textual](https://textual.textualize.io/) TUI. The agent uses
an LLM (OpenAI or Anthropic, configurable) and seven tools — `read`,
`write`, `bash`, `search`, `visit`, `ask`, `remember` — to translate
English requests into shell actions on the host.

## Install

The one-liner installer creates the `agent` system user, NOPASSWD sudoers
entry, sshd `ForceCommand` snippet, and prompts for an LLM API key and an
authorized SSH pubkey.

```bash
curl -fsSL https://raw.githubusercontent.com/cheney-yan/OpsBridge/main/install.sh | bash
```

> **Note**: no leading `sudo`. The installer handles privilege escalation
> itself so its `sudo` invocation is connected to your actual terminal —
> avoiding the broken-pty trap where `curl | sudo bash` silently fails on
> interactive prompts.

For unattended installs (CI / Ansible), set env vars to skip prompts:

```bash
OPSBRIDGE_PROVIDER=openai \
OPSBRIDGE_MODEL=gpt-4.1-mini \
OPSBRIDGE_API_KEY=... \
OPSBRIDGE_PUBKEY="ssh-ed25519 AAAA... me@laptop" \
curl -fsSL .../install.sh | sudo bash
```

Supported platforms: Linux + systemd, macOS + launchd. BSD deferred.

## Use

```bash
ssh agent@your-server
```

You'll land in the TUI (four regions: scrollable top region, middle for
the final answer or a confirmation form, status bar, input line). Type
requests in English. Ctrl-D leaves. Destructive commands trigger a form
prompt — Y/N + Enter.

## Try it locally (no install)

If you just want to kick the tyres against the test LLM proxy without
running the installer:

```bash
git clone https://github.com/cheney-yan/OpsBridge.git
cd OpsBridge
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e '.[dev]'
# Point at your own LLM proxy or vendor endpoint and key.
cat > .env <<EOF
AGENT_TEST_LLM_BASE_URL=https://your-openai-compatible-proxy.example.com
AGENT_TEST_LLM_KEY=your-api-key-here
EOF
.venv/bin/python run-demo.py
```

`run-demo.py` runs the full TUI against the configured proxy. State
lives in `/tmp/opsbridge-demo/` (preferences, audit JSONL).

For a quick non-TUI smoke (e.g. on CI):

```bash
.venv/bin/python run-demo.py --one-shot "what's my pwd?"
```

## Tools

| Tool | Why |
|---|---|
| `read` | Paginated, line-numbered file reads. |
| `write` | Atomic file write. Parent dir must exist. |
| `bash` | `bash -lc` (login shell, sources `.profile`). 60s default timeout. Live output streams to the top region. |
| `search` | Web search via smolagents `WebSearchTool`. No API key. |
| `visit` | Fetch one URL via [Jina Reader](https://jina.ai/reader) (handles JS / bot-detection server-side). Optional API key for higher RPM. |
| `ask` | Structured confirmation form (radio buttons). Used before any destructive action — replaces fragile `[y/N]` text matching. |
| `remember` | Sole audit chokepoint for `/etc/opsbridge/agent/preferences.md`. Enforces 50-line / 4 KB cap and rejects duplicates. |

## Admin CLI

After install, an `opsbridge` console script is symlinked into
`/usr/local/bin`:

```bash
opsbridge install            # idempotent; re-run after `git pull`
opsbridge install --interactive   # prompts for provider/model/key/pubkey
opsbridge config             # rotate API key / switch provider
opsbridge doctor             # verify install integrity
opsbridge doctor --check-api          # also ping the LLM
opsbridge doctor --system-prompt      # validate override anchors
opsbridge enable / disable   # toggle the sshd ForceCommand
opsbridge audit preferences  # canonical + suspicious timelines
opsbridge uninstall          # remove user, sudoers, sshd snippet
```

## Per-host configuration

The agent reads two files at startup:

- `/etc/opsbridge/agent/config.toml` — provider, model, optional
  `base_url`, optional `[visit]` block (`jina_api_key`, `timeout_sec`,
  `max_bytes`).
- `/etc/opsbridge/agent/api.key` — LLM API token. Mode 0400.

The default system prompt ships in the venv at
`opsbridge/agent/prompts/system.md`. To override per-host, write to
`/etc/opsbridge/agent/system_prompt.md`; the override is loaded only if
it contains every required safety anchor (`## Hard rules`,
`ask before destructive`, `preferences file is special`,
`never fabricate tool output`, `NOPASSWD sudo`). `opsbridge doctor
--system-prompt` verifies this.

Operator preferences (`/etc/opsbridge/agent/preferences.md`) persist
across sessions. Mutate only via the `remember` tool — `write` / `bash`
against this path is treated as a security bypass and surfaces in
`opsbridge audit preferences --suspicious`.

## Security model

- **Trust boundary:** an SSH key authorized for the `agent` user is
  equivalent to root on the host. NOPASSWD sudo is intentional — there's
  no second authorization layer inside the TUI.
- **Soft guardrail:** the system prompt instructs the LLM to call the
  `ask` tool before any destructive or shared-state-affecting command.
  The `ask_pre_exec` event in the audit log captures the LLM's intent
  before the operator's response.
- **Audit log:** one JSONL file per session at
  `/var/log/opsbridge/agent/<session-id>.jsonl`. Includes
  `session_start`, `system_prompt_source` (with sha256),
  `bash_pre_exec`, `search_pre_exec`, `visit_pre_exec`, `ask_pre_exec`,
  `tool_call`, `turn_start`/`turn_end`, `session_end`.
- **ANSI sanitization:** all tool output passes through a one-shot
  sanitizer that keeps SGR (colors) and strips CSI cursor / screen
  control / OSC / ESC singles — defeats prompt-injection attempts at
  terminal title hijacking or rolling-window smashing.

## Project status

| Phase | Scope | Status |
|---|---|---|
| 1 | Four tools, REPL TUI, token budget, admin CLI, sudoers/sshd wiring | ✅ |
| 2 | Full-screen textual TUI, `search`/`visit`/`ask` tools, prompt externalization, `install.sh`, macOS support | ✅ — 111 tests pass |

See [PRD.md](PRD.md) (Phase 1 spec) and [PRD-phase2.md](PRD-phase2.md)
(Phase 2 spec) for the full design rationale. [CLAUDE.md](CLAUDE.md)
holds the locked-in decisions that should be re-litigated only with
explicit user buy-in.

## Uninstall

```bash
sudo opsbridge uninstall
```

Removes the `agent` user, `/opt/opsbridge/`, `/etc/opsbridge/`, sudoers
file, sshd snippet, and the `opsbridge` symlink. Logs at
`/var/log/opsbridge/agent/` are kept — remove them separately if
desired.


## License

MIT. See [PRD.md](PRD.md) for design lineage and rationale.
