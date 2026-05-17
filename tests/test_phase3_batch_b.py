"""Phase 3 Batch B acceptance tests.

Covers §11 — /model slash command:
  - /model <id>            → direct session swap
  - /model save <id>       → swap + persist to config.toml
  - /model bare            → opens paginated picker
  - Picker navigation: n/p paging, 1-9 digit pick, Enter apply, Esc cancel
  - discover_models / persist_model_in_config helpers
"""
from __future__ import annotations

from pathlib import Path

import pytest
import respx

from opsbridge.agent import model as M
from opsbridge.agent.model import ModelConfig, VisitConfig
from opsbridge.agent.tui import OpsBridgeApp, _PickerState, _render_picker


# ---------------------------------------------------------------------------
# discover_models helper
# ---------------------------------------------------------------------------

@respx.mock
def test_discover_models_parses_openai_shape():
    """Standard OpenAI-compatible /v1/models response: {"data":[{"id":...}]}"""
    respx.get("https://proxy.example.com/v1/models").respond(
        json={
            "data": [
                {"id": "claude-haiku-4-5", "object": "model"},
                {"id": "claude-sonnet-4-6", "object": "model"},
                {"id": "gpt-4.1-mini", "object": "model"},
            ],
        },
    )
    cfg = ModelConfig(
        provider="openai", model="gpt-4o", base_url="https://proxy.example.com/v1",
        api_key="k", visit=VisitConfig(),
    )
    ids = M.discover_models(cfg)
    assert ids == ["claude-haiku-4-5", "claude-sonnet-4-6", "gpt-4.1-mini"]


@respx.mock
def test_discover_models_returns_empty_on_failure():
    """4xx/5xx/timeout → empty list, never raises."""
    respx.get("https://proxy.example.com/v1/models").respond(status_code=500)
    cfg = ModelConfig(
        provider="openai", model="gpt-4o", base_url="https://proxy.example.com/v1",
        api_key="k", visit=VisitConfig(),
    )
    assert M.discover_models(cfg) == []


def test_discover_models_anthropic_native_uses_hardcoded():
    """Anthropic vendor (no base_url) returns the curated short-list."""
    cfg = ModelConfig(
        provider="anthropic", model="claude-sonnet-4-6", base_url="",
        api_key="k", visit=VisitConfig(),
    )
    ids = M.discover_models(cfg)
    assert "claude-sonnet-4-6" in ids
    assert "claude-haiku-4-5" in ids


# ---------------------------------------------------------------------------
# persist_model_in_config — write back to config.toml
# ---------------------------------------------------------------------------

def test_persist_model_rewrites_model_line(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '# preserved comment\n'
        'provider = "anthropic"\n'
        'model    = "claude-sonnet-4-5"\n'
        'base_url = "https://proxy.example.com/v1"\n'
        '\n'
        '[visit]\n'
        'jina_api_key = "k"\n'
    )
    ok = M.persist_model_in_config("claude-sonnet-4-6", config_path=cfg_path)
    assert ok is True
    new_text = cfg_path.read_text()
    assert 'model    = "claude-sonnet-4-6"' in new_text
    # Surrounding content preserved.
    assert "preserved comment" in new_text
    assert "provider = \"anthropic\"" in new_text
    assert "[visit]" in new_text
    assert "jina_api_key" in new_text


def test_persist_model_missing_file_returns_false(tmp_path):
    assert M.persist_model_in_config("x", config_path=tmp_path / "nope.toml") is False


