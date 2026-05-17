"""CodeAgent assembly + TUI launcher.

System prompt: see `prompts/system.md` (loaded by prompt_loader.py).
Tools: see tools.py.

This module owns:
- `TokenBudget` (context-window accounting, PRD §3 bands)
- `_try_compress_memory` (90% band compression)
- `run_session` — entry point. TTY → textual TUI on main thread, agent
  on background daemon thread. Non-TTY or `one_shot` → bypass TUI,
  run synchronously in the calling thread.
"""
from __future__ import annotations

import io as _io
import os
import queue
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from smolagents import CodeAgent
from smolagents.monitoring import AgentLogger, LogLevel

from . import tools as t
from .logging import SessionLogger
from .model import ModelConfig, build_model, load_config
from .prompt_loader import OVERRIDE_PATH, PromptSource, load_system_prompt

# Token budget bands (PRD §3).
SOFT_THRESHOLD = 0.80
COMPRESS_THRESHOLD = 0.90
HARD_THRESHOLD = 0.95


def _build_quiet_logger() -> AgentLogger:
    """smolagents AgentLogger that swallows all output."""
    try:
        from rich.console import Console
        quiet = Console(file=_io.StringIO(), highlight=False, force_terminal=False)
        return AgentLogger(level=LogLevel.ERROR, console=quiet)
    except ImportError:
        return AgentLogger(level=LogLevel.ERROR)


# ---------------------------------------------------------------------------
# Token budget
# ---------------------------------------------------------------------------

class TokenBudget:
    """Track cumulative session tokens against the model's context window."""

    _CONTEXT_WINDOWS = {
        "gpt-4o": 128_000,
        "gpt-4o-mini": 128_000,
        "gpt-4.1": 1_000_000,
        "gpt-4.1-mini": 1_000_000,
        "gpt-5": 256_000,
        "gpt-5-mini": 256_000,
        "claude-3-5-sonnet": 200_000,
        "claude-3-5-haiku": 200_000,
        "claude-3-7-sonnet": 200_000,
        "claude-sonnet-4": 200_000,
        "claude-sonnet-4-5": 200_000,
        "claude-opus-4": 200_000,
        "claude-opus-4-7": 200_000,
    }
    DEFAULT_WINDOW = 128_000

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id
        self.window = self._lookup_window(model_id)
        self.used = 0
        self.warned_soft = False
        self.compressed_once = False

    def _lookup_window(self, model_id: str) -> int:
        bare = model_id.split("/", 1)[-1].lower()
        if bare in self._CONTEXT_WINDOWS:
            return self._CONTEXT_WINDOWS[bare]
        for key, win in self._CONTEXT_WINDOWS.items():
            if bare.startswith(key):
                return win
        return self.DEFAULT_WINDOW

    def add(self, tokens: int) -> None:
        self.used += max(0, int(tokens))

    def set(self, tokens: int) -> None:
        """Set the current-context-size estimate (overwrites).

        Use this instead of `add` when reading the LATEST conversation
        size out of agent memory — repeated `add` calls across turns
        double-count tokens since each turn's prompt already includes
        the prior turns' history.
        """
        self.used = max(0, int(tokens))

    @property
    def ratio(self) -> float:
        return self.used / self.window if self.window > 0 else 0.0


# ---------------------------------------------------------------------------
# Helpers retained from Phase 1
# ---------------------------------------------------------------------------

