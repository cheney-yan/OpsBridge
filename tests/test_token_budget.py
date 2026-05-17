"""Synthetic tests for the token-budget bands (TEST.md Phase 4).

The real exhaustion behavior is hard to drive against a live LLM, so we
inject usage directly into the TokenBudget and assert the band logic
fires correctly.
"""
from __future__ import annotations

import pytest

from opsbridge.agent import core


def test_band_thresholds_are_pinned():
    """Document the band thresholds — changing these breaks the PRD §3 contract."""
    assert core.SOFT_THRESHOLD == 0.80
    assert core.COMPRESS_THRESHOLD == 0.90
    assert core.HARD_THRESHOLD == 0.95


class TestTokenBudgetBands:
    def test_starts_empty(self):
        b = core.TokenBudget("openai/gpt-4o")
        assert b.used == 0
        assert b.ratio == 0.0
        assert not b.warned_soft
        assert not b.compressed_once

    def test_soft_band_entry(self):
        b = core.TokenBudget("openai/gpt-4o")
        b.add(int(b.window * 0.81))
        assert core.SOFT_THRESHOLD <= b.ratio < core.COMPRESS_THRESHOLD

    def test_compress_band_entry(self):
        b = core.TokenBudget("openai/gpt-4o")
        b.add(int(b.window * 0.91))
        assert core.COMPRESS_THRESHOLD <= b.ratio < core.HARD_THRESHOLD

    def test_hard_band_entry(self):
        b = core.TokenBudget("openai/gpt-4o")
        b.add(int(b.window * 0.96))
        assert b.ratio >= core.HARD_THRESHOLD

    def test_negative_token_count_clamped(self):
        b = core.TokenBudget("openai/gpt-4o")
        b.add(-100)
        assert b.used == 0


class FakeAgent:
    """Stand-in agent: records calls and exposes a `memory.steps` list."""

    class _Memory:
        def __init__(self):
            self.steps = []

    def __init__(self, returned_value: object = "ok"):
        self.memory = self._Memory()
        self.calls: list[tuple[str, bool]] = []
        self._returned = returned_value

    def run(self, task: str, reset: bool = False):
        self.calls.append((task, reset))
        return self._returned


class FakeModel:
    """Minimal model stub."""

    def __init__(self):
        self.calls: list = []

    def __call__(self, messages):
        self.calls.append(messages)

        class _Resp:
            content = "summary text"

        return _Resp()


def test_compress_replaces_old_steps_with_summary():
    """_try_compress_memory should collapse the oldest half of steps into a single summary."""
    agent = FakeAgent()
    # 6 fake steps.
    class _Step:
        def __init__(self, n):
            self.n = n

        def __str__(self):
            return f"step-{self.n}"

    agent.memory.steps = [_Step(i) for i in range(6)]
    model = FakeModel()
    logger = _FakeLogger()
    core._try_compress_memory(agent, model, logger)
    # Half (3) of the original steps should have been replaced with a single summary stub.
    assert len(agent.memory.steps) == 4  # 1 summary + 3 recent
    # The summary stub stringifies and exposes the summary text.
    summary_repr = str(agent.memory.steps[0])
    assert "summary of 3 earlier steps" in summary_repr
    assert "summary text" in summary_repr
    # An audit event was emitted.
    assert any(ev == "context_compress" for ev, _ in logger.events)


def test_compress_skipped_when_few_steps():
    agent = FakeAgent()
    agent.memory.steps = [object(), object()]  # too few
    logger = _FakeLogger()
    core._try_compress_memory(agent, FakeModel(), logger)
    assert len(agent.memory.steps) == 2  # untouched


def test_compress_summary_step_matches_smolagents_protocol():
    """The replacement step's `to_messages` must accept `summary_mode`.

    Newer smolagents (>=1.20) calls `step.to_messages(summary_mode=True)`
    when collecting context, so a bare `to_messages(self)` signature
    crashes the agent loop with:
      _SummaryStep.to_messages() got an unexpected keyword argument 'summary_mode'
    """
    agent = FakeAgent()
    class _Step:
        def __str__(self):
            return "step"
    agent.memory.steps = [_Step() for _ in range(6)]
    core._try_compress_memory(agent, FakeModel(), _FakeLogger())
    summary_step = agent.memory.steps[0]
    # Must accept summary_mode (and tolerate future kwargs).
    msgs = summary_step.to_messages(summary_mode=True)
    assert msgs, "to_messages must return at least one message"
    # Also tolerate extra kwargs we don't know about yet.
    summary_step.to_messages(summary_mode=False, future_flag=1)
    # Each message exposes a role + content reachable by attribute or key.
    msg = msgs[0]
    role = getattr(msg, "role", None) or msg.get("role")
    content = getattr(msg, "content", None) or msg.get("content")
    assert str(role).lower().endswith("system")
    assert "summary text" in str(content)


