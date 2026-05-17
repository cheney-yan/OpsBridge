"""Unit tests for VisitTool — Jina proxy contract, truncation, timeout, audit."""
from __future__ import annotations

import httpx
import pytest
import respx

from opsbridge.agent import tools as t


class _Logger:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, event, **fields):
        self.events.append((event, dict(fields)))


@respx.mock
def test_visit_uses_jina_proxy():
    respx.get("https://r.jina.ai/https://example.com").respond(
        status_code=200,
        text="# Example\nHello world\n",
    )
    logger = _Logger()
    tool = t.VisitTool(logger=logger)
    result = tool.forward("https://example.com")
    assert "Hello world" in result
    tool_call = next(p for e, p in logger.events if e == "tool_call")
    assert tool_call["status"] == 200
    assert tool_call["truncated"] is False


@respx.mock
def test_visit_with_api_key_sets_authorization():
    route = respx.get("https://r.jina.ai/https://example.com").respond(text="ok")
    tool = t.VisitTool(jina_api_key="sk-jina-xyz")
    tool.forward("https://example.com")
    assert route.called
    request = route.calls.last.request
    assert request.headers.get("authorization") == "Bearer sk-jina-xyz"


@respx.mock
def test_visit_without_api_key_no_authorization_header():
    route = respx.get("https://r.jina.ai/https://example.com").respond(text="ok")
    t.VisitTool().forward("https://example.com")
    request = route.calls.last.request
    assert "authorization" not in {k.lower(): v for k, v in request.headers.items()}


@respx.mock
def test_visit_truncates_at_max_bytes():
    big = "x" * 100_000
    respx.get("https://r.jina.ai/https://example.com").respond(text=big)
    logger = _Logger()
    result = t.VisitTool(logger=logger, max_bytes=50_000).forward("https://example.com")
    assert "[truncated]" in result
    assert len(result.encode("utf-8")) < 51_000
    tool_call = next(p for e, p in logger.events if e == "tool_call")
    assert tool_call["truncated"] is True
    assert tool_call["bytes"] == 100_000


@respx.mock
def test_visit_timeout_returns_marker():
    respx.get("https://r.jina.ai/https://example.com").mock(
        side_effect=httpx.TimeoutException("timed out")
    )
    logger = _Logger()
    result = t.VisitTool(logger=logger, timeout_sec=1).forward("https://example.com")
    assert "[visit timeout" in result
    tool_call = next(p for e, p in logger.events if e == "tool_call")
    assert tool_call["status"] == -1


def test_visit_rejects_non_http_url():
    result = t.VisitTool().forward("file:///etc/passwd")
    assert "[visit error" in result
    assert "http" in result


def test_visit_pre_exec_event_recorded():
    logger = _Logger()
    # Use respx context for HTTP isolation
    with respx.mock:
        respx.get("https://r.jina.ai/https://example.com").respond(text="ok")
        t.VisitTool(logger=logger).forward("https://example.com")
    events = [e for e, _ in logger.events]
    assert events[0] == "visit_pre_exec"
    pre = next(p for e, p in logger.events if e == "visit_pre_exec")
    assert pre["url"] == "https://example.com"


@respx.mock
def test_visit_http_error_status_returned():
    respx.get("https://r.jina.ai/https://example.com").respond(status_code=500, text="boom")
    result = t.VisitTool().forward("https://example.com")
    assert "[visit error: HTTP 500" in result
