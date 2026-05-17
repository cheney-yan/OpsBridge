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


def test_update_budget_from_memory_pulls_token_counts():
    """Token counts on agent steps should accumulate into the budget."""
    agent = FakeAgent()

    class _Step:
        def __init__(self, ic, oc):
            self.input_token_count = ic
            self.output_token_count = oc

    agent.memory.steps = [_Step(100, 50), _Step(200, 75), _Step(300, 100)]

    budget = core.TokenBudget("openai/gpt-4o")
    logger = _FakeLogger()
    core._update_budget_from_memory(agent, budget, logger)
    # Only the last 3 steps are pulled; per step we add input + output.
    # Sum: 100+50 + 200+75 + 300+100 = 825.
    assert budget.used == 825


def test_update_budget_from_memory_handles_token_usage_dict():
    """token_usage as dict with total_tokens is also accepted."""
    agent = FakeAgent()

    class _Step:
        def __init__(self, total):
            self.token_usage = {"total_tokens": total}

    agent.memory.steps = [_Step(500)]
    budget = core.TokenBudget("openai/gpt-4o")
    core._update_budget_from_memory(agent, budget, _FakeLogger())
    assert budget.used == 500
