from unittest.mock import patch

import crsbench.genconfig_tui.app as gen_config_tui_app
import pytest
from crsbench.genconfig_tui.app import ConfigBuilderApp
from crsbench.genconfig_tui.core import build_grouped_config, dump_yaml
from rich.markup import escape
from textual.binding import Binding
from textual.widgets import Button, Input, OptionList, Select, SelectionList, TextArea
from textual.widgets._footer import FooterKey


def _valid_loaded_yaml_with_unknown_cloud_block() -> str:
    grouped = build_grouped_config(
        {
            "experiment": {
                "name": "demo-exp",
                "task": "bugfixing",
                "benchmark_suite": "sanity",
                "mode": "delta",
            },
            "runtime": {
                "trials": 1,
                "max_total_time": 4001,
                "build_timeout": 1200,
                "run_timeout": 600,
                "verify_timeout": 600,
                "skip_litellm": True,
                "pov_enabled": True,
                "pov_max_variants_per_cpv": 1,
            },
            "storage": {
                "experiment_filestore": "/tmp/exp",
                "report_filestore": "/tmp/report",
            },
            "crs_compose": {
                "service_name": "crs-libfuzzer",
                "service_num_cores": 2,
                "infra_shared": True,
            },
            "cloud": {},
        }
    )
    grouped["cloud"] = {"custom_block": {"keep": "me"}}
    return dump_yaml(grouped)


