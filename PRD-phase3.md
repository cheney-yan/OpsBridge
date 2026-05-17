# PRD — Phase 3 (open issues from Phase 2 dogfooding)

Recorded from an OrbStack VM dogfooding session, 2026-05-17.
None of these are fixed yet; this file captures the problem statement,
root cause, and a sketch of the right shape, so Phase 3 can pick them
up in priority order.

> The thread that surfaced these was: SSH into agent, ask it to install
> OpenClaw, agent ran `curl -fsSL https://openclaw.ai/install.sh | bash`
> with `timeout_sec=300`. The TUI fell silent for ~5 minutes. Operator
> typed "is it finished?" — it went into a queue invisibly. Operator
> got nervous. Maintainer SSH'd in from the side, killed the curl|bash
> subprocess. LLM saw a non-zero exit and **retried with `timeout_sec=600`**.
> Maintainer killed the whole agent, ending the session. Half an apt
> transaction (and possibly half a Node install) was left on the host.

## 1. Pipe block-buffering makes the TUI look frozen

### Symptom

Operator runs an `apt-get install` or `curl | bash` that produces output
slowly. The TUI shows `$ <command>` and then **complete silence** for
seconds-to-minutes. Status bar says `thinking` but nothing scrolls. The
operator cannot tell whether:

- the agent is busy thinking (LLM call in flight),
- a tool call is running and producing output we just haven't received,
- the subprocess is genuinely hung waiting on stdin / network / lock,
- the agent crashed and the session is dead.

### Root cause

`tools.py:tool_bash` runs the child via `subprocess.PIPE`:

```python
proc = subprocess.Popen(
    ["bash", "-lc", command],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    ...
)
```

Children using C stdio (almost everything: `apt`, `curl`, `npm`, install
scripts using `echo`) detect that stdout is not a TTY and switch from
line-buffering to **4 KB block-buffering**. Output appears to us only
when the child's buffer fills or the child exits. For an install script
that prints `Installing Node 24...\n` and then spends 90 seconds on a
download, we see nothing for 90 seconds.

### Sketch of the right shape

Allocate a PTY for the subprocess so the child sees stdout as a TTY and
keeps line-buffering:

```python
import pty
master_fd, slave_fd = pty.openpty()
proc = subprocess.Popen(
    ["bash", "-lc", command],
    stdin=subprocess.DEVNULL,   # see §3
    stdout=slave_fd,
    stderr=slave_fd,
    start_new_session=True,
    close_fds=True,
)
os.close(slave_fd)
# read from master_fd with a select-loop; handle OSError(EIO) as EOF on Linux
```

Caveats:

- Read loop must handle `EIO` (Linux) / EOF differently than `read()` on a pipe.
- Window-size: the PTY defaults to 80×24. Some progress-bar-style installers
  reflow to the window; consider `termios.TIOCSWINSZ` at allocation time to
  match the operator's actual terminal.
- ANSI sanitizer (`tools.py:sanitize`) keeps SGR and strips cursor controls
  — that already handles installers that paint progress bars.

## 2. No "alive" signal during waits — the operator-anxiety axis

### Symptom

Anywhere the agent does work that takes more than ~2 seconds, the
operator sees the same thing: status bar reads `thinking`, the rest of
the TUI is motionless. There's no signal that distinguishes:

- "the LLM is composing a 4000-token plan, give it 8 more seconds",
- "bash is running `apt install`, currently at the dpkg unpack step",
- "the visit tool is fetching a large page, downloaded 12 KB / ~50 KB",
- "the LLM call hung mid-stream, you should give up",
- "the bash subprocess hung on stdin, you should cancel".

This is **the** dominant complaint from dogfooding: operators don't
trust the system because they can't tell the difference between
"working hard" and "frozen". Even with §1's PTY fix removing the
block-buffering bug, commands that genuinely produce no output for 90
seconds (downloads, slow apt resolvers) leave the operator staring at
nothing.

