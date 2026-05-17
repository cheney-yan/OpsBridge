# PRD — OpsBridge

> **OpsBridge** is an SSH-login agent daemon. The umbrella project name is
> `opsbridge`; v1 ships a single component — the `agent` — which is what
> this PRD covers. Future components (e.g. metrics collectors, web
> dashboards) would live as siblings under the same umbrella.

## 1. Vision

A Linux host where SSHing into a dedicated user (`agent`, default — name is
configurable at install time) drops you directly into a minimal agent TUI
instead of a shell. The agent uses an LLM (OpenAI or Anthropic, configurable)
plus four tools — `read`, `write`, `bash`, `remember` — to translate
natural-language requests into the right shell commands on the host, and
to retain a small, operator-curated preference file across sessions.

The product is the smallest possible "talk to my server in English" surface:
no web UI, no auth tokens to manage on the client side, no custom protocol.
Just `ssh agent@host`.

**Audience.** Self-hosting operators — solo DevOps and small teams running
their own bare-metal / VPS infrastructure, who want a shared LLM admin
reachable as a first-class SSH identity. Not a smarter local terminal
(that space is well-covered by `aichat`, `aish`, `ai-shell`, etc.). Not
for managed / locked-down environments where SSH is not the operating
surface.

## 2. User flow (single session)

1. Operator runs `ssh agent@host` from their laptop.
2. `sshd` authenticates them via public key (password auth disabled).
3. `sshd`'s `ForceCommand` launches `agent` — the user's shell is never
   spawned.
4. `agent` loads `/etc/opsbridge/agent/preferences.md` into its system prompt and
   shows a prompt.
5. Operator types a request in English.
6. The smolagents `CodeAgent` loop runs: the model proposes Python that
   calls `read` / `write` / `bash` / `remember`; the TUI executes it;
   output is fed back; repeat until the agent answers or stops.
7. Operator types another request, or `Ctrl-D` to disconnect.

## 3. Architecture

```
        ┌──────────────────────────────────────────────────────┐
        │ Linux host                                           │
        │                                                      │
        │  sshd ── pubkey auth ──► ForceCommand agent          │
        │                                │                     │
        │                                ▼                     │
        │                         ┌─────────────┐              │
        │                         │    agent    │  (Python)    │
        │                         │             │              │
        │                         │  smolagents │              │
        │                         │  CodeAgent  │──► LLM API   │
        │                         │             │  (LiteLLM)   │
        │                         │  tools:     │              │
        │                         │   read      │              │
        │                         │   write     │              │
        │                         │   bash      │              │
        │                         │   remember  │              │
        │                         └─────────────┘              │
        │                                                      │
        └──────────────────────────────────────────────────────┘
```

One per-session binary, plus an out-of-band admin CLI:

- **`agent`** — Python entrypoint launched by sshd's `ForceCommand` per SSH
  session. Wraps a smolagents `CodeAgent` with the four tools. Reads stdin,
  writes stdout. Exits when the session ends. No long-running daemon.
- **`opsbridge`** — admin CLI (separate console script, same venv). Not in
  the request path. Used by the host operator for install, health checks,
  audit, and toggling access. See §7.

**Python runtime isolation.** Both binaries run from a venv at
`/opt/opsbridge/agent/.venv/`, backed by a standalone Python interpreter under
`/opt/opsbridge/agent/python/` that `uv` fetched at install time. This makes the
agent immune to system `python3` upgrades, missing-Python hosts, and
distro version drift. The interpreter and venv are owned by root; the
`agent` user has read+execute but not write, so the model cannot
`pip install` arbitrary packages from inside `bash` without sudo
(and any such attempt is conspicuous in the JSONL trace).

**Token budget management.** LiteLLM reports per-call token usage. The
agent loop tracks cumulative session tokens against the model's context
limit and reacts in three bands:

| Usage | Behavior |
|---|---|
| ≥ 80% | Print a single-line banner to the TUI (`[context: 80% used]`) — informational, no flow change. |
| ≥ 90% | Auto-compress: ask the model to summarize the oldest N steps as a single observation; replace those steps in `agent.memory.steps` with the summary. Logged as a `context_compress` event in JSONL. |
| ≥ 95% | Hard stop: refuse new operator turns with `[context exhausted; please disconnect and reconnect for a fresh session]`. |

The compress step is itself an LLM call (cheap one — short summarization),
so the soft threshold leaves headroom for it.

## 4. Tools

Four tools in two categories — three for IO/execution, one structural.

| Tool | Signature | Purpose |
|------|-----------|---------|
| `read` | `read(path: str, offset: int = 0, limit: int = 2000) -> str` | Read a file. Line-numbered output to make subsequent `write` calls easier for the model. Output is ANSI-sanitized (see §5). |
| `write` | `write(path: str, content: str) -> str` | Write/overwrite a file. Creates parent dirs only if explicitly requested. |
| `bash` | `bash(command: str, timeout_sec: int = 60) -> str` | Run a shell command via `bash -lc` (login shell — sources `/home/agent/.profile`). Captures full stdout+stderr and returns it to the LLM. Output (both live stream and captured return) is ANSI-sanitized (see §5). To the operator's TUI, the live stream renders as a **rolling 5-line window** so long-running output doesn't drown the screen; the final 5 lines remain frozen on-screen when the command exits. Non-TTY fallback: plain append. Default cwd: `/home/agent`. On timeout, the subprocess is killed (SIGTERM then SIGKILL after 2 s) and the captured output so far is returned with a `[timeout after Ns]` suffix. Cannot be interrupted by the operator mid-run in v1 (see §11). |
| `remember` | `remember(action: Literal["add", "remove"], content: str) -> str` | The sole sanctioned path for mutating `/etc/opsbridge/agent/preferences.md`. Enforces bullet format, size caps (50 lines / 4 KB), no exact duplicates, no removing nonexistent entries. **If the preferences file is missing when `add` is called, the tool creates it via `sudo` (`0640 root:agent`) with an empty stub, then writes the bullet.** `remove` against a missing file is a silent no-op. Emits a structured `preferences_mutation` event to the session JSONL. All judgment — conflict detection, risk assessment, conciseness, pruning — is the LLM's responsibility per system prompt; this function stays dumb. |

No `edit`, no `grep`, no `find` — the model uses `bash` for those.
`remember` is the **one** structural exception, justified because it is
the audit chokepoint for preferences.

## 5. Security model

- **Auth:** SSH public key only. `PasswordAuthentication no`,
  `ChallengeResponseAuthentication no` for the `agent` user.
- **Privileges:** the `agent` user has `NOPASSWD: ALL` sudo, installed at
  `/etc/sudoers.d/opsbridge-agent` (mode 0440) by `opsbridge install`. The model
  prefixes commands with `sudo` when it needs root. This is deliberate —
  see "Trust boundary".
- **Trust boundary:** possession of an SSH key authorized for `agent` is
  equivalent to root on this host. Password-gated sudo would not change
  this (a key holder can read sudoers, install a keylogger, or wait to
  replay a captured prompt). Distribute and revoke keys accordingly.
  There is no second authorization layer inside the TUI.
- **Confirmation by default (soft layer):** the system prompt instructs
  the model to **ask the operator before** any destructive or
  shared-state-affecting action — `rm -rf`, package install/remove,
  service restart, truncating files, network/firewall changes, killing
  processes it didn't start, anything touching other users' data. Not a
  sandbox; a footgun reducer.
- **Preferences as a high-value injection target:** because
  `/etc/opsbridge/agent/preferences.md` is loaded into every future session's
  system prompt, it is a prized target for prompt-injection (e.g. tool
  output containing instructions to write a backdoor preference).
  Defenses:
  - **File permissions:** `0640 root:agent` — agent can read directly
    but must go through `sudo` to write, making any bypass visible as
    a `sudo` invocation in the bash JSONL trace.
  - **`remember` as chokepoint:** system prompt forbids `write`/`bash`
    against this path and requires explicit operator `yes` before
    every `remember` call. The LLM is also instructed to refuse
    preference content that weakens existing safety rules.
  - **Audit:** `opsbridge audit preferences` shows two timelines —
    **canonical** (all `remember` events with diffs) and
    **suspicious** (any `bash`/`write` events whose arguments mention
    the preferences path). The suspicious view catches bypass attempts
    after the fact.