def _valid_loaded_yaml_with_cloud_boot_disk_type(boot_disk_type: str) -> str:
    grouped = build_grouped_config(
        {
            "experiment": {
                "name": "demo-exp",
                "task": "bugfixing",
                "benchmark_suite": "sanity",
                "mode": "delta",
            },
            "runtime": {
                "trials": 1,
                "max_total_time": 4001,
                "build_timeout": 1200,
                "run_timeout": 600,
                "verify_timeout": 600,
                "skip_litellm": True,
                "pov_enabled": True,
                "pov_max_variants_per_cpv": 1,
            },
            "storage": {
                "experiment_filestore": "/tmp/exp",
                "report_filestore": "/tmp/report",
            },
            "crs_compose": {
                "service_name": "crs-libfuzzer",
                "service_num_cores": 2,
                "infra_shared": True,
            },
            "cloud": {
                "enabled": True,
                "provider_project": "aixcc-426805",
                "provider_region": "us-east5",
                "provider_ssh_via_iap": True,
                "profile_machine_type": "n2d-standard-16",
                "profile_boot_disk_size_gb": 100,
                "profile_boot_disk_type": boot_disk_type,
                "profile_image": "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64",
                "profile_service_account_email": "153298433405-compute@developer.gserviceaccount.com",
                "profile_owner_label": "yufu",
                "worker_count": 1,
                "evaluator_count": 1,
                "worker_region": "us-east5",
                "evaluator_region": "us-east5",
            },
        }
    )
    grouped["cloud"]["providers"]["gce"]["instance_profiles"] = {
        "gce-orchestrator-n2d": {},
        "gce-worker-n2d": {},
        "gce-evaluator-n2d": {},
    }
    return dump_yaml(grouped)


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
async def test_left_arrow_on_input_start_focuses_section_selector():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        await pilot.press("tab")

        name = app.query_one("#field--experiment--name", Input)
        assert app.focused is name
        name.cursor_position = 0

        await pilot.press("left")

        assert app.focused is not None
        assert app.focused.id == "section-list"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_cloud_collection_blocks_render_when_cloud_enabled():
    app = ConfigBuilderApp()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()

        app.form_state["cloud"]["enabled"] = True
        app.current_section = "cloud"
        app._sync_widgets_from_state()
        app._refresh_ui()
        await pilot.pause()

        assert app.query_one("#cloud-block--provider_regions").display is True
        assert app.query_one("#cloud-block--instance_profiles").display is True
        assert app.query_one("#cloud-block--worker_placements").display is True
        assert app.query_one("#cloud-block--evaluator_placements").display is True
        assert isinstance(app.query_one("#cloud-add--provider_regions", Button), Button)
        assert isinstance(
            app.query_one("#cloud-list--instance_profiles", OptionList), OptionList
        )


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_cloud_region_add_remove_updates_state_and_preview():
    app = ConfigBuilderApp()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()

        app.form_state["cloud"]["enabled"] = True
        app.current_section = "cloud"
        app._sync_widgets_from_state()
        app._refresh_ui()
        await pilot.pause()

        add_button = app.query_one("#cloud-add--provider_regions", Button)
        add_button.press()
        await pilot.pause()

        assert app.form_state["cloud"]["provider_regions"] == [""]
        regions_list = app.query_one("#cloud-list--provider_regions", OptionList)
        assert regions_list.option_count == 1

        region_input = app.query_one("#cloud-detail--provider_regions--value", Input)
        app.set_focus(region_input)
        await pilot.press("u", "s", "-", "e", "a", "s", "t", "5")

        assert app.form_state["cloud"]["provider_regions"] == ["us-east5"]
        assert "- us-east5" in app.query_one("#section-preview", TextArea).text

        remove_button = app.query_one("#cloud-remove--provider_regions", Button)
        remove_button.press()
        await pilot.pause()

        assert app.form_state["cloud"]["provider_regions"] == []
        assert regions_list.option_count == 0


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_cloud_instance_profile_add_and_edit_updates_preview():
    app = ConfigBuilderApp()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()

        app.form_state["cloud"]["enabled"] = True
        app.current_section = "cloud"
        app._sync_widgets_from_state()
        app._refresh_ui()
        await pilot.pause()

        app.query_one("#cloud-add--instance_profiles", Button).press()
        await pilot.pause()

        assert app.form_state["cloud"]["instance_profiles"] == [
            {"name": "new-profile-1"}
        ]

        machine_type = app.query_one(
            "#cloud-detail--instance_profiles--machine_type", Input
        )
        app.set_focus(machine_type)
        await pilot.press(
            "n", "2", "d", "-", "s", "t", "a", "n", "d", "a", "r", "d", "-", "3", "2"
        )

        assert app.form_state["cloud"]["instance_profiles"] == [
            {"name": "new-profile-1", "machine_type": "n2d-standard-32"}
        ]
        final_preview = app.query_one("#final-preview", TextArea).text
        assert "new-profile-1:" in final_preview
        assert "machine_type: n2d-standard-32" in final_preview


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_cloud_worker_placement_add_and_edit_updates_preview():
    app = ConfigBuilderApp()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()

        app.form_state["cloud"]["enabled"] = True
        app.current_section = "cloud"
        app._sync_widgets_from_state()
        app._refresh_ui()
        await pilot.pause()

        app.query_one("#cloud-add--worker_placements", Button).press()
        await pilot.pause()

        region_input = app.query_one("#cloud-detail--worker_placements--region", Input)
        app.set_focus(region_input)
        await pilot.press("u", "s", "-", "e", "a", "s", "t", "1")

        count_input = app.query_one("#cloud-detail--worker_placements--count", Input)
        app.set_focus(count_input)
        await pilot.press("2")

        assert app.form_state["cloud"]["worker_placements"] == [
            {"region": "us-east1", "count": 2}
        ]
        final_preview = app.query_one("#final-preview", TextArea).text
        assert "placements:" in final_preview
        assert "region: us-east1" in final_preview
        assert "count: 2" in final_preview


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_right_arrow_on_input_end_focuses_section_preview():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        await pilot.press("tab")

        name = app.query_one("#field--experiment--name", Input)
        assert app.focused is name
        name.cursor_position = len(name.value)

        await pilot.press("right")

        assert app.focused is not None
        assert app.focused.id == "section-preview"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_right_arrow_on_section_selector_focuses_first_visible_field():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "section-list"

        await pilot.press("right")

        assert app.focused is not None
        assert app.focused.id == "field--experiment--name"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_left_arrow_on_section_preview_focuses_remembered_field():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()

        task = app.query_one("#field--experiment--task", Select)
        app.set_focus(task)
        await pilot.pause()

        preview = app.query_one("#section-preview", TextArea)
        app.set_focus(preview)
        await pilot.pause()

        await pilot.press("left")

        assert app.focused is task


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_down_arrow_in_section_preview_moves_cursor_before_handoff():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()

        preview = app.query_one("#section-preview", TextArea)
        app.set_focus(preview)
        await pilot.pause()
        preview.cursor_location = (0, 0)

        await pilot.press("down")

        assert app.focused is preview
        assert preview.cursor_location[0] == 1


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_down_arrow_on_section_preview_focuses_final_preview_at_last_line():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()

        preview = app.query_one("#section-preview", TextArea)
        preview.cursor_location = (preview.document.line_count - 1, 0)
        app.set_focus(preview)
        await pilot.pause()

        await pilot.press("down")

        assert app.focused is not None
        assert app.focused.id == "final-preview"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_up_arrow_in_final_preview_moves_cursor_before_handoff():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()

        final_preview = app.query_one("#final-preview", TextArea)
        final_preview.cursor_location = (1, 0)
        app.set_focus(final_preview)
        await pilot.pause()

        await pilot.press("up")

        assert app.focused is final_preview
        assert final_preview.cursor_location[0] == 0


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_up_arrow_on_final_preview_focuses_section_preview_at_first_line():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()

        final_preview = app.query_one("#final-preview", TextArea)
        final_preview.cursor_location = (0, 0)
        app.set_focus(final_preview)
        await pilot.pause()

        await pilot.press("up")

        assert app.focused is not None
        assert app.focused.id == "section-preview"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_left_arrow_on_final_preview_focuses_remembered_field():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()

        task = app.query_one("#field--experiment--task", Select)
        app.set_focus(task)
        await pilot.pause()

        final_preview = app.query_one("#final-preview", TextArea)
        app.set_focus(final_preview)
        await pilot.pause()

        await pilot.press("left")

        assert app.focused is task


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_left_right_arrows_keep_normal_cursor_movement_inside_input():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        await pilot.press("tab")

        name = app.query_one("#field--experiment--name", Input)
        assert app.focused is name
        name.cursor_position = 3

        await pilot.press("left")
        assert app.focused is name
        assert name.cursor_position == 2

        await pilot.press("right")
        assert app.focused is name
        assert name.cursor_position == 3


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
    assert "min-width: 36;" in ConfigBuilderApp.CSS
    assert "#preview-column" in ConfigBuilderApp.CSS
    assert "width: 80;" in ConfigBuilderApp.CSS
    assert "min-width: 36;" in ConfigBuilderApp.CSS


