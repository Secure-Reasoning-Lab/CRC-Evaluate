from unittest.mock import patch

import crsbench.genconfig_tui.app as gen_config_tui_app
import pytest
from crsbench.genconfig_tui.app import ConfigBuilderApp
from rich.markup import escape
from textual.widgets import Input, Select, SelectionList


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


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_down_arrow_moves_focus_to_next_visible_field():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        await pilot.press("tab")
        assert app.focused is not None
        assert app.focused.id == "field--experiment--name"

        await pilot.press("down")

        assert app.focused is not None
        assert app.focused.id == "field--experiment--task"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_up_arrow_moves_focus_to_previous_visible_field():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        await pilot.press("tab")
        await pilot.press("down")
        assert app.focused is not None
        assert app.focused.id == "field--experiment--task"

        await pilot.press("up")

        assert app.focused is not None
        assert app.focused.id == "field--experiment--name"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_down_arrow_moves_within_sanitizer_list_before_leaving_field():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        await pilot.press("tab", "down", "down", "down", "down", "down")

        sanitizers = app.query_one("#field--experiment--sanitizers", SelectionList)
        assert app.focused is sanitizers
        assert sanitizers.highlighted == 0

        await pilot.press("down")

        assert app.focused is sanitizers
        assert sanitizers.highlighted == 1


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_up_arrow_on_first_sanitizer_item_moves_to_previous_field():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        await pilot.press("tab", "down", "down", "down", "down", "down")

        sanitizers = app.query_one("#field--experiment--sanitizers", SelectionList)
        assert app.focused is sanitizers
        assert sanitizers.highlighted == 0

        await pilot.press("up")

        assert app.focused is not None
        assert app.focused.id == "field--experiment--mode"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_down_arrow_on_last_sanitizer_item_moves_to_next_field():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        await pilot.press("tab", "down", "down", "down", "down", "down")

        sanitizers = app.query_one("#field--experiment--sanitizers", SelectionList)
        sanitizers.highlighted = sanitizers.option_count - 1
        await pilot.pause()

        await pilot.press("down")

        assert app.focused is not None
        assert app.focused.id == "field--experiment--only_cpv_harnesses"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_enter_on_select_opens_overlay_without_moving_to_next_field():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        await pilot.press("tab", "down")

        task = app.query_one("#field--experiment--task", Select)
        benchmark_suite = app.query_one("#field--experiment--benchmark_suite", Input)
        assert app.focused is task

        await pilot.press("enter")

        assert app.focused is not benchmark_suite
        assert task.has_class("-expanded")


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
async def test_initial_missing_path_reports_error_and_keeps_ui_running(tmp_path):
    missing_path = tmp_path / "missing.yaml"
    app = ConfigBuilderApp(initial_path=missing_path)

    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()

        assert app.focused is not None
        assert app.focused.id == "section-list"
        assert app.loaded_path is None
        assert "No config loaded" in app._loaded_path_text()


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
        assert memory.has_class("invalid")


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_valid_blur_does_not_replace_status_with_validated_message():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        app.current_section = "resources"
        app._refresh_ui()
        memory = app.query_one("#field--resources--memory_per_trial", Input)
        cores = app.query_one("#field--resources--cores_per_trial", Input)
        app.set_focus(memory)
        await pilot.pause()
        memory.value = "1G"
        await pilot.pause()
        app.set_focus(cores)
        await pilot.pause()
        assert "Validated" not in app.status_text
        assert not memory.has_class("invalid")


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_invalid_field_state_clears_after_valid_value_blurs():
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
        assert memory.has_class("invalid")

        app.set_focus(memory)
        await pilot.pause()
        memory.value = "1G"
        await pilot.pause()
        app.set_focus(cores)
        await pilot.pause()
        assert not memory.has_class("invalid")
        assert "Invalid memory format" not in app.status_text


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_invalid_field_does_not_mark_other_blurred_fields_invalid():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        app.current_section = "resources"
        app._refresh_ui()
        memory = app.query_one("#field--resources--memory_per_trial", Input)
        cores = app.query_one("#field--resources--cores_per_trial", Input)
        cpu_tag = app.query_one("#field--resources--cpu_tag", Input)

        app.set_focus(memory)
        await pilot.pause()
        memory.value = "123"
        await pilot.pause()
        app.set_focus(cores)
        await pilot.pause()
        assert memory.has_class("invalid")

        cores.value = "2"
        await pilot.pause()
        app.set_focus(cpu_tag)
        await pilot.pause()
        assert not cores.has_class("invalid")


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_blur_validation_does_not_run_whole_config_schema():
    app = ConfigBuilderApp()
    with patch.object(
        gen_config_tui_app,
        "validate_grouped_config",
        side_effect=AssertionError("whole-config validation should not run on blur"),
    ):
        async with app.run_test(size=(100, 24)) as pilot:
            app.current_section = "resources"
            app._refresh_ui()
            memory = app.query_one("#field--resources--memory_per_trial", Input)
            cores = app.query_one("#field--resources--cores_per_trial", Input)
            app.set_focus(memory)
            await pilot.pause()
            memory.value = "1G"
            await pilot.pause()
            app.set_focus(cores)
            await pilot.pause()
            assert not memory.has_class("invalid")
            assert (
                "whole-config validation should not run on blur" not in app.status_text
            )


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_validate_action_escapes_markup_in_error_notifications():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        message = "boom [input_type=dict] {'defaults': {'instance_type': 'worker-n2d'}}"
        with (
            patch.object(app, "_validated_config", side_effect=ValueError(message)),
            patch.object(app, "notify") as mock_notify,
        ):
            app.action_validate_config()
            await pilot.pause()
            assert "Validation failed:" in app.status_text
            mock_notify.assert_called_once_with(escape(message), severity="error")
