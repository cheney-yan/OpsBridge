# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

**OpsBridge** — an SSH-login agent daemon. SSHing into a dedicated Linux
user (default name `agent`) drops the operator directly into a smolagents
`CodeAgent` TUI instead of a shell. The umbrella project is `opsbridge`;
v1 has a single component, the `agent`, which is what almost everything
here is about. The agent uses an LLM (OpenAI or Anthropic, configurable
per host) with four tools — `read`, `write`, `bash`, `remember` — to
translate English requests into shell actions, and to maintain a small
persistent preferences file across sessions.

**Read `PRD.md` first** — it's the source of truth for scope, architecture,
tool surface, security model, and non-goals. This file only covers things a
PRD doesn't.

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
- **Deployment target:** bare-metal Linux + systemd. No Docker image in v1.

## Design discipline

The whole point of the project is minimalism. When in doubt, cut:

- **Four tools: three IO/exec + one structural.** `read`, `write`, `bash`
  handle file IO and execution. `remember` is the sole audit chokepoint
  for `/etc/opsbridge/agent/preferences.md` — that's its only justification. Don't
  add convenience tools (`edit`, `grep`, `find`, `ls`); the model uses
  `bash` for those. Don't add other structural tools unless they too
  solve an audit-chokepoint problem.
- **No sandboxing layer inside the TUI.** Unix permissions on the `agent`
  user are the sandbox. Don't add allowlists, command filters, or hard-coded
  confirmation gates — they create a false sense of security and bloat the
  surface area.
- **System-prompt confirmation rule is load-bearing.** Because the `agent`
  user has NOPASSWD sudo, the only thing standing between an LLM hallucination
  and `rm -rf /` is `core.py`'s system prompt telling the model to ask the
  operator before destructive or shared-state-affecting actions. Preserve and
  strengthen that guidance; do not weaken or remove it.
- **System prompt owns the judgment, tools stay dumb.** Conflict detection,
  risk assessment, conciseness, pruning — for both destructive-action
  confirmation and `remember` usage — live in `core.py`'s prompt, not in
  tool implementations. Don't sneak validation logic into the `tools.py`
  functions; they only enforce structural invariants (size caps, format,
  duplicate rejection) and emit audit events.
- **Conversation memory is per-session; preferences persist.** Each SSH
  session starts with empty conversation history. The only carry-over is
  `/etc/opsbridge/agent/preferences.md` (≤ 50 lines / 4 KB), loaded into the system
  prompt at startup and mutable only via `remember`. If a feature needs
  more cross-session state, push back.
- **No streaming, no fancy TUI.** Line-buffered stdin/stdout. If you reach
  for `rich` / `textual` / `prompt_toolkit`, ask first.

## Layout

See PRD.md §10 for source and runtime layout. The repo is currently empty
— scaffolding hasn't been written yet.