def test_app_has_focus_scroll_handler():
    assert hasattr(ConfigBuilderApp, "handle_form_descendant_focus")
    assert hasattr(ConfigBuilderApp, "handle_form_descendant_blur")


def test_app_has_escape_binding_for_section_list():
    bindings = [
        binding if isinstance(binding, Binding) else Binding(*binding)
        for binding in ConfigBuilderApp.BINDINGS
    ]
    assert any(binding.key == "escape" for binding in bindings)
    assert hasattr(ConfigBuilderApp, "action_focus_section_list")


def test_app_has_ctrl_s_binding_for_save():
    bindings = [
        binding if isinstance(binding, Binding) else Binding(*binding)
        for binding in ConfigBuilderApp.BINDINGS
    ]
    assert any(binding.key == "ctrl+s" for binding in bindings)


def test_app_has_nano_style_undo_redo_bindings():
    bindings = [
        binding if isinstance(binding, Binding) else Binding(*binding)
        for binding in ConfigBuilderApp.BINDINGS
    ]
    assert any(binding.key == "alt+u" and binding.priority for binding in bindings)
    assert any(binding.key == "alt+e" and binding.priority for binding in bindings)


def test_app_has_alt_s_binding_for_focus_cycle():
    bindings = [
        binding if isinstance(binding, Binding) else Binding(*binding)
        for binding in ConfigBuilderApp.BINDINGS
    ]
    assert any(binding.key == "alt+s" and binding.priority for binding in bindings)


