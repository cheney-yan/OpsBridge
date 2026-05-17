"""Unit tests for SearchTool — formatting, audit events, cap."""
from __future__ import annotations

from typing import Any

import pytest

from opsbridge.agent import tools as t


class _Logger:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, event, **fields):
        self.events.append((event, dict(fields)))


def test_search_formats_list_results(tmp_path):
    """If the backend returns list[dict], we format stanzas with title/snippet/url."""
    backend_calls: list[str] = []

    def backend(q: str) -> Any:
        backend_calls.append(q)
        return [
            {"title": "OpenClaw GitHub", "body": "C++ port of the OpenClaw engine", "href": "https://github.com/openclaw/openclaw"},
            {"title": "OpenClaw release notes", "body": "v0.2.4 fixes input lag", "href": "https://example.com/release"},
        ]

    logger = _Logger()
    tool = t.SearchTool(logger=logger, backend=backend)
    result = tool.forward("OpenClaw", max_results=2)
    assert "OpenClaw GitHub" in result
    assert "https://github.com/openclaw/openclaw" in result
    assert backend_calls == ["OpenClaw"]

    events = [e for e, _ in logger.events]
    assert events.index("search_pre_exec") < events.index("tool_call"), \
        "pre-exec must precede the tool_call event"
    tool_call = next(p for e, p in logger.events if e == "tool_call")
    assert tool_call["result_count"] == 2
    assert tool_call["backend"] == "web_search"


def test_search_max_results_capped(tmp_path):
    """Asking for max_results=999 must be capped at SEARCH_MAX_RESULTS_CAP."""
    seen_cap: dict = {}

    def backend(q: str) -> Any:
        # capped value lives in meta, not in the backend signature, so check
        # by inspecting the audit log.
        return [{"title": "x", "body": "y", "href": "https://x"}]

    logger = _Logger()
    tool = t.SearchTool(logger=logger, backend=backend)
    tool.forward("anything", max_results=999)
    tool_call = next(p for e, p in logger.events if e == "tool_call")
    assert tool_call["args"]["max_results"] == 999
    # Inspect tool_search meta directly to confirm cap clamps:
    _, meta = t.tool_search("anything", max_results=999, backend=backend)
    assert meta["max_results"] == t.SEARCH_MAX_RESULTS_CAP


def test_search_pre_exec_fires_first():
    """The pre_exec event records intent before the backend is called."""
    order: list[str] = []
    logger = _Logger()
    original_emit = logger.emit

    def trace(event, **fields):
        order.append(event)
        original_emit(event, **fields)

    logger.emit = trace  # type: ignore[assignment]

    def backend(q):
        order.append("backend")
        return []

    t.SearchTool(logger=logger, backend=backend).forward("foo")
    assert order[0] == "search_pre_exec"
    assert "backend" in order
    assert order.index("search_pre_exec") < order.index("backend")


def test_search_backend_error_returns_error_string():
    """Backend exceptions become user-visible `[search error: ...]` strings."""
    def boom(q):
        raise RuntimeError("backend exploded")

    logger = _Logger()
    result = t.SearchTool(logger=logger, backend=boom).forward("x")
    assert "[search error" in result
    assert "backend exploded" in result