- **API key:** read from `/etc/opsbridge/agent/api.key` (mode 0400, owned by
  `agent`). Never passed via env in the SSH session (sshd strips most
  env anyway).
- **Logging:** every tool call (tool name + args + truncated result) goes
  to `/var/log/opsbridge/agent/<session-id>.jsonl`. Logs are owned by root,
  readable by `agent` only via group.
- **Operator attribution:** all operators share the `agent` Unix
  identity; sshd logs the public-key fingerprint that authenticated
  each session, and the session JSONL records that fingerprint at
  startup.
- **Credentials live in `/home/agent/`:** file-based creds
  (`~/.aws/credentials`, `~/.kube/config`, `~/.ssh/id_*`, etc.) and
  env-var creds in `~/.profile` (sourced by every `bash` invocation).
  **All credentials are shared across operators** authorized for the
  `agent` user — there's no per-operator partitioning in v1. Operators
  who need a credential pull it in on demand by asking the agent to
  `sudo cp` it from another user's home; this triggers the standard
  confirmation flow (the agent shows the proposed `sudo cp` + `chown`
  command and waits for "yes"). Treat any credential dropped into
  `/home/agent/` as accessible to every authorized key until removed.
- **Terminal output sanitization:** all tool output destined for the
  TUI **or** the LLM — `read` return values, `bash`'s captured return,
  and `bash`'s live byte stream feeding the rolling window — passes
  through one sanitizer pass:
  - **Kept:** CSI SGR sequences (`\033[<n>m`) — colors, bold, italic.
    These render naturally in the operator's terminal and are harmless
    bytes to the LLM.
  - **Stripped:** CSI cursor / screen control (`\033[H`, `\033[J`,
    `\033[K`, `\033[A`–`\033[D`, `\033[?1049h`, …), OSC sequences
    (`\033]…\007` — terminal title hijacking is the obvious attack),
    and ESC singles (save/restore cursor, charset switch).

  Rationale: a malicious file content (e.g. `printf '\033]0;OWNED\007'
  > /tmp/log.txt`) could otherwise change the operator's terminal
  title, smash the rolling window, or repaint over the agent prompt.
  A single regex pass keeps the implementation tiny. The same string
  goes to TUI and LLM — splitting the pipeline buys little for v1.

## 6. Configuration

### Model and provider

Two files under `/etc/opsbridge/agent/`:

```toml
# /etc/opsbridge/agent/config.toml  (mode 0440, root:agent)
provider = "anthropic"          # "anthropic" | "openai"
model    = "claude-sonnet-4-5"  # provider-specific model id
base_url = ""                   # empty = the provider's official endpoint
```

```
/etc/opsbridge/agent/api.key   (mode 0400, agent:agent)   — single line, the API token
```

Both providers go through smolagents' `LiteLLMModel`. `provider` prefixes
the model id (`anthropic/...` or `openai/...`); `base_url` is passed as
`api_base`. This covers Azure OpenAI, Bedrock proxies, local
OpenAI-compatible servers (vLLM, ollama, Together, Groq, …), and
self-hosted gateways.

Parsed with stdlib `tomllib` (Python 3.11+) — no extra dependency.

### Credentials

Two storage paths, both under `/home/agent/`:

| Kind | Where | How agent picks it up |
|---|---|---|
| File-based (cloud CLI configs, kube configs, SSH keys, gh tokens, …) | `/home/agent/.aws/`, `/home/agent/.kube/`, `/home/agent/.config/`, `/home/agent/.ssh/`, etc. (mode `0600 agent:agent`) | Standard tool conventions — `bash` runs with `cwd=/home/agent`, so `~` resolves correctly. |
| Env-var-based (`GITHUB_TOKEN`, `DATABASE_URL`, …) | `/home/agent/.profile` (mode `0600 agent:agent`) | `bash` tool invokes `bash -lc "{cmd}"` — login shell sources `.profile` for every command. |