### Where heartbeats are missing

Six places where the agent is busy and currently shows nothing
informative:

1. **LLM call**: between `agent.run()` invocation and the first tool
   call landing in `top_log`. Could be 5–30s. Status says `thinking`
   but no elapsed time, no token-streaming preview.
2. **Bash subprocess**: after `$ <command>` echoes, before the first
   output line lands. Could be the entire duration for a download.
3. **Visit tool**: after `[visit] <url>` echoes, before `[visit] ←
   <bytes>` lands. Jina sometimes takes 5–10s on a cold page.
4. **Search tool**: after `[search] <query>`, before `[search] → <n>
   results`. Usually fast but not always.
5. **Ask form**: rendered, but no indication that the agent thread is
   blocked waiting on the operator. Easy to forget the agent is
   stalled.
6. **Compress**: when token budget triggers `_try_compress_memory`,
   the agent makes a synchronous LLM call to summarize. Operator sees
   `[context compressed]` only AFTER it finishes — nothing during.

### Sketch of the right shape

A single discipline applied to all six places: **every long-running
operation publishes a 1-Hz "still alive" status update with elapsed
time and any cheap per-source metric.**

The status bar already supports `state` + `detail`. We expand it to
carry an elapsed-time-since-spinner-started clock + a per-tool detail
string, both refreshing at ~1 Hz:

```
◐ running bash · 00:43 · 1.2 MB out · /tmp/openclaw-install.sh
◐ thinking · 00:08 · step 3/8 · 412 tokens out
◐ visiting · 00:04 · 18 KB / ~50 KB
◐ awaiting input · 00:31  ← yes, even forms get an "operator stalled" clock
```

Implementation:

- **Bash**: alongside the existing `_drain` thread, start a 1-Hz
  ticker that calls `app.set_status("running bash", f"{elapsed}s · {bytes_seen} out")`.
  Cancel on proc exit. Captures the bytes-seen-so-far counter from
  `_drain`.
- **LLM call**: wrap `agent.run(task)` in a ticker thread that updates
  status every second. If smolagents exposes streaming token counts
  (it does, via `step.output_token_count` accumulating live), surface
  those.
- **Visit**: ticker around the httpx `client.get()` call. httpx
  doesn't stream by default; either switch to streaming or just show
  elapsed without bytes.
- **Search**: usually <1s; lower priority. Same ticker pattern if it
  ever exceeds 2s.
- **Ask form**: status changes to `awaiting input` (already does)
  with a "seconds since form rendered" counter so the next operator
  can see at a glance "this has been sitting for 30 minutes".
- **Compress**: emit `[context compressing — older steps summarized]`
  to the top log BEFORE the summarization LLM call, not after.

### Heartbeat design choices to lock in

- **Cadence**: 1 Hz. Slower feels frozen; faster wastes redraws and
  cache-busts the tmux scroll buffer for anyone capturing transcripts.
  Constant-time updates are easier on operators than burst+silence.
- **What the spinner glyph means**: `◐ ◓ ◑ ◒` rotation is "I'm
  alive, the event loop is ticking". Operators learn this quickly.
  Status text + elapsed clock answer "doing what, for how long".
- **What it does NOT do**: no streaming partial LLM output into the
  middle region. Mid-thought text invites operators to interrupt
  ("I see where you're going, just do X") — but the agent isn't
  designed to incorporate mid-stream interruption without re-planning
  from scratch. Keep the middle region locked to the final answer.

### Test plan

- Snapshot tests of the status bar with each of: idle, thinking,
  running bash, visiting, awaiting input. Lock the format.
- Unit test that `BashTool.forward` posts at least N status updates
  for a command that runs N+1 seconds (with a fake clock).
- E2E: `apt install` against the test proxy + a real network-bound
  command (`curl -s http://httpbin.org/delay/8`). Operator sees a
  growing elapsed clock the whole way.