def test_app_has_alt_a_binding_for_reverse_focus_cycle():
    bindings = [
        binding if isinstance(binding, Binding) else Binding(*binding)
        for binding in ConfigBuilderApp.BINDINGS
    ]
    assert any(binding.key == "alt+a" and binding.priority for binding in bindings)


def test_app_has_ctrl_q_binding_for_guarded_quit():
    bindings = [
        binding if isinstance(binding, Binding) else Binding(*binding)
        for binding in ConfigBuilderApp.BINDINGS
    ]
    assert any(
        binding.key == "ctrl+q"
        and binding.action == "quit_with_confirm"
        and binding.show
        and binding.priority
        for binding in bindings
    )


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_footer_places_ctrl_q_helper_on_right():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()

        quit_keys = [
            key
            for key in app.query(FooterKey)
            if isinstance(key, FooterKey) and key.action == "quit_with_confirm"
        ]

        assert len(quit_keys) == 1
        assert quit_keys[0].has_class("-right-helper")


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_footer_right_helpers_do_not_overlap():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()

        helpers = {
            key.action: key
            for key in app.query(FooterKey)
            if isinstance(key, FooterKey)
            and key.action in {"quit_with_confirm", "command_palette"}
        }

        quit_key = helpers["quit_with_confirm"]
        palette_key = helpers["command_palette"]

        assert quit_key.region.right <= palette_key.region.x or (
            palette_key.region.right <= quit_key.region.x
        )


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


def test_write_action_opens_save_path_prompt():
    app = ConfigBuilderApp()
    with patch.object(app, "push_screen") as mock_push:
        app.action_write_timestamped()

    screen = mock_push.call_args.args[0]
    assert isinstance(screen, gen_config_tui_app.SavePathScreen)
    assert screen.default_path == "gen-experiment-config.yaml"


def test_quit_action_exits_immediately_without_session_edits():
    app = ConfigBuilderApp()
    with (
        patch.object(app, "exit") as mock_exit,
        patch.object(app, "push_screen") as mock_push,
    ):
        app.action_quit_with_confirm()

    mock_exit.assert_called_once_with()
    mock_push.assert_not_called()


def test_quit_action_prompts_after_session_edits():
    app = ConfigBuilderApp()
    app._has_session_edits = True
    with (
        patch.object(app, "exit") as mock_exit,
        patch.object(app, "push_screen") as mock_push,
    ):
        app.action_quit_with_confirm()

    mock_exit.assert_not_called()
    screen = mock_push.call_args.args[0]
    assert isinstance(screen, gen_config_tui_app.QuitConfirmScreen)


def test_confirmed_quit_exits():
    app = ConfigBuilderApp()
    with patch.object(app, "exit") as mock_exit:
        app._handle_quit_confirmed("quit")

    mock_exit.assert_called_once_with()


def test_canceled_quit_does_not_exit():
    app = ConfigBuilderApp()
    with patch.object(app, "exit") as mock_exit:
        app._handle_quit_confirmed("cancel")

    mock_exit.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_quit_confirm_left_right_moves_between_buttons():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        app.push_screen(gen_config_tui_app.QuitConfirmScreen())
        await pilot.pause()

        cancel = app.screen.query_one("#cancel-quit")
        confirm = app.screen.query_one("#confirm-quit")
        app.set_focus(cancel)
        await pilot.pause()

        await pilot.press("right")
        assert app.focused is confirm

        await pilot.press("left")
        assert app.focused is cancel