def _ssh_key_fingerprint() -> str:
    auth_info = os.environ.get("SSH_USER_AUTH", "")
    if auth_info and os.path.exists(auth_info):
        try:
            with open(auth_info, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("publickey "):
                        parts = line.split(None, 2)
                        if len(parts) >= 3:
                            try:
                                res = subprocess.run(
                                    ["ssh-keygen", "-lf", "-"],
                                    input=line[len("publickey "):],
                                    capture_output=True,
                                    text=True,
                                    timeout=2,
                                )
                                if res.returncode == 0:
                                    return res.stdout.split()[1]
                            except (OSError, subprocess.SubprocessError):
                                pass
        except OSError:
            pass
    return os.environ.get("SSH_CLIENT", "unknown").split()[0] if os.environ.get("SSH_CLIENT") else "unknown"


def _format_preferences_block(prefs_path: Path) -> str:
    """Backward-compat wrapper used by tests."""
    from .prompt_loader import _format_preferences_block as fmt
    return fmt(prefs_path)


def build_system_prompt(prefs_path: Path) -> str:
    """Backward-compat shim — returns just the post-substitution text."""
    return load_system_prompt(prefs_path, fingerprint=_ssh_key_fingerprint()).text


# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------

@dataclass
class AgentBundle:
    agent: CodeAgent
    tools: list
    model: object
    prompt_source: PromptSource
    budget: TokenBudget


def _build_tools(
    *,
    logger: SessionLogger,
    config: ModelConfig,
    prefs_path: Path,
    app=None,
    ask_stdin=None,
    ask_stderr=None,
) -> list:
    return [
        t.ReadTool(logger=logger),
        t.WriteTool(logger=logger),
        t.BashTool(logger=logger, app=app),
        t.SearchTool(logger=logger, app=app),
        t.VisitTool(
            logger=logger,
            app=app,
            jina_api_key=config.visit.jina_api_key,
            timeout_sec=config.visit.timeout_sec,
            max_bytes=config.visit.max_bytes,
        ),
        t.AskTool(logger=logger, app=app, stdin=ask_stdin, stderr=ask_stderr),
        t.RememberTool(logger=logger, prefs_path=prefs_path),
    ]


def _build_agent(
    *,
    config: ModelConfig,
    logger: SessionLogger,
    prefs_path: Path,
    app=None,
    ask_stdin=None,
    ask_stderr=None,
) -> AgentBundle:
    tools_list = _build_tools(
        logger=logger,
        config=config,
        prefs_path=prefs_path,
        app=app,
        ask_stdin=ask_stdin,
        ask_stderr=ask_stderr,
    )
    model = build_model(config)
    prompt = load_system_prompt(
        prefs_path,
        fingerprint=_ssh_key_fingerprint(),
    )
    logger.emit(
        "system_prompt_source",
        path=prompt.path,
        sha256=prompt.sha256,
        override_used=prompt.override_used,
    )
    if prompt.rejected:
        logger.emit(
            "system_prompt_override_rejected",
            path=str(OVERRIDE_PATH),
            missing_anchors=list(prompt.missing_anchors),
        )

    agent = CodeAgent(
        tools=tools_list,
        model=model,
        add_base_tools=False,
        verbosity_level=0,
        max_steps=8,
        logger=_build_quiet_logger(),
        code_block_tags="markdown",
    )
    budget = TokenBudget(config.model_id)
    return AgentBundle(agent=agent, tools=tools_list, model=model, prompt_source=prompt, budget=budget)


# ---------------------------------------------------------------------------
# Compression + token accounting (unchanged from Phase 1)
# ---------------------------------------------------------------------------

def _is_network_error(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    return any(
        kw in name or kw in msg
        for kw in (
            "connection", "network", "timeout", "unreachable",
            "resolve", "dns", "name or service", "errno -2", "errno -3",
            "no route to host", "refused",
        )
    )


def _update_budget_from_memory(agent, budget: TokenBudget, logger: SessionLogger) -> None:
    """Estimate the current conversation context size from agent memory.

    smolagents stores per-step token accounting in `step.token_usage`
    (modern: `TokenUsage(input_tokens, output_tokens, total_tokens)`).
    `input_tokens` = the size of the prompt sent for that step, which
    already includes the running history — so the LATEST step's
    `total_tokens` is the best single-number estimate of "what does the
    next prompt look like".

    We OVERWRITE budget.used here (not add) so:
      - the same memory snapshot can be re-measured cheaply across turns,
      - post-compress the budget naturally drops (memory got smaller),
      - we don't double-count history that already counts itself.

    Legacy fallback for older smolagents that stored `input_token_count`
    / `output_token_count` as plain ints on the step.
    """
    try:
        steps = getattr(agent.memory, "steps", None) or []
        if not steps:
            return
        # Walk from newest to oldest, take the first step that has a usable
        # token count. We DON'T accumulate across steps.
        latest_total: int | None = None
        for step in reversed(steps):
            tu = getattr(step, "token_usage", None)
            if tu is not None:
                # TokenUsage dataclass — total_tokens is computed post-init.
                total = getattr(tu, "total_tokens", None)
                if total is None and isinstance(tu, dict):
                    total = tu.get("total_tokens")
                    if total is None:
                        total = (tu.get("input_tokens") or 0) + (tu.get("output_tokens") or 0)
                if total is not None:
                    latest_total = int(total)
                    break
            # Legacy attribute path (smolagents <1.10).
            ic = getattr(step, "input_token_count", None)
            oc = getattr(step, "output_token_count", None)
            if ic is not None or oc is not None:
                latest_total = int(ic or 0) + int(oc or 0)
                break
        if latest_total is not None:
            budget.set(latest_total)
    except Exception as exc:  # noqa: BLE001
        logger.emit("budget_update_skipped", error=str(exc))


def _try_compress_memory(agent, model, logger: SessionLogger) -> None:
    try:
        steps = getattr(agent.memory, "steps", None) or []
        if len(steps) < 4:
            return
        half = len(steps) // 2
        old, recent = steps[:half], steps[half:]
        chunks: list[str] = []
        for s in old:
            chunks.append(str(s)[:1000])
        joined = "\n---\n".join(chunks)
        prompt = (
            "Summarize the following agent steps in 5–8 lines, preserving "
            "key facts, file paths, and decisions but discarding low-value "
            "intermediate output.\n\n" + joined
        )
        try:
            resp = model([{"role": "user", "content": prompt}])
            summary = getattr(resp, "content", None) or str(resp)
        except Exception:  # noqa: BLE001
            summary = "[compress: summarization call failed — older steps were truncated to free context]"

        class _SummaryStep:
            def __init__(self, text: str) -> None:
                self.text = text
            def __str__(self) -> str:
                return f"[summary of {len(old)} earlier steps]\n{self.text}"
            # smolagents memory protocol — MemoryStep.to_messages accepts
            # `summary_mode: bool = False`; **kwargs hedges against new
            # signature additions in future versions.
            def to_messages(self, summary_mode: bool = False, **_kwargs):
                content = str(self)
                try:
                    from smolagents.models import ChatMessage, MessageRole  # type: ignore
                    return [ChatMessage(role=MessageRole.SYSTEM, content=content)]
                except ImportError:
                    return [{"role": "system", "content": content}]

        agent.memory.steps = [_SummaryStep(summary)] + recent
        logger.emit("context_compress", summarized=len(old), kept=len(recent))
    except Exception as exc:  # noqa: BLE001
        logger.emit("context_compress_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Session runners
# ---------------------------------------------------------------------------

NON_TTY_ERROR = (
    "OpsBridge requires a TTY. Use `ssh` (not `ssh -T`); avoid piping into "
    "`script` / `tee`."
)


def _is_tty(stream) -> bool:
    try:
        return stream.isatty()
    except (AttributeError, OSError):
        return False


def run_session(
    *,
    config: ModelConfig | None = None,
    prefs_path: Path = t.PREFS_PATH,
    stream_in=None,
    stream_out=None,
    stream_err=None,
    log_dir: Path | None = None,
    one_shot: str | None = None,
) -> int:
    """Run a single SSH session.

    - `one_shot is not None` → non-TUI exec path (scripts, tests, CI).
      Runs the agent in the calling thread, prints final answer to stdout.
    - Otherwise → requires a TTY. Launches the textual TUI.
    """
    stream_in = stream_in or sys.stdin
    stream_out = stream_out or sys.stdout
    stream_err = stream_err or sys.stderr

    if one_shot is not None:
        return _run_one_shot(
            user_msg=one_shot,
            config=config,
            prefs_path=prefs_path,
            stream_in=stream_in,
            stream_out=stream_out,
            stream_err=stream_err,
            log_dir=log_dir,
        )

    if not _is_tty(stream_out) or not _is_tty(stream_in):
        try:
            stream_err.write(NON_TTY_ERROR + "\n")
            stream_err.flush()
        except Exception:  # noqa: BLE001
            pass
        return 2

    return _run_tui_session(
        config=config,
        prefs_path=prefs_path,
        stream_err=stream_err,
        log_dir=log_dir,
    )


def _run_one_shot(
    *,
    user_msg: str,
    config: ModelConfig | None,
    prefs_path: Path,
    stream_in,
    stream_out,
    stream_err,
    log_dir: Path | None,
) -> int:
    """No textual import in this path — keeps the smoke/CI tests fast."""
    logger = SessionLogger(log_dir=log_dir)
    end_reason = "clean"
    try:
        if config is None:
            try:
                config = load_config()
            except (OSError, ValueError) as exc:
                stream_err.write(f"[opsbridge] config error: {exc}\n")
                logger.emit("session_end", reason="config_error", error=str(exc), turn_count=0)
                return 2
        logger.emit(
            "session_start",
            ssh_key_fingerprint=_ssh_key_fingerprint(),
            provider=config.provider,
            model=config.model,
            base_url=config.base_url,
            hostname=socket.gethostname(),
            one_shot=True,
        )
        bundle = _build_agent(
            config=config,
            logger=logger,
            prefs_path=prefs_path,
            app=None,
            ask_stdin=stream_in,
            ask_stderr=stream_err,
        )
        task = f"{bundle.prompt_source.text}\n\n# Operator request\n\n{user_msg}"
        logger.emit("turn_start", turn=1, user_msg=user_msg)
        try:
            result = bundle.agent.run(task, reset=True)
        except KeyboardInterrupt:
            end_reason = "interrupted"
            return 130
        except Exception as exc:  # noqa: BLE001
            stream_err.write(f"[opsbridge] error: {exc}\n")
            logger.emit("turn_error", turn=1, error=str(exc), error_type=type(exc).__name__)
            return 1
        _update_budget_from_memory(bundle.agent, bundle.budget, logger)
        if result is not None:
            stream_out.write(str(result).rstrip() + "\n")
            stream_out.flush()
        logger.emit("turn_end", turn=1, tokens_used=bundle.budget.used, ratio=bundle.budget.ratio)
        return 0
    finally:
        logger.emit("session_end", reason=end_reason, turn_count=1)
        logger.close()


def _run_tui_session(
    *,
    config: ModelConfig | None,
    prefs_path: Path,
    stream_err,
    log_dir: Path | None,
) -> int:
    """Launch the textual TUI on the main thread; agent in a daemon thread."""
    from .tui import OpsBridgeApp

    logger = SessionLogger(log_dir=log_dir)
    end_reason = "clean"
    turn_count = 0

    try:
        if config is None:
            try:
                config = load_config()
            except (OSError, ValueError) as exc:
                stream_err.write(f"[opsbridge] config error: {exc}\n")
                logger.emit("session_end", reason="config_error", error=str(exc), turn_count=0)
                return 2

        logger.emit(
            "session_start",
            ssh_key_fingerprint=_ssh_key_fingerprint(),
            provider=config.provider,
            model=config.model,
            base_url=config.base_url,
            hostname=socket.gethostname(),
        )

        # Inter-thread plumbing.
        turn_queue: queue.Queue[str | None] = queue.Queue()
        cancel_requested = threading.Event()

        def _on_operator_turn(text: str) -> None:
            turn_queue.put(text)

        def _on_cancel() -> None:
            cancel_requested.set()

        app = OpsBridgeApp(
            hostname=socket.gethostname(),
            model_label=config.model,
            on_operator_turn=_on_operator_turn,
            on_cancel=_on_cancel,
        )

        bundle = _build_agent(
            config=config,
            logger=logger,
            prefs_path=prefs_path,
            app=app,
        )

        nonlocal_state = {"turns": 0, "end_reason": "clean"}

        def _agent_loop() -> None:
            while True:
                user_msg = turn_queue.get()
                if user_msg is None:
                    nonlocal_state["end_reason"] = "eof"
                    break
                user_msg = user_msg.strip()
                if not user_msg:
                    continue
                turn = nonlocal_state["turns"] = nonlocal_state["turns"] + 1

                if bundle.budget.ratio >= HARD_THRESHOLD:
                    app.set_final_answer(
                        "[context exhausted; please disconnect and reconnect for a fresh session]"
                    )
                    logger.emit("context_exhausted", ratio=bundle.budget.ratio)
                    nonlocal_state["end_reason"] = "context_exhausted"
                    break

                if turn == 1:
                    task = f"{bundle.prompt_source.text}\n\n# Operator request\n\n{user_msg}"
                else:
                    task = user_msg

                logger.emit("turn_start", turn=turn, user_msg=user_msg)
                app.set_status("thinking", f"step 1")
                try:
                    result = bundle.agent.run(task, reset=(turn == 1))
                except Exception as exc:  # noqa: BLE001
                    logger.emit(
                        "turn_error",
                        turn=turn,
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                    app.set_status("idle", "error")
                    app.set_final_answer(f"[error] {exc}")
                    if _is_network_error(exc):
                        nonlocal_state["end_reason"] = "network_error"
                        break
                    continue

                _update_budget_from_memory(bundle.agent, bundle.budget, logger)
                pct = int(bundle.budget.ratio * 100)
                app.set_context_percent(pct)

                if result is not None:
                    app.set_final_answer(str(result).rstrip())

                logger.emit(
                    "turn_end", turn=turn,
                    tokens_used=bundle.budget.used,
                    ratio=bundle.budget.ratio,
                )
                app.set_status("idle", "")

                if not bundle.budget.warned_soft and bundle.budget.ratio >= SOFT_THRESHOLD:
                    app.write_top(f"[context: {pct}% used]")
                    bundle.budget.warned_soft = True
                if not bundle.budget.compressed_once and bundle.budget.ratio >= COMPRESS_THRESHOLD:
                    _try_compress_memory(bundle.agent, bundle.model, logger)
                    bundle.budget.compressed_once = True
                    app.write_top("[context compressed — older steps summarized]")

            try:
                app.call_from_thread(app.exit)
            except RuntimeError:
                pass

        agent_thread = threading.Thread(target=_agent_loop, daemon=True, name="agent-loop")
        agent_thread.start()

        try:
            app.run()
        except KeyboardInterrupt:
            end_reason = "interrupted"
        finally:
            # Tell the agent loop to stop after the current turn.
            turn_queue.put(None)
            agent_thread.join(timeout=2)
            end_reason = nonlocal_state.get("end_reason", end_reason)
            turn_count = nonlocal_state.get("turns", 0)

        return 0
    finally:
        logger.emit("session_end", reason=end_reason, turn_count=turn_count)
        logger.close()