### Why this lives at the top of the priority list

§1 and §2 both target operator confidence, but §2 is the one that
makes the system feel **trustworthy** rather than just functional.
Even after §1 lands, there will be 30-second silences during
downloads — without §2, the operator still doesn't know whether
to wait or to Ctrl-C.

## 3. No operator-initiated cancellation of in-flight bash

Today, once the LLM calls `bash` with `timeout_sec=300`, the operator
has **no way to cancel** other than:

- wait 5 minutes for the timeout to fire,
- Ctrl-D to end the entire session (which leaves the subprocess
  orphaned in the agent's process group),
- SSH in from the side and `kill` the subprocess.

The TUI's Ctrl-C only resolves the active `ask` form. While bash is
running, Ctrl-C currently:

1. Triggers `OpsBridgeApp.action_cancel`.
2. Sets `cancel_requested` event in `core.py`.
3. The agent loop checks this between turns — **too late**, the current
   bash call already blocks the loop.

### Sketch

Two-press semantics modeled on most TUIs:

- **First Ctrl-C while a tool is running**: send `SIGTERM` to the
  subprocess group; status shows `cancelling…`. Tool returns
  `[cancelled by operator]` to the LLM. LLM is instructed (system
  prompt) to acknowledge and stop, not retry.
- **Second Ctrl-C within 2s**: send `SIGKILL`; agent thread joins; LLM
  is fed `[killed]` and the agent turn ends.

Wire-up:

- `BashTool` stores its current `proc` on `self._active_proc` while
  running.
- `OpsBridgeApp.action_cancel` (no active form) calls
  `bash_tool.cancel()` which signals the subprocess group.
- `_stream_lines` watches a `cancel_event`; on set, sends signals and
  returns partial output with a sentinel suffix.

This is also the v1 PRD §11 "interruptible long-running commands"
open question — phase 3 is the time.

## 4. LLM escalates `timeout_sec` on retry

Observed: first `bash` call had `timeout_sec=300`, the LLM saw partial
output + nonzero exit (from an external kill), retried with
`timeout_sec=600`. This is bad reflexive behavior on a host action;
the right move would be to ask the operator before retrying anything
destructive-state-affecting.

### Sketch (system prompt)

Add a clause to `prompts/system.md` §"Hard rules":

> When a previous tool call against shared state failed or was interrupted,
> do NOT silently retry with a longer timeout or different flags. Stop,
> describe what you observed, and **`ask` the operator** whether to retry,
> rollback, or escalate. Examples: failed apt install, partial curl|bash,
> half-applied `systemctl` action, kubectl apply that errored mid-rollout.

Pair with a regression test similar to T2-A3 (10 destructive prompts):
ten "first attempt failed" scenarios → 10/10 should `ask_pre_exec`
before retrying.

## 5. Queued operator turns are invisible

The agent loop processes one turn at a time via `turn_queue.get()`.
When the operator types a new request while the agent is busy with the
previous one, the new request is **silently appended to the queue**.
The operator sees their text scroll past in the top log (we echo
`> <text>`) but gets no feedback that it's queued. Worse: if the agent
hangs forever, the queue grows unbounded.

### Sketch

Two small UX additions:

1. After echoing `> <text>` to the top log, also flash a `(queued — N
   ahead)` line if `turn_queue.qsize() > 0` at submit time.
2. Show `queued: N` next to the spinner in the status bar when N > 0.
3. Bound the queue depth to ~5 with a polite reject: `(queue full —
   press Ctrl-C to cancel the current turn first)`.

## 6. Killing mid-install corrupts host state

This is the meta-issue behind §3. The current "send SIGTERM to the
subprocess" path is fine for a single `bash` command — `rm`, `ls`,
`tail` are all interruption-safe. But operators in practice use the
agent to run **transactions** (apt, npm, systemctl, dpkg). Killing
mid-transaction leaves:

- apt: `/var/lib/dpkg/lock-frontend` held, half-configured packages,
  needs `sudo dpkg --configure -a`.
- npm: half-extracted node_modules.
- systemctl restart: service in failed state.

### Sketch

The agent can't *prevent* this — a real Ctrl-C should still work in
emergencies. But we can make recovery cheaper:

- **Bash tool emits a `bash_post_kill` audit event** with the last
  ~200 lines of output + the signal sent. Recovery script (admin tool
  `opsbridge audit recovery`) prints "your last killed command was X
  on host Y at time Z, suggested recovery: `sudo dpkg --configure -a`"
  for known patterns.
- **System prompt § "Working tips"**: tell the LLM, on next session
  startup, to check for the standard "interrupted transaction"
  signals (dpkg lock, apt journal, half-systemd services) and surface
  them to the operator unprompted.

This converts a sharp failure into a soft one. Doesn't eliminate the
problem.

## 7. Sudo doesn't auto-suppress confirmation, per operator request

Operator note from the same session:

> "Remember in the future you don't have to ask me for sudo permissions."

The LLM acknowledged but **did not call `remember`** — the preference
wasn't saved. Next session would re-trigger confirmations. Two
problems:

- LLM didn't follow up the operator's "remember X" with a `remember add`
  call. Likely because the directive could be interpreted as relaxing a
  hard safety rule (which the system prompt explicitly forbids).
- Even if recorded, the current preferences-loading is one-way: the
  system prompt sees the bullet but has no clause that says "apply
  operator overrides to the confirmation rule, within these bounds."

### Sketch (deliberately small scope)

Don't let preferences override the destructive-command confirmation
chokepoint — that's the locked decision. Instead:

- Add a `confirm_sudo: bool` slot in `config.toml` (per-host, admin-set,
  default `true`).
- Document the trade-off in PRD-phase3 + `opsbridge config` prompts:
  setting it to false weakens the soft guardrail; the audit chokepoint
  (the `ask_pre_exec` event) gets bypassed. Operators who really want
  this need root on the host to flip the flag.

Per-operator-preference relaxation stays out of scope until per-key
preferences (PRD §11 open question) lands.

## 8. `_silence_third_party_noise` discipline is fragile

Phase 2 patched the "TUI lands in /var/log/.../stderr.log" bug by
restoring fd 2 after silencing imports. That fix is the right shape but
the **convention is implicit** — if some future contributor adds a new
helper that imports litellm or smolagents inside `main()` (vs. at module
top), the noise re-leaks; if they swap stderr without restoring it,
the TUI vanishes. A regression test that asserts stderr isn't a non-TTY
file descriptor by the time `run_session` is entered would lock this in.

## 9. Documentation gap: agent user lifecycle

When an operator hits Ctrl-D mid-session, the agent process exits but
its subprocess group (bash → curl → install.sh) **may still be running**
because of `start_new_session=True`. There's no documented "the next
SSH session will inherit residue" behavior. `opsbridge doctor` could
grow a `--check-orphans` flag that lists processes owned by `agent`
that aren't the current session's agent.

## 10. Wide-character (CJK / emoji) backspace leaves screen residue

### Symptom

Operator types Chinese into the input line (e.g., `安装nginx`). Each
CJK glyph takes **two terminal columns** but only one logical character
in the input buffer. When the operator hits Backspace, the textual
`Input` widget erases one logical char — but only repaints one column
worth of cells. The right-hand half of the wide glyph remains on
screen as **garbage** (`█`, `▌`, or a stale half-glyph) until the
operator types more or the widget redraws.

Likely also affects emoji (most width-2 in monospace fonts, some
width-1 or grapheme-cluster), combining characters, and zero-width
joiners.

### Root cause

textual's `Input` widget (as of 0.x at time of writing) computes glyph
widths assuming each codepoint is column-width 1 for cursor / erase
math. Wide characters need `wcwidth`/`east-asian-width` handling. This
is upstream behavior, not OpsBridge code — but it bites us hard
because OpsBridge has a Chinese-speaking operator base.

### Sketch

1. **Quick mitigation**: subclass textual's `Input` to force a full
   widget repaint on every keystroke (lose efficiency, gain correctness).
   Cheap.
2. **Right fix**: implement / contribute proper width calculation
   based on `wcwidth.wcswidth`. Two places need it: cursor advance
   on insert, and column count on delete.
3. **Test**: snapshot test with a fixture string mixing ASCII + CJK +
   emoji + combining diacritics. Should pass `pytest-textual-snapshot`
   reproducibly across runs.

Priority: **high** for operators who type Chinese; low for English-only
users. Bumps to high overall given the project's audience.

## 11. `/model` slash command — switch model mid-session

### Symptom

The operator picks a model at install time. During a session they realize
the chosen model is wrong for the task — too slow (sonnet on a quick
question), too cheap (haiku for a tricky bash debug), unavailable
(proxy rate-limited that model), or just curious to compare. Today the
only way out is:

- Ctrl-D → `sudo opsbridge config` → re-prompt for everything → re-ssh.

That's a six-step ceremony for what should be a one-line operator action.

### Acceptance shape

A `/model` slash command in the TUI input line:

- **`/model`** with no argument — opens a picker in the middle region.
  Re-uses install.sh's model-discovery: fetches `<base_url>/v1/models`,
  paginates if more than ~12 fit on screen, highlights the currently-active
  one, defaults to a sensible recommendation (first `claude-sonnet*` for
  anthropic, etc., same logic as install.sh `default_model_from_list`).
  Operator picks via number (`1`..`9`), arrow keys, or by typing a model
  id. Enter applies; Esc / Ctrl-C cancels with the active model intact.
- **`/model <id>`** with explicit id — swaps directly without the picker.
  Useful muscle-memory shortcut for operators who already know what they
  want (`/model claude-haiku-4-5`). If the id isn't in the discovered
  list, we still try it — proxies sometimes serve un-listed models.
- Either way, the swap is **session-only** by default. `/etc/opsbridge/
  agent/config.toml` is unchanged — next session re-loads the installed
  default. A separate `/model save <id>` (or `/model --persist`)
  variant writes back to config.toml. (Keep the dangerous one explicit;
  no surprise persistence.)

### Pagination

Most operators see 20–40 models on a real proxy (claude family + gpt
family + grok + gemini = ~25 today on our test proxy). Shoving all
into the middle region wastes screen space and forces scrolling on
small terminals (80×24 is the lower bound we support).

Page size = visible-rows-in-middle-region-minus-3 (header, footer, prompt).
Floor: 8. Navigation: `n` / `p` / arrow-down / arrow-up to page;
`/` to filter (typing narrows the list as you type, like fzf); Enter
to pick the highlighted entry.

State stays in the middle region — the model picker IS a form, same
widget class as the `ask` form. Top region keeps scrolling; status bar
shows `awaiting input · model picker` while open.

### Mid-flight semantics

What happens to an in-flight agent turn?

| State | `/model X` behavior |
|---|---|
| Idle (operator's turn) | Pick immediately; next operator turn uses new model |
| Agent thinking (LLM call) | Reject with `model swap queued — will apply after current turn`; apply after current turn ends |
| Running bash | Same — queue, apply after |
| Active `ask` form | Reject with `dismiss form first`; user must answer or Ctrl-C the ask form |

This sidesteps the smolagents-internal complexity of swapping `agent.model`
while a `run()` is mid-flight. (We tried — it segfaults on partial
streaming-response state in some smolagents versions.)

### Audit

New event:

| Event | Fields | When |
|---|---|---|
| `model_switch` | `from`, `to`, `source` (`/model` / `/model save` / install) | Right before `agent.model = new_model` takes effect |

The audit log already captures `model` in `session_start`; the new event
documents intra-session changes so a post-hoc reader can trace which
model handled which turn.

### Why not a full `/config` or `/set` shell

Considered, rejected. Operators wanted:

> "I want to flip the model. The other config rarely changes."

A general `/set provider=...` is a footgun — provider change implies key
change implies endpoint check; we'd be reimplementing install.sh inside
a chat. Keep `/model` narrow; add `/endpoint` or `/key` later only if
demand actually emerges.

## 12. `!` prefix — direct bash execution, skip the LLM

### Symptom

Operator types something they know is a shell command (`ls /etc/nginx`,
`tail -f /var/log/syslog`, `git status`). The agent currently routes
it through the LLM, which:

- Costs tokens for trivial reformulation.
- Adds 1–4 seconds of LLM round-trip on top of a sub-second command.
- Sometimes the LLM second-guesses the operator (asks for confirmation,
  rewrites the command "safer", etc.) when the operator just wanted to
  see the raw output.

Other AI shells solve this with a sigil prefix. We adopt the same.

### Acceptance shape

A line starting with `!` (with or without leading whitespace) is
handled as a **direct bash invocation** — the rest of the line goes
straight through `tool_bash` without the LLM ever seeing it. Output
streams into the top region exactly like a normal `bash` tool call,
and the same audit chain fires (`bash_pre_exec` → `tool_call`).

Examples:

| Input | Interpretation |
|---|---|
| `!ls /etc/nginx` | exec `ls /etc/nginx` directly |
| `! tail -n 20 /var/log/syslog` | exec `tail -n 20 /var/log/syslog` (leading space tolerated) |
| `!!` | re-run last `!` command (bash-style, optional v2 feature) |
| `please !ls` | NOT a direct exec — `!` not at start |
| `\!ls` | `\!` escapes the sigil; treated as English ("!ls") |

Audit log captures the source: `tool_call.source = "direct"` (vs.
default `"llm"`) so retrospective reads can distinguish operator-typed
commands from LLM-generated ones.

### Safety

Doesn't bypass the locked confirmation rule because there's no LLM in
the loop to decide — the operator is explicitly invoking. We trust
operator intent for direct exec; same risk surface as a regular shell.
The `bash_pre_exec` audit event still fires, so destructive direct
commands are still recorded.

`ask` form behavior under direct exec: not invoked. Operators using
the sigil have opted out of the confirmation chokepoint by design.
Document this clearly in `/help`.

### Why not just expose a separate `/bash` slash command

`/bash ls` works as a model but is twice as many keystrokes. The `!`
sigil is the universally-recognized "I want shell" signal (csh, bash,
fish, vi `:!`, jupyter, many AI shells). Keep the friction low.

## 13. Current-folder indicator in the TUI

### Symptom

The `bash` tool's working directory is `/home/agent` by default but
the LLM can `cd` mid-session (within a single `bash` call) — and
when operators use `!` (§12) for direct exec, they often want to
chain commands assuming the same cwd as the last one. Without a
visible cwd, they're guessing.

Even without `!`, operators benefit from knowing where the agent
"is" — answers a frequent first question of "did it touch a file
in /etc or in /tmp?".

### Acceptance shape

A cwd chip in the **status bar** (1 row, already present):

```
◐ idle · /home/agent · ctx 12%
```

For long paths, abbreviate via standard shell rules: replace `$HOME`
with `~`, truncate middle with `…` once total length exceeds ~30
chars. Examples:

| Real cwd | Displayed |
|---|---|
| `/home/agent` | `~` |
| `/home/agent/projects/x` | `~/projects/x` |
| `/var/log/opsbridge/agent` | `/var/log/opsbridge/agent` |
| `/usr/share/very/deep/nested/path/here` | `/usr/share/…/path/here` |

### Tracking the agent's cwd

`tool_bash` already passes `cwd=DEFAULT_BASH_CWD`. But the child shell
may `cd` inside the command (`cd /tmp && do_thing`). The parent
process's cwd doesn't change — but the operator's mental model says it
did.

Two implementation options:

1. **Honest mode**: status bar shows the cwd `tool_bash` was called
   WITH (always `/home/agent` unless we change defaults). Simple.
   Doesn't track in-command `cd`.
2. **Sticky mode**: at the end of each bash call, run `pwd` in the
   same subshell and capture it. Persist as the next call's cwd.

Sticky mode matches operator expectations (`!cd /tmp` followed by
`!ls` should ls /tmp). Implementation: append `; pwd 1>&3` with
fd 3 captured separately to a `cwd_track` variable.

### Source of truth

Sticky-tracked cwd is **per-session**, never persisted to config.
Resets to `/home/agent` (or whatever the install configured) on next
SSH login.

## 14. `/help` slash command

### Symptom

Operators discover features by reading docs, dogfooding, or asking
in chat. Even the existing `/quit`, `/exit`, `/q` aren't documented
anywhere the operator can see at runtime.

### Acceptance shape

`/help` (or `/?`) prints a one-screen reference into the top region:

```
[help] OpsBridge slash commands

  /model           open model picker (paginated)
  /model <id>      switch to <id> for this session
  /model save <id> switch and persist to config.toml
  /quit /exit /q   end the session
  /help /?         show this help

Direct exec:
  !<cmd>           run <cmd> via bash, skipping the LLM
                   (e.g. !tail -f /var/log/syslog)

Hotkeys:
  Ctrl-D ×2        quit (first press arms; second within 2s exits)
  Ctrl-C           cancel current ask form / running bash
  ↑ / ↓            input history

Audit log:
  /var/log/opsbridge/agent/<session-id>.jsonl

Preferences:
  /etc/opsbridge/agent/preferences.md  (mutate only via `remember`)
```

Help text is hand-curated, not auto-generated — it's faster to read
than a reflection-based dump and we control the wording. Versioned
with the code (lives in `tui.py` as a constant or a `.help.md`
resource file).

### Why not embed help in the system prompt

Tempting (would let `what can I do?` route via the LLM). But:

- Adds ~150 tokens to every call, every turn, forever.
- LLM might paraphrase or misremember the exact key/command names.
- `/help` should be available without an LLM call (offline, rate-
  limited, key-rotated mid-session, etc.).

Keep the operator-facing reference structural; let the LLM handle
"what can the agent do for me" via the existing system prompt.

---

## Priority

For the next phase planning:

| # | Issue | Priority |
|---|---|---|
| 1 | Pipe block-buffering / no live output | **high** (operator confidence) |
| 2 | "Still running" status heartbeat | **high** (same axis) |
| 3 | Operator-initiated bash cancel | **high** (recovery without footguns) |
| 4 | LLM retry-escalation discipline | medium (system-prompt + tests) |
| 5 | Queued-turn visibility | medium |
| 6 | Mid-transaction kill recovery | medium |
| 7 | Per-host sudo-confirmation toggle | low (real demand: 1 person) |
| 8 | stderr discipline regression test | low |
| 9 | Orphan-process documentation/doctor | low |
| 10 | Wide-char (CJK/emoji) backspace residue | **high** (CJK operators) |
| 11 | `/model` slash command (with pagination) | medium (quality-of-life, operator-requested) |
| 12 | `!` prefix for direct bash execution | medium (operator-requested) |
| 13 | Current-folder indicator in status bar | medium (operator-requested) |
| 14 | `/help` slash command | low (discoverability; trivial implementation) |

§1 + §2 + §3 are the "operators don't feel safe" cluster — should be
the bulk of Phase 3 if there's a Phase 3. §4 is a system-prompt change
+ E2E test; cheap. The rest are nice-to-haves.