def test_persist_model_missing_model_line_returns_false(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('provider = "anthropic"\n')  # no model = line
    assert M.persist_model_in_config("x", config_path=cfg_path) is False


# ---------------------------------------------------------------------------
# _PickerState — pagination + selection logic
# ---------------------------------------------------------------------------

class TestPickerState:
    def test_total_pages_for_short_list(self):
        s = _PickerState(models=["a", "b", "c"], page_size=10)
        assert s.total_pages == 1

    def test_total_pages_for_long_list(self):
        s = _PickerState(models=[f"m{i}" for i in range(25)], page_size=10)
        assert s.total_pages == 3

    def test_visible_first_page(self):
        s = _PickerState(models=[f"m{i}" for i in range(25)], page_size=10)
        visible = s.visible()
        assert len(visible) == 10
        assert visible[0] == (0, "m0")
        assert visible[-1] == (9, "m9")

    def test_visible_last_page_partial(self):
        s = _PickerState(models=[f"m{i}" for i in range(25)], page_size=10, page=2)
        visible = s.visible()
        assert len(visible) == 5
        assert visible[0] == (20, "m20")

    def test_select_relative_wraps(self):
        s = _PickerState(models=["a", "b", "c"], page_size=10)
        s.select_relative(-1)
        assert s.selected_idx == 2

    def test_select_relative_moves_page(self):
        s = _PickerState(models=[f"m{i}" for i in range(25)], page_size=10)
        s.selected_idx = 9
        s.select_relative(1)
        assert s.selected_idx == 10
        assert s.page == 1

    def test_page_relative_clamps(self):
        s = _PickerState(models=[f"m{i}" for i in range(25)], page_size=10)
        s.page_relative(99)
        assert s.page == 2
        s.page_relative(-99)
        assert s.page == 0


# ---------------------------------------------------------------------------
# _render_picker — visible text content
# ---------------------------------------------------------------------------

class TestRenderPicker:
    def test_render_marks_selected(self):
        s = _PickerState(models=["a", "b", "c"], selected_idx=1)
        text = _render_picker(s)
        assert any(line.lstrip().startswith("(•)") and "b" in line for line in text.splitlines())

    def test_render_highlights_current_model(self):
        s = _PickerState(models=["a", "b"], current_model="b")
        text = _render_picker(s)
        assert "← current" in text

    def test_render_shows_page_info_when_paginated(self):
        s = _PickerState(models=[f"m{i}" for i in range(25)], page_size=10)
        text = _render_picker(s)
        assert "page 1/3" in text
        assert "n=next" in text

    def test_render_handles_empty_models(self):
        s = _PickerState(models=[])
        text = _render_picker(s)
        assert "no models discovered" in text


# ---------------------------------------------------------------------------
# Integration: /model command dispatch in the TUI
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_slash_model_with_id_calls_on_model_swap():
    swaps: list[tuple[str, bool]] = []
    app = OpsBridgeApp(
        hostname="h", model_label="old-model",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
        on_model_swap=lambda mid, persist: swaps.append((mid, persist)),
    )
    async with app.run_test() as pilot:
        await pilot.press(*list("/model new-id"), "enter")
        await pilot.pause()
    assert swaps == [("new-id", False)]


@pytest.mark.asyncio
async def test_slash_model_save_persists():
    swaps: list[tuple[str, bool]] = []
    app = OpsBridgeApp(
        hostname="h", model_label="old",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
        on_model_swap=lambda mid, persist: swaps.append((mid, persist)),
    )
    async with app.run_test() as pilot:
        await pilot.press(*list("/model save new-id"), "enter")
        await pilot.pause()
    assert swaps == [("new-id", True)]


@pytest.mark.asyncio
async def test_bare_slash_model_opens_picker():
    app = OpsBridgeApp(
        hostname="h", model_label="claude-sonnet-4-6",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
        on_model_swap=lambda _m, _p: None,
        discover_models=lambda: ["claude-haiku-4-5", "claude-sonnet-4-6", "gpt-5"],
    )
    async with app.run_test() as pilot:
        await pilot.press(*list("/model"), "enter")
        await pilot.pause()
        assert app._active_picker is not None
        assert app._active_picker.current_model == "claude-sonnet-4-6"
        assert app._active_picker.selected_idx == 1


@pytest.mark.asyncio
async def test_picker_digit_pick_applies():
    swaps: list[tuple[str, bool]] = []
    models = ["a", "b", "c", "d"]
    app = OpsBridgeApp(
        hostname="h", model_label="a",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
        on_model_swap=lambda mid, p: swaps.append((mid, p)),
        discover_models=lambda: models,
    )
    async with app.run_test() as pilot:
        await pilot.press(*list("/model"), "enter")
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
    assert swaps == [("c", False)]
    assert app._active_picker is None


@pytest.mark.asyncio
async def test_picker_escape_cancels():
    swaps: list[tuple[str, bool]] = []
    app = OpsBridgeApp(
        hostname="h", model_label="a",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
        on_model_swap=lambda mid, p: swaps.append((mid, p)),
        discover_models=lambda: ["a", "b", "c"],
    )
    async with app.run_test() as pilot:
        await pilot.press(*list("/model"), "enter")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert swaps == []
    assert app._active_picker is None


@pytest.mark.asyncio
async def test_picker_paging_with_n_and_p():
    models = [f"m{i}" for i in range(25)]
    app = OpsBridgeApp(
        hostname="h", model_label="m0",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
        on_model_swap=lambda _m, _p: None,
        discover_models=lambda: models,
    )
    async with app.run_test() as pilot:
        await pilot.press(*list("/model"), "enter")
        await pilot.pause()
        assert app._active_picker.page == 0
        await pilot.press("n")
        await pilot.pause()
        assert app._active_picker.page == 1
        await pilot.press("p")
        await pilot.pause()
        assert app._active_picker.page == 0


@pytest.mark.asyncio
async def test_picker_falls_back_when_discover_empty():
    app = OpsBridgeApp(
        hostname="h", model_label="m",
        on_operator_turn=lambda _t: None,
        on_cancel=lambda: None,
        on_model_swap=lambda _m, _p: None,
        discover_models=lambda: [],
    )
    async with app.run_test() as pilot:
        await pilot.press(*list("/model"), "enter")
        await pilot.pause()
    assert app._active_picker is None
