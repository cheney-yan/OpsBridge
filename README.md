# OpsBridge

SSH into a server and land in an AI sysadmin session instead of a shell.

```
$ ssh agent@my-server

  ┌─ pi ──────────────────────────────────────────────────────────────┐
  │ > install nginx                                                    │
  │                                                                    │
  │ I'll install nginx using apt.                                      │
  │ $ sudo apt-get update && sudo apt-get install -y nginx             │
  │ ...                                                                │
  └────────────────────────────────────────────────────────────────────┘
```

OpsBridge wires SSH into [pi.dev](https://github.com/badlogic/pi-mono) — a
fully-featured terminal AI agent. `sshd` matches the `agent` user and runs a
generated launcher script instead of a shell:

```
SSH → ForceCommand → /usr/local/bin/opsbridge-agent
                           └── exec pi --model "anthropic/claude-opus-4-7"
                                  ↑ reads ~/.pi/agent/SYSTEM.md (safety rules)
                                  ↑ reads ~/.pi/agent/auth.json (API key)
```

Pi.dev handles the TUI, tools (read / write / bash / edit / grep / find / ls),
and LLM calls. OpsBridge's role is SSH glue, system prompt injection, and the
admin CLI.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/cheney-yan/OpsBridge/main/install.sh | bash
```

> **No leading `sudo`** — the installer handles privilege escalation itself so
> its `sudo` attaches to your real terminal. `curl | sudo bash` silently breaks
> interactive prompts.

Pin a specific version:

```bash
curl -fsSL https://raw.githubusercontent.com/cheney-yan/OpsBridge/main/install.sh \
  | bash -s -- -v v0.4.6
```

Uninstall:

```bash
curl -fsSL https://raw.githubusercontent.com/cheney-yan/OpsBridge/main/install.sh \
  | bash -s -- uninstall
```

Unattended / CI:

```bash
OPSBRIDGE_PROVIDER=anthropic \
OPSBRIDGE_MODEL=claude-opus-4-7 \
OPSBRIDGE_API_KEY=... \
OPSBRIDGE_PUBKEY="ssh-ed25519 AAAA... me@laptop" \
curl -fsSL https://raw.githubusercontent.com/cheney-yan/OpsBridge/main/install.sh | bash
```

Supported: Linux + systemd, macOS + launchd.

## Use

```bash
ssh agent@your-server
```

Pi.dev's TUI launches. Type requests in English. Use Ctrl-C to interrupt a
running command, Ctrl-D to exit.

## Tools

Pi.dev's built-in tools — no custom Python required:

| Tool | What it does |
|---|---|
| `read` | Read a file |
| `write` | Write / overwrite a file |
| `bash` | Run shell commands with live output |
| `edit` | Apply a targeted diff to a file |
| `grep` | Search file contents |
| `find` | Find files by name or pattern |
| `ls` | List directory contents |

## Admin CLI

```bash
opsbridge install            # idempotent; re-run after git pull
opsbridge install --reconfigure   # re-prompt for provider/model/key
opsbridge config             # rotate API key / switch provider or model
opsbridge doctor             # verify install integrity
opsbridge enable / disable   # toggle the sshd ForceCommand snippet
opsbridge uninstall          # remove user, sudoers, sshd snippet
```

## Configuration

`opsbridge install` / `opsbridge config` write three pi.dev config files under
`~agent/.pi/agent/` and the opsbridge config under `/etc/opsbridge/agent/`:

| File | Purpose |
|---|---|
| `/etc/opsbridge/agent/config.toml` | provider, default model, optional base\_url, model list with context windows |
| `/etc/opsbridge/agent/api.key` | LLM API token (mode 0400, read by auth.json shell command) |
| `~agent/.pi/agent/auth.json` | Pi.dev credential — `"!cat /etc/opsbridge/agent/api.key"` |
| `~agent/.pi/agent/models.json` | Pi.dev model metadata (context window, max tokens, optional base URL) |
| `~agent/.pi/agent/SYSTEM.md` | Safety rules injected as system prompt every session |

**Custom provider / Azure / Bedrock:** set `base_url` during `opsbridge config`
and the installer writes a `models.json` `baseUrl` override for pi.dev.

## Security model

- **Trust boundary:** an SSH key authorized for the `agent` user is equivalent
  to root on the host. NOPASSWD sudo is intentional — there is no second
  authorization layer inside the session.
- **Soft guardrail:** the system prompt (`~agent/.pi/agent/SYSTEM.md`) instructs
  the model to confirm before destructive commands (`rm`, `drop`, `kill`,
  overwrite). This is the only confirmation mechanism.
- **API key at rest:** stored mode 0400 at `/etc/opsbridge/agent/api.key`,
  read at runtime by pi.dev via the `!cat` shell command in `auth.json`. Never
  exported into the process environment.

## Uninstall

```bash
sudo opsbridge uninstall
```

Removes the `agent` user, `/opt/opsbridge/`, `/etc/opsbridge/`, sudoers file,
sshd snippet, launcher script, and the `opsbridge` symlink.

## License

MIT.