**Provisioning path (recommended, simplest):** the admin doesn't pre-populate
anything at install time. When an operator needs a credential, they ask
the agent to pull it from another user's home, e.g.:

```
> I need alice's AWS creds for an S3 task
agent: I'll run:
       sudo cp /home/alice/.aws/credentials /home/agent/.aws/credentials
       sudo chown -R agent:agent /home/agent/.aws/
       Confirm? (yes/no)
> yes
agent: done. aws CLI is ready.
```

This works out of the box because the `agent` user has NOPASSWD sudo and
the system prompt classifies cross-home file copies as
shared-state-affecting actions (forces the confirmation flow).

**Alternative (centralized):** the admin pre-populates `/home/agent/` as
root with whatever credentials all operators should share. Useful for
service-account style setups where credentials don't belong to any
individual.

**The agent must not modify credential paths on its own.** System prompt
forbids `write` / `bash` mutation of `~/.aws/`, `~/.kube/`, `~/.ssh/`,
`~/.profile`, `~/.config/` unless the operator explicitly initiates the
change (paste-a-new-key, copy-from-user, rotate-token). Read-only access
for inspection is fine.

### Operator preferences

```
/etc/opsbridge/agent/preferences.md   (mode 0640, root:agent)
```

A small markdown file (max 50 lines / 4 KB) of bullet-listed conventions
that apply to this host. Loaded into the agent's system prompt at the
start of every session. Example:

```markdown
# Operator preferences for $(hostname)

- Service management uses systemctl; init.d is deprecated.
- Dev server lives on port 3000.
- Don't touch /srv/legacy/ — frozen archive.
```

Mutated only via the `remember` tool (see §4). The LLM is responsible for
keeping it concise, deduplicated, and free of self-undermining entries;
size caps are enforced in code as a final guardrail.

**Size discipline** (enforced via system prompt):

| File state | LLM behavior |
|---|---|
| ≤ 20 lines | Add normally after the operator-confirmation flow |
| 20–40 lines | Proactively propose consolidation or removal of stale entries |
| > 40 lines | Strongly recommend pruning before adding |
| > 50 lines or > 4 KB | `remember` function refuses with an error |

## 7. Admin CLI (`opsbridge`)

Console script installed alongside `agent` in `/opt/opsbridge/agent/.venv`, symlinked
into `/usr/local/bin/opsbridge`. All subcommands require root.

| Subcommand | Behavior |
|------------|----------|
| `opsbridge install` | **Idempotent and update-aware.** On a fresh host: creates the `agent` user, fetches a `uv`-managed Python into `/opt/opsbridge/agent/python/`, builds `/opt/opsbridge/agent/.venv`, writes `/etc/sudoers.d/opsbridge-agent` (NOPASSWD ALL) and `/etc/ssh/sshd_config.d/50-opsbridge-agent.conf`, prompts for provider / model / base_url / API key, reloads sshd. **If already installed:** refreshes the venv (re-resolves deps, fetches a newer pinned Python if the pin moved), restores any missing files (sudoers, sshd snippet, log dir), and leaves existing config / preferences / `authorized_keys` untouched. Pass `--reconfigure` to re-run the LLM prompts as well. `--skip-model-config` for unattended fresh installs. `--use-system-python` to use `/usr/bin/python3` instead of the uv-managed interpreter (requires 3.11+). There is no separate `upgrade` command — re-run `install`. |
| `opsbridge config` | Re-run the model-configuration prompts only (rotate key, switch provider, change base_url). |
| `opsbridge doctor` | Verify user state, venv integrity, file paths and permissions, sudoers entry, sshd config syntax, `authorized_keys` presence, log dir writability. `--check-api` additionally pings the configured LLM. Exit `0` ok, `1` error, `2` warning only. |
| `opsbridge enable` | Restore the sshd `ForceCommand` snippet and reload sshd. |
| `opsbridge disable` | Move the sshd snippet aside and reload sshd. Existing sessions keep running; new logins refused. |
| `opsbridge audit preferences` | Print two timelines from `/var/log/opsbridge/agent/*.jsonl`: **canonical** (every `remember` event with diff) and **suspicious** (any `bash` or `write` event whose arguments contain the preferences path). |
| `opsbridge uninstall` | Remove the `agent` user, `/opt/opsbridge/`, `/etc/opsbridge/`, `/var/log/opsbridge/`, `/etc/sudoers.d/opsbridge-agent`, and the sshd snippet. Asks for explicit confirmation. |

