# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

**OpsBridge** — an SSH-login agent daemon. SSHing into a dedicated Linux
or macOS user (default name `agent`) drops the operator directly into a
smolagents `CodeAgent` full-screen TUI instead of a shell. The umbrella
project is `opsbridge`; v1 ships a single component — the `agent`. The
agent uses an LLM (OpenAI or Anthropic, configurable per host) with
seven tools — `read`, `write`, `bash`, `search`, `visit`, `ask`,
`remember` — to translate English requests into shell actions, and to
maintain a small persistent preferences file across sessions.

**Read `PRD.md` first**, then `PRD-phase2.md` for the v2 UX / web /
installer additions. They are the source of truth for scope,
architecture, tool surface, security model, and non-goals.

## Foundational choices (locked)

These were decided up front; don't re-litigate without asking:

- **Agent framework:** smolagents `CodeAgent` (Python). Not ToolCallingAgent,
  not a hand-rolled loop.
- **LLM backend:** OpenAI + Anthropic, dispatched through smolagents'
  `LiteLLMModel`. `base_url` is configurable per host (Azure / Bedrock /
  local OpenAI-compatible servers). No abstraction beyond what LiteLLM
  gives — don't add provider routing, fallback chains, or our own wrapper.
- **SSH integration:** `ForceCommand` in `sshd_config` matched on the `agent`
  user. Not a custom login shell, not per-key `command=`.
- **Deployment targets:** Linux+systemd and macOS+launchd. BSD deferred.
  No Docker image in v1.

## Design discipline

The whole point of the project is minimalism. When in doubt, cut:

- **Seven tools: three IO/exec + two info-retrieval + one human-input +
  one structural.** `read`, `write`, `bash` for IO/exec. `search`,
  `visit` for info retrieval. `ask` for operator confirmation.
  `remember` for structural preferences mutation. Don't add convenience
  tools (`edit`, `grep`, `find`, `ls`); the model uses `bash` for those.
  Don't add other structural tools unless they too solve an
  audit-chokepoint problem.
- **No sandboxing layer inside the TUI.** Unix permissions on the `agent`
  user are the sandbox. Don't add allowlists, command filters, or hard-coded
  confirmation gates — they create a false sense of security and bloat the
  surface area. The `ask` tool is operator-facing UX, not a sandbox.
- **System-prompt confirmation rule is load-bearing.** Because the `agent`
  user has NOPASSWD sudo, the only thing standing between an LLM hallucination
  and `rm -rf /` is the system prompt telling the model to call the `ask`
  tool before destructive or shared-state-affecting actions. Preserve and
  strengthen that guidance; do not weaken or remove it. The confirmation
  mechanism is the `ask` tool (form-rendered, audit-logged), not free-text
  `[y/N]`.
- **System prompt owns the judgment, tools stay dumb.** Conflict detection,
  risk assessment, conciseness, pruning — for both destructive-action
  confirmation and `remember` usage — live in the system prompt, not in
  tool implementations. Don't sneak validation logic into the `tools.py`
  functions; they only enforce structural invariants (size caps, format,
  duplicate rejection) and emit audit events.
- **System prompt lives in markdown, not Python.** The default is at
  `src/opsbridge/agent/prompts/system.md`; an optional per-host override
  at `/etc/opsbridge/agent/system_prompt.md` is validated against required
  safety anchors at every session start. Don't paste prompt content into
  `.py` files; edit the markdown.
- **Conversation memory is per-session; preferences persist.** Each SSH
  session starts with empty conversation history. The only carry-over is
  `/etc/opsbridge/agent/preferences.md` (≤ 50 lines / 4 KB), loaded into the
  system prompt at startup and mutable only via `remember`. If a feature
  needs more cross-session state, push back.
- **Full-screen TUI via textual is the v2 UX.** Non-TTY exits with a clear
  error; line-buffered fallback deferred to a later phase. Don't reach for
  additional TUI dependencies beyond textual (which transitively pulls
  rich + markdown-it-py).

## Layout

See PRD.md §10 for source and runtime layout. Phase 2 additions:
- `src/opsbridge/agent/prompts/` — `system.md` (default prompt) + README
- `src/opsbridge/agent/prompt_loader.py` — prompt loader + validator
- `src/opsbridge/agent/tui.py` — textual `App` (four-region layout)
- `install.sh` — top-level one-liner installer (curl|sudo bash)
