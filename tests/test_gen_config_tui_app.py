from unittest.mock import patch

import crsbench.genconfig_tui.app as gen_config_tui_app
import pytest
from crsbench.genconfig_tui.app import ConfigBuilderApp
from textual.widgets import Input


def test_app_constructs():
    app = ConfigBuilderApp()
    assert isinstance(app, ConfigBuilderApp)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_initial_focus_is_section_selector():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "section-list"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_first_tab_from_section_selector_goes_to_first_field():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        await pilot.press("tab")
        assert app.focused is not None
        assert app.focused.id == "field--experiment--name"


def test_app_css_prioritizes_form_width():
    assert "#form-scroll" in ConfigBuilderApp.CSS
    assert "width: 2fr;" in ConfigBuilderApp.CSS
    assert "#preview-column" in ConfigBuilderApp.CSS


def test_app_has_focus_scroll_handler():
    assert hasattr(ConfigBuilderApp, "handle_form_descendant_focus")
    assert hasattr(ConfigBuilderApp, "handle_form_descendant_blur")


def test_app_has_escape_binding_for_section_list():
    assert any(binding[0] == "escape" for binding in ConfigBuilderApp.BINDINGS)
    assert hasattr(ConfigBuilderApp, "action_focus_section_list")


def test_app_does_not_expose_load_path_input():
    assert "#loaded-path" in ConfigBuilderApp.CSS
    assert "#load-path" not in ConfigBuilderApp.CSS


def test_programmatic_main_does_not_reparse_cli_args_when_config_is_none():
    with (
        patch.object(
            gen_config_tui_app,
            "build_argument_parser",
            side_effect=AssertionError("should not parse argv"),
        ),
        patch.object(gen_config_tui_app, "ConfigBuilderApp") as mock_app_cls,
    ):
        result = gen_config_tui_app.main(config_path=None)

    assert result == 0
    mock_app_cls.assert_called_once_with(initial_path=None)
    mock_app_cls.return_value.run.assert_called_once_with()


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_app_uses_plain_text_status_widget():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        status = app.query_one("#status")
        assert status._render_markup is False


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_runtime_form_is_actually_scrollable_in_small_terminal():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        app.current_section = "runtime"
        app._refresh_ui()
        await pilot.pause()
        form = app.query_one("#form-scroll")
        assert form.max_scroll_y > 0


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_invalid_memory_format_errors_when_field_blurs():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        app.current_section = "resources"
        app._refresh_ui()
        memory = app.query_one("#field--resources--memory_per_trial", Input)
        cores = app.query_one("#field--resources--cores_per_trial", Input)
        app.set_focus(memory)
        await pilot.pause()
        memory.value = "123"
        await pilot.pause()
        app.set_focus(cores)
        await pilot.pause()
        assert "Invalid memory format" in app.status_text