## 8. Lifecycle journeys

### 8.1 Admin: first-time setup

Run on a fresh Linux host as root:

```bash
curl -fsSL <release-tarball-url> | tar -xz -C /opt/opsbridge-src
sudo /opt/opsbridge-src/bootstrap.sh   # installs uv if absent, builds venv,
                                   # symlinks opsbridge → /usr/local/bin
sudo opsbridge install              # interactive
```

Interactive prompts in `opsbridge install`:

```
[1/5] Create system user 'agent'... ok
[2/5] Fetch Python 3.12 via uv to /opt/opsbridge/agent/python ... ok
[3/5] Install project to /opt/opsbridge/agent/.venv ... ok
[4/5] Configure LLM:
      Provider? [anthropic/openai]: openai
      Model name [gpt-4o]: 
      Custom base URL? (empty = official) []: 
      Paste API key (hidden): ********
[5/5] Wire sudoers + sshd ... ok

Done. Add operator pubkeys to /home/agent/.ssh/authorized_keys.
```

Then the admin appends each operator's pubkey:

```bash
echo "ssh-ed25519 AAAA... alice@laptop" | sudo tee -a /home/agent/.ssh/authorized_keys
```

Verify end-to-end:

```bash
sudo opsbridge doctor --check-api
```

### 8.2 Operator: first use

```
$ ssh agent@host
> Show me the top 5 disk consumers under /var
[agent runs `du`, presents results]
> remember that the dev server lives on port 3000
[agent shows proposed diff, asks for "yes"]
> yes
[remember tool fires; future sessions inherit the preference]
> ^D
```

### 8.3 Admin: routine operations

| Need | Command |
|---|---|
| Health check (CI-friendly) | `opsbridge doctor` (add `--check-api` to ping the LLM) |
| Review preferences history | `opsbridge audit preferences` |
| Maintenance window | `opsbridge disable` … `opsbridge enable` |
| Rotate API key or switch provider | `opsbridge config` |
| Refresh venv / Python after a release | `opsbridge install` (idempotent — refreshes deps; config and prefs preserved) |
| Add operator | append pubkey to `/home/agent/.ssh/authorized_keys` |
| Revoke operator | remove their pubkey line |

### 8.4 Admin: decommission

```bash
sudo opsbridge uninstall
```

Removes the `agent` user, `/opt/opsbridge/`, `/etc/opsbridge/`,
`/etc/sudoers.d/opsbridge-agent`, the sshd snippet, and
`/usr/local/bin/opsbridge`. JSONL logs in `/var/log/opsbridge/agent/`
are kept for audit; remove them separately if desired.

## 9. Non-goals (v1)

- Multi-user concurrent sessions sharing context — each SSH session has
  its own conversation history. The only persistent cross-session state
  is `/etc/opsbridge/agent/preferences.md`.
- Per-operator preferences namespaces — preferences are shared across
  every operator authorized for the `agent` user. Per-key partitioning
  is a possible v2.
- A web UI, REST API, or any non-SSH transport.
- Tool sandboxing beyond Unix file permissions (no seccomp, no nsjail).
- Streaming token-by-token output to the TUI — line-buffered is fine.
- Warm-pool / long-running model-client daemon — out of scope until
  measured cold-start latency makes it worth the complexity.