def test_compress_handles_summarization_failure():
    """If the summarization LLM call fails, we fall back to a placeholder summary."""
    class BrokenModel:
        def __call__(self, messages):
            raise RuntimeError("network down")

    agent = FakeAgent()
    class _Step:
        def __str__(self):
            return "step"
    agent.memory.steps = [_Step() for _ in range(6)]
    logger = _FakeLogger()
    core._try_compress_memory(agent, BrokenModel(), logger)
    # Compression still proceeded with a fallback summary.
    assert len(agent.memory.steps) == 4
    assert "compress: summarization call failed" in str(agent.memory.steps[0])


def test_is_network_error_detects_common_patterns():
    cases = [
        ConnectionError("connection refused"),
        TimeoutError("timeout reading response"),
        OSError("[Errno -2] Name or service not known"),  # DNS
        RuntimeError("LLM unreachable: name resolution failed"),
    ]
    for exc in cases:
        assert core._is_network_error(exc), f"missed: {exc}"


def test_is_network_error_ignores_unrelated():
    assert not core._is_network_error(ValueError("bad arg"))
    assert not core._is_network_error(KeyError("k"))


class _FakeLogger:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, event, **payload):
        self.events.append((event, payload))

    def close(self):
        pass


def test_update_budget_from_memory_takes_latest_step():
    """budget.used = latest step's total tokens (no accumulation).

    Each turn's prompt input already INCLUDES prior history, so summing
    across steps double-counts. The latest step's `total_tokens` is the
    best single-number estimate of current context size — that's what
    we compare to the model's context window.
    """
    agent = FakeAgent()

    class _Step:
        def __init__(self, ic, oc):
            self.input_token_count = ic
            self.output_token_count = oc

    agent.memory.steps = [_Step(100, 50), _Step(200, 75), _Step(300, 100)]
    budget = core.TokenBudget("openai/gpt-4o")
    core._update_budget_from_memory(agent, budget, _FakeLogger())
    # Only the LATEST step counts: 300 + 100 = 400.
    assert budget.used == 400


def test_update_budget_from_memory_handles_token_usage_dataclass():
    """The modern smolagents path uses TokenUsage with total_tokens."""
    agent = FakeAgent()

    class _TU:
        def __init__(self, total):
            self.total_tokens = total
            self.input_tokens = total - 50
            self.output_tokens = 50

    class _Step:
        def __init__(self, total):
            self.token_usage = _TU(total)

    # Latest step wins.
    agent.memory.steps = [_Step(300), _Step(450), _Step(900)]
    budget = core.TokenBudget("openai/gpt-4o")
    core._update_budget_from_memory(agent, budget, _FakeLogger())
    assert budget.used == 900


def test_update_budget_from_memory_handles_token_usage_dict():
    """Dict-shaped token_usage with total_tokens — older proxies."""
    agent = FakeAgent()

    class _Step:
        def __init__(self, total):
            self.token_usage = {"total_tokens": total}

    agent.memory.steps = [_Step(500)]
    budget = core.TokenBudget("openai/gpt-4o")
    core._update_budget_from_memory(agent, budget, _FakeLogger())
    assert budget.used == 500


def test_update_budget_does_not_accumulate_across_calls():
    """Re-measuring the same memory snapshot must yield the same number.

    Regression: previously `_update_budget_from_memory` used `add()` so
    successive calls inflated `used` linearly — triggering compress
    well before the actual context window was near full.
    """
    agent = FakeAgent()

    class _TU:
        def __init__(self, total):
            self.total_tokens = total
            self.input_tokens = total - 100
            self.output_tokens = 100

    class _Step:
        def __init__(self, total):
            self.token_usage = _TU(total)

    agent.memory.steps = [_Step(1000), _Step(2000)]
    budget = core.TokenBudget("openai/gpt-4o")
    for _ in range(5):
        core._update_budget_from_memory(agent, budget, _FakeLogger())
    # Five measurements of the same memory → still 2000, not 10_000.
    assert budget.used == 2000


def test_update_budget_drops_after_compress():
    """After compress shrinks memory, the next measurement reflects it.

    With `set` semantics, budget.used follows current memory size — so
    compressing 90% of history down to a summary halves the ratio,
    freeing room for more turns. With the old `add` semantics this
    didn't work: used kept growing forever.
    """
    agent = FakeAgent()

    class _TU:
        def __init__(self, total):
            self.total_tokens = total
            self.input_tokens = total - 50
            self.output_tokens = 50

    class _Step:
        def __init__(self, total):
            self.token_usage = _TU(total)

    # Pre-compress: latest step is at 180k.
    agent.memory.steps = [_Step(50_000), _Step(120_000), _Step(180_000)]
    budget = core.TokenBudget("openai/gpt-4o")  # 128k window
    core._update_budget_from_memory(agent, budget, _FakeLogger())
    pre = budget.used
    assert pre == 180_000

    # Compress collapses old steps into a stub w/o token_usage.
    class _SummaryStub:
        pass
    agent.memory.steps = [_SummaryStub(), _Step(30_000)]
    core._update_budget_from_memory(agent, budget, _FakeLogger())
    # Budget reflects the post-compress latest step, not the pre-compress max.
    assert budget.used == 30_000
    assert budget.used < pre
