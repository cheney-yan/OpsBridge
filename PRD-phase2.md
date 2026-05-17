# UX — full-screen textual TUI

Companion to PRD.md. Records design decisions for the operator-facing
TUI added in Phase 2. Modeled on Hermes Agent's four-region layout
(scrollable output / pinned message / status bar / input line). Not
implemented yet; this is the specification.

**This section replaces the earlier "spinner-as-prompt-character"
StatusLine proposal.** That design was constrained by PRD §5's
"no streaming, no fancy TUI" rule; Phase 2 explicitly lifts that
constraint (see "PRD amendment" below) in exchange for a much better
operator experience.

## Problem

`agent.run()` blocks 5–30 seconds per operator turn. With line-
buffered stdin/stdout and `verbosity_level=0`, the operator sees a
blank cursor between input and final answer, occasional bursts of
`bash` output, and no indication of what the agent is doing or whether
it's alive. Final answers, bash logs, and prompts all scroll past in
one stream, fighting each other for the same screen real estate.

This stops being acceptable once the agent also has `search`, `visit`,
and (eventually) confirmation forms. The line-buffered model can't
host those without ugly pattern-matching on the LLM's output.

## Principles

Constraints the design must respect:

1. **TTY-only.** Full-screen TUI requires a real terminal. Non-TTY
   stdin/stdout falls back to a clear error: *"OpsBridge requires a
   TTY. Use `ssh` (not `ssh -T`); avoid piping into `script` / `tee`."*
   A line-buffered degraded mode is deferred to a later phase.
2. **Alternate screen buffer.** On exit, the operator's terminal
   scrollback is preserved untouched. The TUI lives in its own
   off-screen world (`\x1b[?1049h` / `\x1b[?1049l`), automatically
   handled by textual.
3. **Four regions, fixed roles.** Top scrollable output, middle pinned
   summary or form, status bar, input line. Each owns its rows; no
   coordination plumbing between them beyond textual's layout engine.
4. **The AI summarizes; the operator scrolls if curious.** `bash`
   output, search results, page fetches all stream into the top region.
   The middle region holds *only* the agent's final answer for the
   current turn, or a confirmation form when one is active.
5. **Confirmation is a form, not a typed `yes`.** When the LLM needs
   operator approval (per the load-bearing system-prompt rule), it
   calls the `ask` tool; the TUI renders a structured form in the
   middle region; the tool returns the operator's choice to the agent
   thread. No more pattern-matching `[y/N]` text.

## Region layout

```
┌────────────────────────────────────────────────────────────────────┐
│ OpsBridge · host hermes-01 · gpt-4.1-mini · ctx 12%                │  ← header (1 row)
├────────────────────────────────────────────────────────────────────┤
│ $ uname -a                                                          │
│ Linux hermes-01 6.5.0-...                                           │
│ $ apt show nginx                                                    │
│ Package: nginx                                                       │
│ ...                                                                  │
│ [search] "openclaw install" → 5 results                             │
│   1. github.com/openclaw/openclaw   - The OpenClaw project ...      │
│   2. ...                                                             │  ← TOP region: scrollable
│ [visit] github.com/openclaw/openclaw                                │     output stream
│ # OpenClaw                                                           │     (auto-trimmed)
│ Install via apt:                                                     │
│ ...                                                                  │
│                                                                      │
├────────────────────────────────────────────────────────────────────┤
│ Will install nginx (apt install nginx -y).                          │  ← MIDDLE region:
│ ~150 MB download, restarts nginx if already running.                │     final answer
│                                                                      │     OR form
├────────────────────────────────────────────────────────────────────┤
│ ◐ thinking · step 3/8 · 4.2s                                  ⌘ ^D │  ← status bar
├────────────────────────────────────────────────────────────────────┤
│ > install nginx_                                                     │  ← input line
└────────────────────────────────────────────────────────────────────┘
```

Sizing:

- **Header**: 1 row. Hostname, model, context-usage percent, session ID.
- **Top region**: flex-grows to fill remaining space. Internal buffer
  capped at ~1000 lines to bound memory; pageup/pagedown scrolls within
  the buffer. Older lines drop off the top (not the operator's
  terminal scrollback — that's preserved separately).
- **Middle region**: variable height, 2–10 rows depending on content.
  Auto-resizes to fit the final answer or form, capped at 30% of screen.
- **Status bar**: 1 row. Spinner glyph + textual status (`idle` /
  `thinking · step N/M` / `running bash` / `awaiting input`) + elapsed.
- **Input line**: 1 row. `>` prompt + cursor. Edit history via up/down
  arrows.

## Confirmation forms

When the LLM calls `ask`, the middle region transforms from "final
answer text" into a structured form. The shape depends on `options`:

- `options=["yes", "no"]` → radio buttons defaulted to "no", Y/N keys
  selectable, Enter confirms.
- `options=["yes", "no", "skip"]` → three radio buttons, arrow keys to
  cycle, Enter confirms.
- `options=None` → free-text input.

Example for a destructive command:

```
┌────────────────────────────────────────────────────────────────────┐
│ ▶ The agent proposes to run:                                       │
│     sudo apt install nginx -y                                       │
│   This will download ~150 MB and restart nginx if already running. │
│                                                                     │
│   Proceed?                                                          │
│     ( ) yes                                                         │
│     (•) no  ← default                                               │
└────────────────────────────────────────────────────────────────────┘
```

The form blocks the agent's `tool_call` until the operator answers.
The chosen value is returned to the LLM as the tool result.

The system prompt instructs the LLM to use `ask` (not plain text) for
all destructive-action confirmations. Plain-text `[y/N]` style replies
become a soft anti-pattern.

## The `ask` tool

Seventh tool. Signature:

```python
ask(prompt: str, options: list[str]) -> str
```

- `prompt`: the question text rendered in the middle region (multi-
  line OK; basic markdown via textual's `Markdown` widget).
- `options`: non-empty list of choices; rendered as radio buttons with
  the first option as default. Result is the chosen option string
  verbatim.
- Blocks the agent thread until the operator confirms.
- Cancellable via `Ctrl-C`, which returns `"__cancelled__"` to the LLM.

**No free-text mode.** Free-text from the operator goes through the
main input line as the next operator turn. `ask` exists only for
structured selection — keeping it narrow makes the audit log
unambiguous about whether the LLM was asking for confirmation or just
chatting.

## System-prompt nudge for `ask`

The TUI alone doesn't guarantee the LLM uses `ask`; the system prompt
has to push for it. `core.py:SYSTEM_PROMPT_TEMPLATE` gains a new
subsection (after "Hard rules"):

> ## Asking the operator
>
> When you need a yes/no decision from the operator — every time the
> "ask before destructive or shared-state-affecting actions" rule fires
> — call the `ask` tool, do NOT type "should I proceed? [y/N]" as plain
> text. Examples that MUST use `ask`:
>
> - `ask(prompt="Run `sudo apt install nginx -y`? This installs ~150 MB.", options=["yes", "no"])`
> - `ask(prompt="Three candidates found. Which?", options=["nginx", "caddy", "haproxy"])`
>
> Plain-text "type yes to continue" is an anti-pattern: it bypasses
> the audit log's `ask_pre_exec` event, defeating the confirmation
> chokepoint. The TUI cannot render a typed `[y/N]` as a form — the
> operator must scroll up and type into the main input, which is
> noisier and less safe.
>
> Read-only commands (`ls`, `cat`, `ps`, `journalctl`, etc.) do NOT
> need `ask`. Use it only when the existing "Hard rules" require
> confirmation.

This subsection is load-bearing: it's the only thing that converts the
locked confirmation rule into the new tool-based mechanism. Phase 2's
safety-critical regression test (see TEST-phase2.md §"Destructive
command uses `ask`, not plaintext") guards against the LLM ignoring
this nudge.

## Threading model

The single most important architectural detail of the TUI: how the
**agent thread** (smolagents `CodeAgent.run()`) communicates with the
**App main thread** (textual's asyncio event loop). Get this wrong and
the TUI deadlocks or drops events.

### Threads

- **Main thread**: textual's asyncio loop. Owns all widget I/O,
  rendering, and operator-input capture. Never blocks on agent work.
- **Agent thread**: a single daemon `threading.Thread` started at App
  startup. Runs `agent.run(task)` per operator turn. Blocks on LLM
  calls, bash subprocesses, etc.

### Message contract

Communication is one-way per direction:

| Direction | Mechanism | Messages |
|---|---|---|
| App → agent | `queue.Queue[OperatorTurn]` (thread-safe) | `OperatorTurn(text: str)` posted when operator hits Enter |
| Agent → App | `App.call_from_thread(...)` (textual-provided) | `LogLine(text)` per bash/search/visit output line; `FinalAnswer(text)` at end of turn; `StatusChange(state)` for "thinking" / "running bash" / "idle"; `AskRequest(prompt, options)` when ask is invoked |
| App → agent (reply path) | `threading.Event` set by App, agent waits on it | `AskResponse(choice: str)` published into a shared slot before the Event fires |

### `ask` blocking semantics

The `ask` tool runs on the **agent thread**. To render a form on the
**main thread** without re-entrancy:

```python
# inside AskTool.forward, agent thread:
event = threading.Event()
slot = {"choice": None}
app.call_from_thread(app.show_ask_form, prompt, options, slot, event)
event.wait()           # blocks agent thread; main thread keeps running
return slot["choice"]  # populated by app.show_ask_form's submit handler
```

The App's submit handler writes `slot["choice"]` and calls
`event.set()` — both happen on the main thread, before yielding back
to the event loop.

### `Ctrl-C` paths

- **Main thread Ctrl-C while idle**: textual catches it, exits cleanly.
- **Main thread Ctrl-C with form active**: form's submit handler is
  called with `choice="__cancelled__"`; agent thread unblocks; LLM
  sees the cancel string and is instructed (system prompt) to abort.
- **Main thread Ctrl-C while agent is running bash/LLM**: the agent
  thread's current step finishes naturally; the next iteration of the
  agent loop checks a `cancel_requested` flag and raises
  `KeyboardInterrupt` inside `agent.run()`. The App posts a
  `[cancelled]` status and returns to idle.

### Why threads, not asyncio everywhere

smolagents `CodeAgent.run()` is synchronous and does subprocess +
blocking HTTP calls. Wrapping it in asyncio would require either
rewriting smolagents or shoving every blocking call through
`run_in_executor` — and `tools.py`'s `bash` already manages its own
subprocess lifecycle. A single daemon thread plus textual's
`call_from_thread` is simpler.

## `--one-shot` and non-TUI execution

`run_session(one_shot=...)` is used by tests and scripted invocations.
In Phase 2 it bypasses the TUI entirely:

- `one_shot is not None` → no textual App is started. The agent runs
  directly in the calling thread, prints final answer to stdout, exits.
- `ask` tool in one-shot mode → reads from stdin (`input()`), prompts
  to stderr. This is enough for E2E tests; not a polished UX.
- All other tool behavior identical.

This keeps Phase 1's `--one-shot` regression tests working unchanged,
and gives us a non-TUI exec path for CI / cron / ansible.

## Audit logging

The TUI itself is "just rendering" — audit events come from the tools
and the agent loop, unchanged from earlier phases. Two additions:

| Event | Fields | When |
|---|---|---|
| `ask_pre_exec` | `prompt`, `options` | Before the form is rendered |
| `tool_call` (`tool=ask`) | `args`, `chosen`, `duration_ms`, `cancelled` | After operator answers |

The pre-exec captures the LLM's intent even if the operator drops the
SSH connection mid-prompt.

## SIGWINCH and disconnect handling

- **Terminal resize**: textual handles SIGWINCH and reflows. We don't
  manage rows manually.
- **SSH disconnect mid-turn**: the agent thread continues to its
  natural conclusion (a bash command finishes, an LLM call completes).
  When textual notices the TTY is gone (`OSError` on next render), it
  exits cleanly; the `session_end` audit event fires with
  `reason="tty_gone"`.
- **Ctrl-C in input line**: cancels the current turn (sends a cancel
  signal to the agent thread), returns to idle.
- **Ctrl-C inside a form**: returns `"__cancelled__"` from `ask`; the
  LLM sees this and is instructed (system prompt) to abort the
  proposed action and explain why.

## PRD amendment

This section explicitly lifts the PRD §5 lock that previously read:

> No streaming, no fancy TUI. Line-buffered stdin/stdout. If you reach
> for `rich` / `textual` / `prompt_toolkit`, ask first.

Phase 2 takes the dependency on **textual**. The amended rule:

> **Full-screen TUI via textual is the v2 UX.** Line-buffered fallback
> is deferred to a later phase. Don't reach for additional TUI
> dependencies beyond textual (which transitively pulls rich +
> markdown-it-py).

CLAUDE.md gets the same amendment.

The consolidated tool-count rule (single source of truth — supersedes
the earlier "six tools" amendment in the Web access section):

> **Seven tools: three IO/exec + two info-retrieval + one human-input +
> one structural.** `read`, `write`, `bash` for IO/exec. `search`,
> `visit` for info retrieval. `ask` for operator confirmation.
> `remember` for structural preferences mutation. Don't add convenience
> tools (`edit`, `grep`, `find`, `ls`); the model uses `bash` for those.

## Rejected alternatives

- **Keep the line-buffered REPL + raw-ANSI StatusLine (the previous
  spec in this file).** Cleaner technically; loses dramatically on UX.
  Pattern-matching `[y/N]` for confirmation forms is exactly the kind
  of fragility a real form widget eliminates.
- **prompt_toolkit instead of textual.** Lighter (~1.5 MB vs ~20 MB),
  but the imperative API costs us in layout glue and re-renders.
  textual's CSS-like layout + reactive state model fits a four-region
  app better.
- **rich with `rich.live` + `rich.layout`.** Already transitively
  installed via smolagents, no new dep. But rich is rendering, not
  interactivity — input handling, focus, key bindings would all need
  to be hand-rolled.
- **Raw ANSI alt-screen + manual region management.** ~500–1000 lines
  of TUI plumbing including SIGWINCH math; brittle. Not worth it for
  an ops tool.
- **bash as a separate pane that always shows the last N lines.**
  Considered; rejected because most turns have *no* bash, and a
  permanently-empty pane wastes screen rows that the middle region
  can use for forms.
- **Status bar with multiple sub-chips (Hermes-style: `[o] [ctx --]
  [▓▓▓] [25s] [@ 0s]`).** Pretty but information-poor. We get the same
  signal with a single status string + percent. Revisit if v2 ships
  and operators ask for it.

## Implementation pointers

- New file `src/opsbridge/agent/tui.py` — textual `App` subclass with
  four widgets: `Header`, `RichLog` (top), `Static` or `Markdown`
  (middle), `Footer` (status), `Input` (bottom).
- `core.py:run_session` becomes a launcher: detect TTY → start textual
  App → pass message queues to the agent thread → run agent in a
  background thread, route its events into the App.
- `core.py:_read_operator_input` is replaced by an asyncio Queue
  populated by the `Input` widget's `on_submitted` handler.
- All "write to stream_out" calls in `core.py` and `tools.py` become
  "post to top-region log" via the App.
- `tools.py:BashTool` calls `app.top.write(line)` for each captured
  bash line (replacing the rolling window entirely).
- New `tools.py:AskTool` — blocks on a `threading.Event` set by the
  form-widget submit handler.
- Non-TTY path: detect at startup, print
  `OpsBridge requires a TTY...` and exit 2.

Estimated diff: `tui.py` +400 lines (App + widgets + form rendering),
`core.py` -80 lines / +120 lines (loop replaced by thread + queue
wiring), `tools.py` -60 lines / +80 lines (drop rolling window, add
AskTool), tests +200 lines (using textual's `App.run_test()` harness).
Net ~+500 lines.

## Open questions for implementation time

- **`ask` cancellation semantics.** "Return `__cancelled__`" is one
  option; another is "raise a tool error the agent sees." Decide based
  on what the LLM does with each — but instinct says return a sentinel
  string, since errors invite retries.
- **Top region buffer cap.** 1000 lines feels right but a 30-step
  agent run with verbose bash could exceed it. Memory is trivial; lift
  to 5000 if needed.

---

# System prompt — externalization

Companion to PRD.md §6 (Agent design). Phase 2 moves the system prompt
out of `core.py` into a markdown file so it can be reviewed by
operators without reading Python, diffed cleanly across versions, and
optionally overridden per host with safety validation.

## Layout

```
src/opsbridge/agent/
├── core.py
└── prompts/
    ├── system.md            # default template; placeholders {hostname},
    │                        # {fingerprint}, {preferences_block}
    └── README.md            # "this file is read at startup; do not
                             #  edit at runtime; see /etc/opsbridge/
                             #  agent/system_prompt.md for overrides"
```

After install, the default lives at
`/opt/opsbridge/agent/.venv/lib/python3.12/site-packages/opsbridge/agent/prompts/system.md`
(read-only as part of the venv). The optional operator override lives
at `/etc/opsbridge/agent/system_prompt.md`.

## Load order

`build_system_prompt(prefs_path)` in `core.py`:

1. Read default from `importlib.resources.files("opsbridge.agent.prompts") / "system.md"`.
2. If `/etc/opsbridge/agent/system_prompt.md` exists:
   - Read it.
   - Validate (see below).
   - If valid → use it instead of the default.
   - If invalid → log `system_prompt_override_rejected` audit event
     with the missing anchor(s), print a banner to the operator at
     session start ("⚠ Override rejected — using default; see
     `opsbridge doctor`"), and fall back to the default.
3. Substitute `{hostname}`, `{fingerprint}`, `{preferences_block}`.

## Validation: required anchors

The override must contain all of these substrings, verbatim, otherwise
it is rejected:

| Anchor | Purpose |
|---|---|
| `## Hard rules` | Section header for the load-bearing rules |
| `ask before destructive` | The first hard rule |
| `preferences file is special` | The `remember`-only rule |
| `never fabricate tool output` | The honesty rule |
| `NOPASSWD sudo` | The trust-boundary warning |

These are chosen as **content-level** anchors, not whitespace-sensitive
delimiters, so operators can reformat / re-order surrounding text but
can't accidentally strip the safety vocabulary.

Validation is a simple `all(anchor in text for anchor in REQUIRED)`.
Cheap, deterministic, and easy to extend if a future hard rule
becomes load-bearing.

## Audit events

| Event | Fields | When |
|---|---|---|
| `system_prompt_source` | `path` (the default package path or `/etc/.../system_prompt.md`), `sha256` of the post-substitution text | At every session start |
| `system_prompt_override_rejected` | `path`, `missing_anchors` (list) | When validation fails |

The `sha256` lets the audit log prove which exact prompt the agent saw
across thousands of sessions — useful for incident review.

## `opsbridge doctor` check

`doctor` subcommand gains an `--system-prompt` check that:
- Confirms the default file is present in the venv.
- If the override exists, runs the validator and prints PASS/FAIL with
  any missing anchors.
- Prints the sha256 of the prompt that would be used at next session
  start.

## Why not append-only

An alternative was: append the override to the default rather than
replacing. Rejected because:
- The default's "Working tips" / "Current host context" sections are
  the most likely things an operator wants to *replace* per host
  (different cwd conventions, different credential paths).
- Append-only forces operators to live with default phrasing forever,
  which defeats the auditability goal.

The validator gives us the safety property (required anchors must
still appear) without locking the rest of the prompt.

## Effects on Phase 1

- `SYSTEM_PROMPT_TEMPLATE` constant in `core.py` is removed; replaced
  by a function that loads from package data.
- Existing test `test_core_smoke.py::test_system_prompt_contains_safety_rules`
  continues to work — it calls `build_system_prompt()`, which now
  reads the file. New test added for the validator.
- No runtime behavior change unless an override is present.

## Rejected alternatives

- **Option A (package-data, no override).** Loses the operator's
  ability to customize per host. The validator hybrid below is strictly
  better.
- **Option B (full override, no validation).** Lets operators delete
  the load-bearing safety rules. Defeats the CLAUDE.md lock that says
  the confirmation rule is load-bearing.
- **Append-only override.** See above; defeats the auditability
  motivation.
- **Templating with named blocks (Jinja-style).** Overkill for a 200-
  line prompt; introduces a second file format to learn.

## Open questions

- **Anchor list staleness.** If the default prompt evolves and removes
  one of the anchor strings, every override breaks at once. Mitigation:
  the anchor list is itself a tested invariant (`tests/test_prompts.py`
  asserts that the default's anchors match the validator's list).

---

# Confirmation UX — `bash` and `sudo`

Separate UX topic from the spinner design above. Recorded here because
it also concerns what the operator sees and is asked to decide during
a session.

## The question

> Should `BashTool` be designed to ask the operator for permission
> before any command that invokes `sudo`?

Concretely: a hardcoded check in `BashTool.forward()` — if the
command's first shell token is `sudo`, display the command, wait for
`yes`, then execute.

## The answer

**No.** Honoring an existing locked decision in CLAUDE.md:

> **No sandboxing layer inside the TUI.** Unix permissions on the
> `agent` user are the sandbox. Don't add allowlists, command filters,
> or hard-coded confirmation gates — they create a false sense of
> security and bloat the surface area.
>
> **System-prompt confirmation rule is load-bearing.** ... Preserve
> and strengthen that guidance; do not weaken or remove it.

Four reasons that decision was made, recorded so we don't re-litigate:

1. **String matching has holes.** `sh -c 'sudo …'`, `S=sudo; $S rm`,
   `eval $(echo sudo …)`, setuid binaries, `pkexec`, `systemd-run`,
   sudoers aliases, `at`/`cron` scheduling — none trigger a `sudo`-prefix
   filter. Worse: once the filter exists, both operator and future
   maintainers will assume "anything that wasn't gated is safe," which
   is wrong.

2. **`sudo`-only scope misses half the risk.** The system prompt
   already requires confirmation on `rm` outside `/tmp`, package
   installs, service restarts, firewall changes, killing other users'
   processes, writing to `/etc`/`/srv`/`/var/lib`, etc. The `agent`
   user has write permission on its own `~/.ssh/authorized_keys` and
   `~/.profile` — damaging those requires zero `sudo`. A `sudo`-only
   gate creates the illusion of coverage where coverage is partial.

3. **Alert fatigue trains the wrong reflex.** Read-only `sudo` is
   high-frequency in real ops: `sudo journalctl`, `sudo cat
   /var/log/auth.log`, `sudo systemctl status nginx`, `sudo ls /root`,
   `sudo netstat -tlnp`. If every one prompts, the operator hits `y`
   reflexively within 20 minutes — and that same reflex then catches
   the one truly dangerous prompt. Net safety: worse than no gate.

4. **Diffused responsibility = no responsibility.** Today the line is
   clear: the LLM decides whether to confirm; the tool just runs.
   Adding a hard gate creates a fuzzy split — LLM thinks "the tool
   will catch it," tool thinks "the LLM will have asked." Both sides
   slacken one notch; the system overall is less safe, not more.

## What this question usually really means

The "should we add a sudo gate" question typically masks one of three
different underlying concerns. Each has a different correct answer:

| Real concern | Correct response |
|---|---|
| "I don't trust the LLM to actually ask before sudo." | Not a hard gate. **Audit + E2E tests.** Verify via the existing `SessionLogger` that the LLM does ask in known-destructive scenarios. If it doesn't, fix the system prompt. |
| "I want a paper trail of every command, even if confirmation isn't required." | Not a hard gate. **Strengthen pre-execution logging:** have `SessionLogger.emit("bash_pre_exec", ...)` fire *before* `subprocess.Popen` runs, so the record survives even if the process is killed mid-run. Compatible with current architecture. |
| "On this specific high-stakes host, I want a hard gate regardless." | This is the only case that warrants breaking the locked decision. The right shape: a per-host **config flag** in `/etc/opsbridge/agent/config.toml` (e.g. `confirm_all_sudo = true`, default `false`). Document the exception in `PRD.md` and `CLAUDE.md`. Do NOT change the default. |

## Decision

Keep the locked design. The confirmation responsibility stays in the
system prompt (`core.py:SYSTEM_PROMPT_TEMPLATE` §"Hard rules"); the
tool stays dumb.

Validation work to do at some point (not blocking, but real):

- E2E tests that feed the agent destructive-sudo prompts and assert
  the LLM asks before calling `bash`. The test harness should fail
  loud if the LLM ever calls `bash` with a destructive command
  without a preceding confirmation turn.
- Pre-execution `bash_pre_exec` audit event added to `SessionLogger`,
  so logs reflect intent regardless of execution outcome.

Re-open this decision only if validation reveals the LLM consistently
fails to ask, or if a specific deployment scenario justifies the
per-host `confirm_all_sudo` flag.

---

# Web access — `search` and `visit`

Companion to PRD.md. Records design decisions for the two new
information-retrieval tools added in Phase 2. Not implemented yet;
this is the specification.

## Problem

The agent today has four tools — `read`, `write`, `bash`, `remember` —
none of which let it touch the internet. So if the operator says
"install OpenClaw" and the LLM has never heard of OpenClaw, the agent
has to guess or refuse. It can't:

- look up what a tool is when the name post-dates the model's training
  cutoff;
- read a project's current install instructions before running them;
- check today's CVEs, package versions, error-message context.

These are first-class ops needs, not edge cases. Closing the gap
requires letting the agent search the web and fetch a page.

## Principles

Constraints the design must respect:

1. **No local browser.** Chromium / Playwright are out. They're ~250 MB
   on disk and pull in a GUI-shaped dependency tree that doesn't belong
   on a headless SSH host.
2. **JS-rendered pages must still work.** Many modern doc sites are
   SPAs; "static fetch only" would leave a known coverage hole.
3. **Two tools, not one.** `search` returns ranked snippets + URLs;
   `visit` returns the markdown of one URL. Conflating them into a
   single "search and read" tool hides the URL choice from the operator
   audit log and makes the LLM less deliberate about which pages it
   reads.
4. **Audit every network egress.** Same chokepoint discipline as
   `bash_pre_exec`: log the *intent* (query / URL) before the request
   leaves the box, not just the result.
5. **Default works with zero secrets.** Out of the box, no API keys
   should be required. Keys raise rate limits but are never load-bearing.

## The design

Two new tools, both thin wrappers around `smolagents` built-ins so we
don't reinvent HTTP and HTML parsing.

### `search`

| | |
|---|---|
| Signature | `search(query: str, max_results: int = 5) -> str` |
| Backend | `smolagents.WebSearchTool` (parses search-engine HTML; no API key) |
| Return shape | One result per stanza: `N. <title>\n   <snippet>\n   <url>` |

Single backend, no config. If the scraping backend breaks (search
engine changes HTML), the fix is a smolagents upgrade. Pluggable
backends (Brave, DDG) are deferred to a later phase until someone
asks.

### `visit`

| | |
|---|---|
| Signature | `visit(url: str) -> str` |
| Backend | **Always** `https://r.jina.ai/<URL>` (Jina Reader cloud proxy) |
| Why Jina | One code path. Handles JS rendering server-side, handles bot-detection, returns LLM-ready markdown. No local browser needed. |
| Auth | Optional `JINA_API_KEY` (Bearer header). Unauthenticated = 20 RPM, plenty for interactive ops. Free key = 500 RPM. |
| Size cap | Response truncated to `max_bytes` (default 50 KB) to bound token cost; a `[truncated]` marker is appended when hit. |
| Timeout | `timeout_sec` (default 15s). On timeout, returns `[visit timeout after Ns]`. |

No `requests`-based fallback. If Jina is unreachable, `visit` returns a
clear error and the agent can either retry or tell the operator. The
single-path design was chosen explicitly over "local fetch first,
Jina fallback" because the dual-path version doubles the test surface,
and Jina's rate limits are not the bottleneck for interactive use.

## Configuration

`/etc/opsbridge/agent/config.toml` gets one new optional block for the
visit tool. Search has no config.

```toml
[visit]
jina_api_key = ""             # optional; raises rate limit 20 → 500 RPM
timeout_sec = 15
max_bytes = 50_000
```

The interactive installer (see "Installation" §) collects
`jina_api_key` if the operator supplies one.

## System prompt updates

`src/opsbridge/agent/prompts/system.md` (see "System prompt —
externalization" §) gains a new "Web access" subsection between "How
to use `remember`" and "Working tips". Key rules:

- **Use `search` when you don't recognize a name or need current info**
  (CVEs, package versions, today's docs). One search per question is
  usually enough — don't spam.
- **Use `visit` after `search` has identified one specific URL worth
  reading**, not speculatively. Don't fetch a list of URLs "to compare";
  pick the best one, read it, then decide.
- **Never `visit` a URL that came from the operator without showing
  it first.** Same logic as the destructive-command rule: prevent
  prompt injection from steering the agent to fetch attacker-chosen
  pages.
- **Honor the size cap.** If `visit` returns `[truncated]`, the rest of
  the page isn't reachable; summarize from what was returned or
  refine the URL (anchor, sub-page).
- **No web for things you already know.** Don't `search "how to use rm"` —
  use the existing tools.

## Audit logging

Two new event types in `SessionLogger`, mirroring the `bash_pre_exec`
pattern locked in §"Confirmation UX" above:

| Event | Fields | When |
|---|---|---|
| `search_pre_exec` | `query`, `backend` | Before the request leaves the box |
| `tool_call` (`tool=search`) | `args`, `result_count`, `duration_ms` | After response (success or error) |
| `visit_pre_exec` | `url` | Before the request leaves the box |
| `tool_call` (`tool=visit`) | `args`, `bytes`, `duration_ms`, `truncated` | After response |

The pre-exec events fire *before* the HTTP call so the audit trail
captures intent even if the network is killed mid-request.

## Rejected alternatives

- **`bb-browser`** (npm package controlling a real Chrome via CDP).
  Wrong category — requires Chrome installed *and* a running desktop
  session. Designed for the operator's laptop, not a headless server.
- **Playwright + Chromium.** ~250 MB on disk plus a sandboxed browser
  process per fetch. Violates "no local browser" principle. Operators
  who genuinely need it can `bash apt install` themselves; not the
  default.
- **Tavily / Brave / Exa as `visit` backend.** Their search/extract
  combos are good but tie us to a paid tier and a single vendor.
  Jina's unauthenticated 20 RPM is the right floor for "works on a
  fresh install with no secrets."
- **Single combined `web` tool that searches and fetches in one call.**
  Hides the URL choice from the audit log; encourages the LLM to be
  less deliberate about *which* page it reads. Two tools, two events,
  two decisions.
- **Local `requests`-first with Jina fallback.** Doubles the test
  surface; the fallback logic ("looks SPA-empty? retry") is fuzzy and
  brittle. Single Jina path is simpler.
- **`bash curl` only (no `visit` tool).** Works in principle but the
  agent has to parse raw HTML noise out of every page; token-heavy
  and unreliable for following install docs.

## Implementation pointers

- New `SearchTool`, `VisitTool` classes in `src/opsbridge/agent/tools.py`,
  same `(logger=None, ...)` ctor shape as the existing four. Each
  emits its `*_pre_exec` event before the network call.
- `core.py:run_session` constructs both and appends them to
  `agent_tools` after `BashTool`.
- `core.py:SYSTEM_PROMPT_TEMPLATE` gains the "Web access" subsection
  described above.
- `model.py` config parsing extends to read `[search]` and `[visit]`
  blocks; absent blocks fall back to documented defaults.
- New test files: `tests/test_search.py`, `tests/test_visit.py`. For
  `visit`, the Jina HTTP call is mocked in unit tests; an opt-in
  integration test hits `https://r.jina.ai/https://example.com`.

Estimated diff: `tools.py` +120 lines (two tool classes + Jina HTTP
client), `core.py` +30 lines (wiring + system prompt section),
`model.py` +30 lines (config parsing), tests +150 lines.

## Open questions for implementation time

- **HTML extraction quality of `WebSearchTool`.** It scrapes search-
  engine HTML; layout changes can break it silently. The `tool_call`
  audit event captures `result_count`, so a sudden drop to zero across
  hosts is an early signal we can monitor.
- **Truncation at 50 KB.** Install READMEs occasionally run longer.
  Revisit if frequent truncation shows up in the audit log.

---

# Installation — `install.sh` one-liner

Companion to PRD.md §10 ("Layout"). Records design for the bootstrap
UX added in Phase 2. Not implemented yet; this is the specification.

## Problem

Today the install path is two manual steps:

```bash
git clone <repo>
cd opsbridge
sudo ./bootstrap.sh         # uv + venv + symlink admin CLI
sudo opsbridge install      # sshd hook + sudoers + agent user + config
```

…and the second step requires the operator to know in advance: which
LLM provider to use, the API key, optionally a Jina key, and which SSH
pubkey to authorize. There's no README and no one-line invocation.

For an ops tool, the "I want to try this on a fresh VM right now"
threshold matters. We want a single curl pipe to be all the operator
types.

## Principles

1. **One line on the operator's command line.** No `git clone` before
   the install; the script handles its own source acquisition.
2. **Interactive prompts in the middle, not env-var-only.** Operators
   should be able to install without reading docs first to discover
   what `OPSBRIDGE_PROVIDER` is.
3. **Re-runnable.** Running the curl pipe a second time is safe and
   either updates or skips, never corrupts.
4. **No vendored secrets.** The script never has a default API key.
5. **Linux+systemd only for v1.** Cleanly error out on Mac/BSD until
   we decide to broaden (see "Platform compat" §, TBD).

## The one-liner

```bash
curl -fsSL https://raw.githubusercontent.com/<user>/opsbridge/main/install.sh | sudo bash
```

For a pinned version:

```bash
curl -fsSL https://raw.githubusercontent.com/<user>/opsbridge/v0.2.0/install.sh | sudo bash
```

## The `/dev/tty` trick

curl-pipe-bash means the script's stdin *is* the curl pipe, not the
operator's terminal. `read` would just consume HTTP body bytes. To
prompt interactively the script re-attaches stdin to the controlling
TTY:

```bash
if [ ! -t 0 ] && [ -e /dev/tty ]; then
    exec </dev/tty
fi
```

If `/dev/tty` is unavailable (CI, container without a TTY allocated),
the script falls back to env-var driven mode:

```bash
OPSBRIDGE_PROVIDER=openai \
OPSBRIDGE_MODEL=gpt-4.1-mini \
OPSBRIDGE_API_KEY=... \
OPSBRIDGE_PUBKEY="ssh-ed25519 AAAA... user@host" \
curl -fsSL .../install.sh | sudo bash
```

Same script, both paths.

## Script flow

```
install.sh
├── 1. detect platform           (Linux + systemd; clean error otherwise)
├── 2. detect re-run             (existing /etc/opsbridge/agent/config.toml?)
│        └── [k]eep / [r]econfigure / [a]bort
├── 3. acquire source            (git clone → /opt/opsbridge-src;
│                                 or curl tarball if no git installed)
├── 4. run bootstrap.sh          (existing; uv + venv + admin CLI symlink)
└── 5. run `opsbridge install --interactive`   (new flag on existing subcommand)
        ├── prompt sequence (below)
        ├── write /etc/opsbridge/agent/config.toml
        ├── write /etc/opsbridge/agent/api.key (mode 0600)
        ├── append operator pubkey to /home/agent/.ssh/authorized_keys
        ├── apply sshd + sudoers snippets (existing logic)
        └── final summary + next steps
```

The interactive provisioning lives in Python (`opsbridge install
--interactive`), not in bash. Reason: it's testable. The shell script
stays thin and orchestrational.

## Prompt sequence

In order, with sensible defaults shown in brackets:

1. **LLM provider** — `openai` / `anthropic` / `custom` (OpenAI-
   compatible base URL). Default: `openai`.
2. **Model** — default depends on provider (`gpt-4.1-mini` /
   `claude-sonnet-4-5` / no default for `custom`).
3. **Custom base URL** — shown only if provider = `custom`.
4. **LLM API key** — hidden input (`read -s` / `getpass`). Stored to
   `/etc/opsbridge/agent/api.key`, mode `0600`, owner `root:agent`.
5. **Jina API key** — optional. Plain "press Enter to skip" hint.
   Skipping means `visit` uses 20 RPM unauthenticated, which is fine
   for interactive ops.
6. **Operator SSH pubkey** — paste the full line. Validated by a quick
   `ssh-keygen -lf -` round-trip; rejected with a clear message if
   parsing fails. Scripted installs can set `OPSBRIDGE_PUBKEY=...` to
   skip this prompt.
7. **Review screen** — show the resolved config (API key truncated to
   `sk-...XXXX`), require `y` to apply.

## Idempotency

Step 2 above handles re-runs. The "reconfigure" path re-prompts the
sequence; the "keep" path skips prompting and just re-syncs sshd /
sudoers / venv (useful for `git pull` then re-run after a code update).

The pubkey prompt is additive: if the operator's pubkey already
appears in `authorized_keys`, it's skipped silently.

## README quick-start

A new top-level `README.md` opens with:

```markdown
# OpsBridge

SSH into a server and land in an AI sysadmin TUI instead of a shell.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/<user>/opsbridge/main/install.sh | sudo bash
```

The script will prompt for an LLM API key and the SSH pubkey to
authorize. Takes about a minute.

## Use

```bash
ssh agent@your-server
```

…and you're in the agent. Type requests in English. Ctrl-D to leave.

## Uninstall

```bash
sudo opsbridge uninstall
```
```

Below the quick-start, the README links out to `PRD.md` for the full
design and `PRD-phase2.md` for the in-progress UX layer.

## Rejected alternatives

- **Env-var-only install (no interactivity).** Fast for the script
  author, painful for first-time operators who have to read docs to
  learn the var names. Kept as the fallback path for CI; not the
  default.
- **Two-stage install** (curl downloads tarball, operator extracts,
  runs `./install.sh`). Cleaner from a security audit angle (operator
  can `cat install.sh` before running it), but breaks the "one line"
  goal. `curl | sudo bash` is the established ops pattern (rustup,
  uv, Homebrew); we follow it.
- **Vanity install URL** (`install.opsbridge.dev`). Until there's a
  brand identity, the raw GitHub URL is fine. Revisit at v1.0.
- **`opsbridge install` as a single non-flag interactive command.**
  Considered, but `--interactive` keeps the non-interactive flag-driven
  path available unchanged for CI / Ansible / scripted installs.

## Open questions for implementation time

- **Source acquisition.** `git clone` is the obvious choice but a
  fresh VM may not have git. Decision: try git; if absent, fall back
  to `curl <tarball-url> | tar -xz`.
- **Pubkey paste UX.** Multi-line pubkeys with comments can be ugly to
  paste over SSH. The "use the pubkey you logged in with" shortcut
  covers 80% of cases. For the rest, support `OPSBRIDGE_PUBKEY=...`
  env var even in interactive mode (skips that prompt).
- **What happens on SSH disconnect mid-install?** Probably fine since
  the script is short, but worth a `nohup`/`disown` wrapper note in
  the README.

---

# Platform compat

Phase 2 broadens the PRD §"Deployment target: bare-metal Linux +
systemd" lock to include macOS. BSD remains out of scope.

| Target | Status | Why |
|---|---|---|
| Linux + systemd | ✅ | Phase 1 path; unchanged |
| macOS (12+) + launchd | ✅ | Sysadmins self-hosting on Mac is a real case |
| FreeBSD / OpenBSD / NetBSD | ❌ Deferred | Three more init-system code paths for marginal user count |

## What changes for macOS

`install.sh` dispatches on `uname -s`. `admin.py` resolves all paths
through a single `PLATFORM_PATHS` dict keyed on `platform.system()`.

| Concern | Linux | macOS |
|---|---|---|
| Service manager | systemd unit at `/etc/systemd/system/opsbridge-agent.service` | launchd plist at `/Library/LaunchDaemons/com.opsbridge.agent.plist` |
| Agent user create | `useradd agent` | `dscl . -create /Users/agent` (+ Group) |
| Agent home | `/home/agent` | `/Users/agent` |
| sshd config dir | `/etc/ssh/sshd_config.d/` | Same |
| sudoers | `/etc/sudoers.d/opsbridge-agent` | Same |
| Log dir, install prefix, config dir | `/var/log`, `/opt`, `/etc` | Same |

## Two macOS gotchas to handle

1. **Remote Login is off by default.** Installer detects via
   `systemsetup -getremotelogin` and prints a clear instruction to
   enable it — does NOT enable silently (changing the user's SSH access
   is too sensitive to automate).
2. **`launchd` plist is wiring, not a daemon.** The agent is a login
   shell, not a long-running service. The plist exists only to
   declare the `ForceCommand`-equivalent path; no `KeepAlive`.

## Non-supported platforms

`install.sh` exits 1 with: *"OpsBridge supports Linux and macOS.
Detected: $(uname -s). File an issue if you need BSD/other support."*

---

# CLAUDE.md amendments shipped in Phase 2

Phase 2 invalidates several CLAUDE.md "locked" decisions. The actual
CLAUDE.md edit is a Phase 2 deliverable, not a follow-up. Track here
so nothing is forgotten:

| CLAUDE.md section | Phase 2 change |
|---|---|
| "Four tools: three IO/exec + one structural" | → "Seven tools: three IO/exec + two info-retrieval + one human-input + one structural" |
| "No streaming, no fancy TUI. Line-buffered stdin/stdout..." | → "Full-screen TUI via textual is the v2 UX. Non-TTY exits with a clear error; line-buffered fallback deferred." |
| "Deployment target: bare-metal Linux + systemd. No Docker image in v1." | → "Deployment targets: Linux+systemd and macOS+launchd. BSD deferred." |
| "System prompt owns the judgment, tools stay dumb." | Unchanged in spirit, but the prompt now lives at `src/opsbridge/agent/prompts/system.md` with an optional validated override at `/etc/opsbridge/agent/system_prompt.md`. New rule: "Don't paste prompt content into `.py` files; edit the markdown." |
| "No sandboxing layer inside the TUI. Unix permissions on the agent user are the sandbox." | Unchanged. The `ask` tool is operator-facing UX, not a sandbox. |
| "Confirmation responsibility stays in the system prompt." | Unchanged in spirit. Mechanism becomes the `ask` tool (called from the prompt's directive), not free-text `[y/N]`. |

The CLAUDE.md commit ships in the same merge as the rest of Phase 2.
Phase 2 is not complete until CLAUDE.md reflects the amended rules.