- **Per-user CLI install mode** — running the agent under an unprivileged
  user's home without a dedicated `agent` system user, sudoers entry, or
  sshd ForceCommand. Explicitly out of scope: the "smarter local
  terminal" niche is already saturated by `aichat`, `aish`,
  `ai-shell`, and friends. Our differentiator is the multi-operator
  service-account model behind a dedicated SSH identity; collapsing back
  into per-user mode erases that distinction. If you find yourself
  wanting to relax a system-mode constraint (sudo, root install, sshd
  config), check first whether the actual user scenario needs it — not
  whether it's technically possible.
- Risk-grading / regex-based command screening inside the `bash` tool —
  the trust boundary is "SSH key for `agent` = root on this host"
  (§5). Layering a regex matcher on top of an explicit-root account is
  a false sense of security and contradicts the no-allowlist rule.

## 10. Layout

Source repo:

```
smolagent/
├── PRD.md                    # this file
├── CLAUDE.md                 # guidance for Claude Code working in this repo
├── pyproject.toml            # uv project; pins smolagents, litellm, openai, anthropic
├── bootstrap.sh              # one-shot: install uv, build venv, symlink opsbridge
├── src/
│   └── opsbridge/
│       ├── __init__.py
│       ├── admin.py              # `opsbridge` umbrella CLI (install/doctor/…)
│       └── agent/
│           ├── __init__.py
│           ├── __main__.py       # `agent` entrypoint — ForceCommand target
│           ├── core.py           # CodeAgent assembly, system prompt
│           ├── tools.py          # read, write, bash, remember
│           ├── model.py          # provider → LiteLLMModel factory
│           └── logging.py        # JSONL session logging
├── deploy/
│   ├── sshd_config.snippet   # Match User agent + ForceCommand
│   └── sudoers.snippet       # agent ALL=(ALL) NOPASSWD:ALL
└── tests/
    └── test_tools.py
```

Runtime layout on a host (post-install):

```
/opt/opsbridge/agent/
├── python/                   # standalone Python interpreter (uv-managed)
└── .venv/                    # project venv; bin/agent, bin/opsbridge live here

/etc/opsbridge/agent/
├── config.toml               # provider, model, base_url
├── api.key                   # LLM API token
└── preferences.md            # operator preferences

/etc/sudoers.d/opsbridge-agent                       # NOPASSWD: ALL
/etc/ssh/sshd_config.d/50-opsbridge-agent.conf       # ForceCommand
/var/log/opsbridge/agent/<session-id>.jsonl
/home/agent/.ssh/authorized_keys
/usr/local/bin/opsbridge                    # symlink → /opt/opsbridge/agent/.venv/bin/opsbridge
```

`pyproject.toml` declares two console scripts:
`agent = "opsbridge.agent.__main__:main"` and `opsbridge = "opsbridge.admin:main"`.

## 11. Open questions

- **Interruptible long-running commands (v2).** v1's `bash` tool cannot
  be cancelled mid-run — the operator must wait for `timeout_sec` or
  disconnect (which kills the whole session and the subprocess with
  it). Proper support requires process-group management, a clean way
  to surface the cancellation to the LLM (so it doesn't retry blindly),
  and a `^C`-binding in the TUI. Punted from v1.
- **Per-operator credential namespacing (v2).** Currently all operators
  authorized for `agent` share the same `/home/agent/` credential set.
  A future enhancement could partition by SSH key fingerprint
  (`/home/agent/creds/<fp>/`) and bind-mount or symlink the right
  subdir per session. Adds complexity; not worth it until the shared
  blast radius hurts someone.
- **Multi-byte ANSI split across read boundaries.** The output
  sanitizer (§5) assumes complete escape sequences inside each
  read-from-pipe chunk. If an escape gets split across a boundary
  (rare; escape sequences are usually < 20 bytes), the prefix can
  leak as visible garbage and the suffix as stripped-but-orphaned
  bytes. Mitigation in code: hold a small carry-over buffer when a
  chunk ends mid-escape. Listed as a known minor display artifact,
  not a v1 blocker.