def test_resolve_requested_output_path_uses_current_dir_for_relative_prefixes(tmp_path):
    app = ConfigBuilderApp()
    with patch.object(gen_config_tui_app.Path, "cwd", return_value=tmp_path):
        path = app._resolve_requested_output_path("saved")

    assert path == tmp_path / "saved.yaml"


def test_resolve_requested_output_path_preserves_absolute_yaml_paths(tmp_path):
    app = ConfigBuilderApp()
    path = app._resolve_requested_output_path(str(tmp_path / "saved.yaml"))
    assert path == tmp_path / "saved.yaml"


def test_resolve_requested_output_path_appends_yaml_to_absolute_prefixes(tmp_path):
    app = ConfigBuilderApp()
    path = app._resolve_requested_output_path(str(tmp_path / "saved"))
    assert path == tmp_path / "saved.yaml"


def test_resolve_requested_output_path_rejects_non_yaml_suffixes():
    app = ConfigBuilderApp()
    with pytest.raises(ValueError, match=r"\.yaml or \.yml"):
        app._resolve_requested_output_path("saved.txt")


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
async def test_fixing_field_does_not_clear_unrelated_status_message():
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

        app._set_status("Write failed: disk full")
        app.set_focus(memory)
        await pilot.pause()
        memory.value = "1G"
        await pilot.pause()
        app.set_focus(cores)
        await pilot.pause()
        assert app.status_text == "Write failed: disk full"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_fixing_one_memory_field_does_not_clear_another_memory_field_error():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        app.current_section = "crs_compose"
        app._refresh_ui()
        service_mem_limit = app.query_one(
            "#field--crs_compose--service_mem_limit", Input
        )
        infra_mem_limit = app.query_one("#field--crs_compose--infra_mem_limit", Input)
        work_dir = app.query_one("#field--crs_compose--work_dir", Input)

        app.set_focus(service_mem_limit)
        await pilot.pause()
        service_mem_limit.value = "123"
        await pilot.pause()
        app.set_focus(infra_mem_limit)
        await pilot.pause()
        assert "Invalid memory format" in app.status_text

        infra_mem_limit.value = "456"
        await pilot.pause()
        app.set_focus(work_dir)
        await pilot.pause()
        assert "Invalid memory format" in app.status_text

        app.set_focus(service_mem_limit)
        await pilot.pause()
        service_mem_limit.value = "1G"
        await pilot.pause()
        app.set_focus(work_dir)
        await pilot.pause()
        assert "Invalid memory format" in app.status_text


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
async def test_write_requested_path_uses_current_dir_for_relative_prefixes(tmp_path):
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        grouped = {"experiment": {"name": "demo-exp"}}
        with (
            patch.object(app, "_validated_config", return_value=grouped),
            patch.object(gen_config_tui_app.Path, "cwd", return_value=tmp_path),
            patch.object(
                gen_config_tui_app,
                "write_grouped_config",
                return_value=tmp_path / "saved.yaml",
            ) as mock_write,
        ):
            app._write_to_requested_path("saved")
            await pilot.pause()
            mock_write.assert_called_once_with(
                grouped, output_path=tmp_path / "saved.yaml"
            )
            assert "Wrote config to" in app.status_text


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_save_as_uses_loaded_yaml_as_preservation_base(tmp_path):
    source = tmp_path / "source.yaml"
    source.write_text(
        "# top comment\nexperiment:\n  # keep me\n  name: old-name\n",
        encoding="utf-8",
    )

    app = ConfigBuilderApp(initial_path=source)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        name = app.query_one("#field--experiment--name", Input)
        app.set_focus(name)
        await pilot.pause()
        name.value = "new-name"
        await pilot.pause()

        app._write_to_requested_path(str(tmp_path / "copy.yaml"))
        await pilot.pause()

    written = (tmp_path / "copy.yaml").read_text(encoding="utf-8")
    assert "# top comment" in written
    assert "# keep me" in written
    assert "name: new-name" in written


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_save_as_uses_in_memory_loaded_yaml_when_disk_file_changes(tmp_path):
    source = tmp_path / "source.yaml"
    source.write_text(
        "# loaded comment\nexperiment:\n  # keep loaded\n  name: old-name\n",
        encoding="utf-8",
    )

    app = ConfigBuilderApp(initial_path=source)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        source.write_text(
            "# disk changed later\nexperiment:\n  # changed on disk\n  name: disk-name\n",
            encoding="utf-8",
        )

        name = app.query_one("#field--experiment--name", Input)
        app.set_focus(name)
        await pilot.pause()
        name.value = "new-name"
        await pilot.pause()

        app._write_to_requested_path(str(tmp_path / "copy.yaml"))
        await pilot.pause()

    written = (tmp_path / "copy.yaml").read_text(encoding="utf-8")
    assert "# loaded comment" in written
    assert "# keep loaded" in written
    assert "# disk changed later" not in written
    assert "# changed on disk" not in written
    assert "name: new-name" in written


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_validate_loaded_unknown_cloud_block_ignores_preserved_extras(tmp_path):
    source = tmp_path / "source.yaml"
    source.write_text(_valid_loaded_yaml_with_unknown_cloud_block(), encoding="utf-8")

    app = ConfigBuilderApp(initial_path=source)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()

        app.action_validate_config()
        await pilot.pause()

        assert app.status_text.startswith("Validation passed for ")


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_save_as_preserves_unknown_cloud_block_without_injecting_defaults(
    tmp_path,
):
    source = tmp_path / "source.yaml"
    source.write_text(_valid_loaded_yaml_with_unknown_cloud_block(), encoding="utf-8")

    app = ConfigBuilderApp(initial_path=source)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()

        app._write_to_requested_path(str(tmp_path / "copy.yaml"))
        await pilot.pause()

    written = (tmp_path / "copy.yaml").read_text(encoding="utf-8")
    assert "custom_block:\n    keep: me" in written
    assert "orchestrator:" not in written
    assert "workers:" not in written
    assert "evaluators:" not in written


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_save_as_updates_cloud_boot_disk_type_field(tmp_path):
    source = tmp_path / "source.yaml"
    source.write_text(
        _valid_loaded_yaml_with_cloud_boot_disk_type("pd-balanced"),
        encoding="utf-8",
    )

    app = ConfigBuilderApp(initial_path=source)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()

        app.current_section = "cloud"
        app._refresh_ui()
        await pilot.pause()

        disk_type = app.query_one("#field--cloud--profile_boot_disk_type", Input)
        app.set_focus(disk_type)
        await pilot.pause()
        disk_type.value = "pd-ssd"
        await pilot.pause()

        app._write_to_requested_path(str(tmp_path / "copy.yaml"))
        await pilot.pause()

    written = (tmp_path / "copy.yaml").read_text(encoding="utf-8")
    assert "boot_disk_type: pd-ssd" in written
    assert "boot_disk_type: pd-balanced" not in written


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_section_preview_scrolls_to_edited_input_field_value(tmp_path):
    source = tmp_path / "source.yaml"
    source.write_text(
        _valid_loaded_yaml_with_cloud_boot_disk_type("pd-balanced"),
        encoding="utf-8",
    )

    app = ConfigBuilderApp(initial_path=source)
    async with app.run_test(size=(100, 12)) as pilot:
        await pilot.pause()

        app.current_section = "cloud"
        app._refresh_ui()
        await pilot.pause()

        preview = app.query_one("#section-preview", TextArea)
        service_account = app.query_one(
            "#field--cloud--profile_service_account_email", Input
        )
        updated_value = "updated-service-account@example.com"

        app.set_focus(service_account)
        await pilot.pause()
        service_account.value = updated_value
        await pilot.pause()

        matching_row = next(
            index
            for index, line in enumerate(preview.text.splitlines())
            if updated_value in line
        )

        assert preview.cursor_location[0] == matching_row
        assert preview.scroll_offset.y > 0


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_update_loaded_uses_loaded_yaml_as_preservation_base(tmp_path):
    source = tmp_path / "source.yaml"
    source.write_text(
        "# top comment\nexperiment:\n  # keep me\n  name: old-name\n",
        encoding="utf-8",
    )

    app = ConfigBuilderApp(initial_path=source)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        name = app.query_one("#field--experiment--name", Input)
        app.set_focus(name)
        await pilot.pause()
        name.value = "new-name"
        await pilot.pause()

        app.action_update_loaded()
        await pilot.pause()

    written = source.read_text(encoding="utf-8")
    assert "# top comment" in written
    assert "# keep me" in written
    assert "name: new-name" in written


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_failed_reload_clears_loaded_yaml_preservation_base(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.yaml"
    source.write_text(
        "experiment:\n  name: ok\n",
        encoding="utf-8",
    )

    app = ConfigBuilderApp(initial_path=source)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        assert app._loaded_roundtrip_yaml is not None

        monkeypatch.setattr(
            gen_config_tui_app,
            "read_grouped_config",
            lambda _: (_ for _ in ()).throw(ValueError("boom")),
        )
        app.action_reload_file()
        await pilot.pause()

        assert app._loaded_roundtrip_yaml is None
        assert app.section_extras == {}


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_update_loaded_fails_after_reload_clears_preservation_base(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.yaml"
    original = "# top comment\nexperiment:\n  # keep me\n  name: old-name\n"
    source.write_text(original, encoding="utf-8")

    app = ConfigBuilderApp(initial_path=source)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()

        name = app.query_one("#field--experiment--name", Input)
        app.set_focus(name)
        await pilot.pause()
        name.value = "new-name"
        await pilot.pause()

        monkeypatch.setattr(
            gen_config_tui_app,
            "read_grouped_config",
            lambda _: (_ for _ in ()).throw(ValueError("boom")),
        )
        app.action_reload_file()
        await pilot.pause()

        app.action_update_loaded()
        await pilot.pause()

        assert app._loaded_roundtrip_yaml is None
        assert app.loaded_path == source
        assert app.status_text == (
            "Update failed: Loaded file can no longer be updated in place until it is "
            "reloaded successfully"
        )

    assert source.read_text(encoding="utf-8") == original


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_invalid_save_path_keeps_save_modal_open():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        app.action_write_timestamped()
        await pilot.pause()
        assert isinstance(app.screen, gen_config_tui_app.SavePathScreen)

        save_input = app.screen.query_one("#save-path-input", Input)
        save_input.value = "saved.txt"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, gen_config_tui_app.SavePathScreen)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_ctrl_s_opens_save_as_when_no_file_is_loaded():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()

        assert isinstance(app.screen, gen_config_tui_app.SavePathScreen)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_ctrl_s_updates_loaded_file_when_config_is_loaded(tmp_path):
    source = tmp_path / "source.yaml"
    source.write_text("experiment:\n  name: old-name\n", encoding="utf-8")

    app = ConfigBuilderApp(initial_path=source)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()

        name = app.query_one("#field--experiment--name", Input)
        app.set_focus(name)
        await pilot.pause()
        name.value = "new-name"
        await pilot.pause()

        await pilot.press("ctrl+s")
        await pilot.pause()

    written = source.read_text(encoding="utf-8")
    assert "name: new-name" in written


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_alt_u_undoes_last_field_edit():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()

        name = app.query_one("#field--experiment--name", Input)
        original_value = app.form_state["experiment"]["name"]
        app.set_focus(name)
        await pilot.pause()

        name.value = "edited-name"
        await pilot.pause()
        assert app.form_state["experiment"]["name"] == "edited-name"

        await pilot.press("alt+u")
        await pilot.pause()

        assert app.form_state["experiment"]["name"] == original_value
        assert name.value == original_value


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_alt_e_redoes_last_undone_field_edit():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()

        name = app.query_one("#field--experiment--name", Input)
        app.set_focus(name)
        await pilot.pause()

        name.value = "edited-name"
        await pilot.pause()
        await pilot.press("alt+u")
        await pilot.pause()

        await pilot.press("alt+e")
        await pilot.pause()

        assert app.form_state["experiment"]["name"] == "edited-name"
        assert name.value == "edited-name"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_alt_s_cycles_focus_through_panes_and_remembers_last_field():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "section-list"

        await pilot.press("alt+s")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "field--experiment--name"

        await pilot.press("alt+s")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "section-preview"

        await pilot.press("alt+s")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "final-preview"

        await pilot.press("alt+s")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "section-list"

        task = app.query_one("#field--experiment--task", Select)
        app.set_focus(task)
        await pilot.pause()

        await pilot.press("alt+s")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "section-preview"

        await pilot.press("alt+s")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "final-preview"

        await pilot.press("alt+s")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "section-list"

        await pilot.press("alt+s")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "field--experiment--task"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_alt_s_uses_first_field_after_section_change():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()

        task = app.query_one("#field--experiment--task", Select)
        app.set_focus(task)
        await pilot.pause()

        await pilot.press("alt+s", "alt+s", "alt+s")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "section-list"

        section_list = app.query_one("#section-list")
        section_list.highlighted = 1
        await pilot.pause()

        await pilot.press("alt+s")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "field--runtime--trials"

        await pilot.press("escape")
        await pilot.pause()
        section_list.highlighted = 0
        await pilot.pause()

        await pilot.press("alt+s")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "field--experiment--name"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_alt_a_cycles_focus_backward_through_panes():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "section-list"

        await pilot.press("alt+a")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "final-preview"

        await pilot.press("alt+a")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "section-preview"

        await pilot.press("alt+a")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "field--experiment--name"

        await pilot.press("alt+a")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "section-list"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_alt_a_uses_remembered_field_when_cycling_backward():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()

        task = app.query_one("#field--experiment--task", Select)
        app.set_focus(task)
        await pilot.pause()

        await pilot.press("alt+a")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "section-list"

        await pilot.press("alt+a")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "final-preview"

        await pilot.press("alt+a")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "section-preview"

        await pilot.press("alt+a")
        await pilot.pause()
        assert app.focused is not None
        assert app.focused.id == "field--experiment--task"


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_toolbar_buttons_are_removed():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()

        assert not list(app.query("#reload-button"))
        assert not list(app.query("#validate-button"))
        assert not list(app.query("#write-button"))
        assert not list(app.query("#update-button"))


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_escape_still_focuses_section_list_from_input():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()

        name = app.query_one("#field--experiment--name", Input)
        app.set_focus(name)
        await pilot.pause()
        assert app.focused is name

        await pilot.press("escape")
        await pilot.pause()

        assert app.focused is not None
        assert app.focused.id == "section-list"


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


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_q_opens_quit_confirm_after_session_edit():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()

        name = app.query_one("#field--experiment--name", Input)
        app.set_focus(name)
        await pilot.pause()
        name.value = "edited-name"
        await pilot.pause()

        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()

        assert isinstance(app.screen, gen_config_tui_app.QuitConfirmScreen)


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_ctrl_q_opens_quit_confirm_from_focused_input_after_session_edit():
    app = ConfigBuilderApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()

        name = app.query_one("#field--experiment--name", Input)
        app.set_focus(name)
        await pilot.pause()
        name.value = "edited-name"
        await pilot.pause()

        await pilot.press("ctrl+q")
        await pilot.pause()

        assert isinstance(app.screen, gen_config_tui_app.QuitConfirmScreen)
