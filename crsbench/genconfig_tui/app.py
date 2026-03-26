from __future__ import annotations

from argparse import ArgumentParser
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

from rich.markup import escape
from textual import events, on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalGroup, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    OptionList,
    Select,
    SelectionList,
    Static,
    Switch,
    TextArea,
)

from crsbench.genconfig_tui.core import (
    SECTION_ORDER,
    _load_roundtrip_document,
    build_grouped_config,
    dump_yaml,
    load_state_from_grouped_config,
    read_grouped_config,
    write_grouped_config,
)
from crsbench.genconfig_tui.field_specs import (
    SECTION_SPECS,
    FieldSpec,
    default_form_state,
)
from crsbench.genconfig_tui.schema_bridge import validate_grouped_config
from crsbench.genconfig_tui.validators import validate_field_value


def _widget_id(section: str, key: str) -> str:
    return f"field--{section}--{key}"


def _wrap_id(section: str, key: str) -> str:
    return f"field-wrap--{section}--{key}"


NotificationSeverity = Literal["information", "warning", "error"]


def resolve_requested_output_path(raw_path: str, cwd: Path | None = None) -> Path:
    path_text = raw_path.strip()
    if not path_text:
        raise ValueError("Enter a filename or absolute path to save the config.")
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = (cwd or Path.cwd()) / path
    if path.suffix == "":
        path = path.with_suffix(".yaml")
    elif path.suffix not in {".yaml", ".yml"}:
        raise ValueError("Saved config path must end with .yaml or .yml.")
    return path


class SavePathScreen(ModalScreen[str | None]):
    CSS = """
    SavePathScreen {
        align: center middle;
    }

    #save-dialog {
        width: 72;
        max-width: 100%;
        height: auto;
        padding: 1 2;
        border: solid $panel;
        background: $surface;
    }

    #save-help {
        margin: 1 0;
        color: $text-muted;
    }

    #save-actions {
        height: auto;
        align: right middle;
        margin-top: 1;
    }

    #save-path-input {
        width: 1fr;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, default_path: str) -> None:
        super().__init__()
        self.default_path = default_path

    def compose(self) -> ComposeResult:
        with Vertical(id="save-dialog"):
            yield Label("Save config", classes="panel-title")
            yield Static(
                "Enter a relative filename to save in the current directory, "
                "or an absolute path. Bare prefixes are saved as .yaml.",
                id="save-help",
            )
            yield Input(self.default_path, id="save-path-input")
            with Horizontal(id="save-actions"):
                yield Button("Cancel", id="cancel-save")
                yield Button("Save", id="confirm-save", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#save-path-input", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _submit_path(self, raw_path: str) -> None:
        try:
            resolve_requested_output_path(raw_path)
        except ValueError as exc:
            self.notify(escape(str(exc)), severity="error")
            self.query_one("#save-path-input", Input).focus()
            return
        self.dismiss(raw_path)

    @on(Input.Submitted, "#save-path-input")
    def handle_submit(self, event: Input.Submitted) -> None:
        self._submit_path(event.value)

    @on(Button.Pressed, "#cancel-save")
    def handle_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#confirm-save")
    def handle_confirm(self) -> None:
        value = self.query_one("#save-path-input", Input).value
        self._submit_path(value)


class ConfigBuilderApp(App[None]):
    CSS = """
    Screen {
        layout: vertical;
    }

    #toolbar {
        height: auto;
        padding: 0 1;
        border-bottom: solid $panel;
    }

    #toolbar-row {
        height: auto;
        align: left middle;
    }

    #loaded-path {
        width: 1fr;
        margin-right: 1;
        color: $text-muted;
    }

    #body {
        height: 1fr;
    }

    #section-list {
        width: 20;
        margin-right: 1;
    }

    #form-scroll {
        width: 2fr;
        padding-right: 1;
        min-width: 40;
    }

    #preview-column {
        width: 40;
        min-width: 32;
    }

    .panel-title, .form-title {
        height: auto;
        text-style: bold;
        margin-top: 1;
    }

    .form-description {
        color: $text-muted;
        margin-bottom: 1;
    }

    .section-form {
        display: none;
        padding-right: 1;
    }

    .field-wrap {
        width: 1fr;
        height: auto;
        margin-bottom: 1;
    }

    .field-wrap.invalid > .field-label {
        color: $error;
    }

    .field-wrap > Input,
    .field-wrap > Select,
    .field-wrap > SelectionList {
        width: 1fr;
    }

    .field-wrap.invalid > Input,
    .field-wrap.invalid > Select,
    .field-wrap.invalid > SelectionList,
    Input.invalid,
    Select.invalid,
    SelectionList.invalid {
        border: solid $error;
    }

    .field-label {
        text-style: bold;
        margin-bottom: 1;
    }

    .field-help {
        color: $text-muted;
    }

    #section-preview, #final-preview {
        height: 1fr;
    }

    #status {
        height: auto;
        min-height: 3;
        border-top: solid $panel;
        padding: 0 1;
    }
    """

    BINDINGS = [
        ("escape", "focus_section_list", "Sections"),
        ("ctrl+r", "reload_file", "Reload"),
        ("ctrl+w", "write_timestamped", "Save As"),
        ("ctrl+u", "update_loaded", "Update Loaded"),
        ("ctrl+v", "validate_config", "Validate"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, initial_path: Path | None = None) -> None:
        super().__init__()
        self.initial_path = initial_path
        self.loaded_path: Path | None = initial_path
        self.current_section = SECTION_ORDER[0]
        self.form_state = default_form_state()
        self.section_extras: dict[str, dict[str, Any]] = {}
        self._setting_fields = False
        self.status_text = ""
        self._status_source_widget_id: str | None = None
        self._last_focused_field_widget: Any = None
        self._field_validation_messages: dict[str, str] = {}
        self._loaded_roundtrip_yaml: Any | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="toolbar"):
            yield Label("Loaded config", classes="panel-title")
            with Horizontal(id="toolbar-row"):
                yield Static(
                    self._loaded_path_text(),
                    id="loaded-path",
                )
                yield Button("Reload", id="reload-button", variant="primary")
                yield Button("Validate", id="validate-button")
                yield Button("Save As...", id="write-button")
                yield Button(
                    "Update Loaded File", id="update-button", variant="warning"
                )
        with Horizontal(id="body"):
            yield OptionList(
                *[SECTION_SPECS[s].title for s in SECTION_ORDER], id="section-list"
            )
            with VerticalScroll(id="form-scroll", can_focus=False):
                for section in SECTION_ORDER:
                    section_spec = SECTION_SPECS[section]
                    with VerticalGroup(
                        id=f"section-form--{section}", classes="section-form"
                    ):
                        yield Label(section_spec.title, classes="form-title")
                        yield Static(
                            section_spec.description, classes="form-description"
                        )
                        for field in section_spec.fields:
                            with VerticalGroup(
                                id=_wrap_id(section, field.key),
                                classes="field-wrap",
                            ):
                                yield Label(field.label, classes="field-label")
                                yield self._build_field_widget(section, field)
                                if field.help_text:
                                    yield Static(field.help_text, classes="field-help")
            with Vertical(id="preview-column"):
                yield Label("Section preview", classes="panel-title")
                yield TextArea.code_editor(
                    language="yaml",
                    id="section-preview",
                    read_only=True,
                )
                yield Label("Final preview", classes="panel-title")
                yield TextArea.code_editor(
                    language="yaml",
                    id="final-preview",
                    read_only=True,
                )
        yield Static(id="status", markup=False)
        yield Footer()

    def _build_field_widget(self, section: str, field: FieldSpec):
        widget_id = _widget_id(section, field.key)
        value = deepcopy(self.form_state.get(section, {}).get(field.key, field.default))
        if field.kind in {"text", "int"}:
            return Input(
                value="" if value is None else str(value),
                placeholder=field.placeholder,
                id=widget_id,
            )
        if field.kind == "bool":
            return Switch(value=bool(value), id=widget_id)
        if field.kind == "select":
            return Select(
                [(option.label, option.value) for option in field.options],
                value=value,
                allow_blank=False,
                id=widget_id,
            )
        if field.kind == "multiselect":
            selected = set(value or [])
            return SelectionList(
                *[
                    (option.label, option.value, option.value in selected)
                    for option in field.options
                ],
                id=widget_id,
            )
        raise ValueError(f"Unsupported field kind: {field.kind}")

    def _loaded_path_text(self) -> str:
        if self.loaded_path is None:
            return "No config loaded. Pass a file path as a CLI argument to load one."
        return str(self.loaded_path)

    def on_mount(self) -> None:
        section_list = self.query_one("#section-list", OptionList)
        section_list.highlighted = 0
        section_list.focus()
        self._sync_widgets_from_state()
        if self.initial_path is not None:
            try:
                self._load_from_path(self.initial_path)
            except Exception as exc:  # noqa: BLE001
                self.loaded_path = None
                self.query_one("#loaded-path", Static).update(self._loaded_path_text())
                self._notify_plain(str(exc), severity="error")
                self._refresh_ui()
                section_list.focus()
                self._set_status(f"Load failed: {exc}")
        else:
            self._refresh_ui()

    def _set_status(self, text: str, *, source_widget_id: str | None = None) -> None:
        self.status_text = text
        self._status_source_widget_id = source_widget_id
        self.query_one("#status", Static).update(text)

    def _notify_plain(
        self,
        message: str,
        severity: NotificationSeverity = "information",
    ) -> None:
        self.notify(escape(message), severity=severity)

    def _default_save_path(self) -> str:
        return "gen-experiment-config.yaml"

    def _resolve_requested_output_path(self, raw_path: str) -> Path:
        return resolve_requested_output_path(raw_path)

    def _handle_save_path_selected(self, raw_path: str | None) -> None:
        if raw_path is None:
            return
        self._write_to_requested_path(raw_path)

    def _write_grouped_config_for_path(
        self,
        grouped: dict[str, Any],
        path: Path,
    ) -> Path:
        if self._loaded_roundtrip_yaml is None:
            return write_grouped_config(grouped, output_path=path)
        return write_grouped_config(
            grouped,
            output_path=path,
            source_roundtrip_document=self._loaded_roundtrip_yaml,
        )

    def _merged_grouped_config(self) -> dict[str, Any]:
        return build_grouped_config(self.form_state, section_extras=self.section_extras)

    def _task_default_pov_enabled(self, task: str | None) -> bool:
        return task == "bugfixing"

    def _sync_widgets_from_state(self) -> None:
        self._setting_fields = True
        try:
            for section in SECTION_ORDER:
                section_state = self.form_state.get(section, {})
                section_spec = SECTION_SPECS[section]
                for field in section_spec.fields:
                    widget_id = _widget_id(section, field.key)
                    value = deepcopy(section_state.get(field.key, field.default))
                    if field.kind in {"text", "int"}:
                        widget = self.query_one(f"#{widget_id}", Input)
                        widget.value = "" if value is None else str(value)
                    elif field.kind == "bool":
                        widget = self.query_one(f"#{widget_id}", Switch)
                        widget.value = bool(value)
                    elif field.kind == "select":
                        widget = self.query_one(f"#{widget_id}", Select)
                        widget.value = value
                    elif field.kind == "multiselect":
                        widget = self.query_one(f"#{widget_id}", SelectionList)
                        widget.deselect_all()
                        for selected_value in value or []:
                            widget.select(selected_value)
                    self._field_validation_messages.pop(widget.id, None)
                    self._set_widget_invalid_state(widget, invalid=False)
        finally:
            self._setting_fields = False

    def _refresh_field_visibility(self) -> None:
        for section in SECTION_ORDER:
            section_container = self.query_one(
                f"#section-form--{section}", VerticalGroup
            )
            section_container.display = section == self.current_section
            section_state = self.form_state.get(section, {})
            for field in SECTION_SPECS[section].fields:
                wrapper = self.query_one(
                    f"#{_wrap_id(section, field.key)}", VerticalGroup
                )
                wrapper.display = section == self.current_section and field.is_visible(
                    section_state
                )

    def _refresh_previews(self) -> None:
        grouped = self._merged_grouped_config()
        section_preview = self.query_one("#section-preview", TextArea)
        final_preview = self.query_one("#final-preview", TextArea)
        section_preview.text = dump_yaml(
            {self.current_section: grouped.get(self.current_section, {})}
        )
        final_preview.text = dump_yaml(grouped)

    def _refresh_ui(self) -> None:
        self._refresh_field_visibility()
        self._refresh_previews()

    def _apply_loaded_state(self, loaded_state: dict[str, dict[str, Any]]) -> None:
        merged_state = default_form_state()
        for section, values in loaded_state.items():
            merged_state.setdefault(section, {})
            merged_state[section].update(values)
        if "pov_enabled" not in loaded_state.get("runtime", {}):
            merged_state["runtime"]["pov_enabled"] = self._task_default_pov_enabled(
                merged_state["experiment"].get("task")
            )
        self.form_state = merged_state

    def _load_from_path(self, path: Path) -> None:
        self._loaded_roundtrip_yaml = None
        grouped = read_grouped_config(path)
        roundtrip_yaml = _load_roundtrip_document(path)
        loaded_state, extras = load_state_from_grouped_config(grouped)
        self._apply_loaded_state(loaded_state)
        self.section_extras = extras
        self.loaded_path = path
        self._loaded_roundtrip_yaml = roundtrip_yaml
        self.query_one("#loaded-path", Static).update(self._loaded_path_text())
        self._sync_widgets_from_state()
        self._refresh_ui()
        self._set_status(f"Loaded {path}")

    def _parse_input_value(self, field: FieldSpec, raw_value: str) -> Any:
        stripped = raw_value.strip()
        if field.kind == "int":
            if not stripped:
                return None
            return int(stripped)
        return stripped

    def _coerce_widget_value_for_validation(self, field: FieldSpec, widget: Any) -> Any:
        if field.kind in {"text", "int"}:
            return self._parse_input_value(field, widget.value)
        if field.kind == "bool":
            return bool(widget.value)
        if field.kind == "select":
            return widget.value
        if field.kind == "multiselect":
            return list(widget.selected)
        raise ValueError(f"Unsupported field kind: {field.kind}")

    def _set_widget_invalid_state(self, widget: Any, invalid: bool) -> None:
        widget.set_class(invalid, "invalid")
        field_info = self._field_from_widget_id(widget.id)
        if field_info is None:
            return
        section, field = field_info
        wrapper = self.query_one(f"#{_wrap_id(section, field.key)}", VerticalGroup)
        wrapper.set_class(invalid, "invalid")

    def _set_field_error_status(self, widget: Any, message: str) -> None:
        self._set_status(message, source_widget_id=widget.id)

    def _clear_status_if_owned_by(self, widget_id: str | None) -> None:
        if widget_id and self._status_source_widget_id == widget_id:
            self._set_status("")

    def _validate_widget(self, widget: Any) -> None:
        field_info = self._field_from_widget_id(widget.id)
        if field_info is None:
            return
        section, field = field_info
        previous_error = self._field_validation_messages.get(widget.id)
        try:
            value = self._coerce_widget_value_for_validation(field, widget)
            validate_field_value(section, field.key, value)
        except Exception as exc:
            self._field_validation_messages[widget.id] = str(exc)
            self._set_widget_invalid_state(widget, invalid=True)
            raise
        self._field_validation_messages.pop(widget.id, None)
        self._set_widget_invalid_state(widget, invalid=False)
        if previous_error is not None:
            self._clear_status_if_owned_by(widget.id)

    def _is_form_field_widget(self, widget: Any) -> bool:
        if not (widget.id and widget.id.startswith("field--")):
            return False
        form_scroll = self.query_one("#form-scroll", VerticalScroll)
        return widget in form_scroll.query("*")

    def _visible_field_widgets(self) -> list[Any]:
        section_state = self.form_state.get(self.current_section, {})
        widgets: list[Any] = []
        for field in SECTION_SPECS[self.current_section].fields:
            if not field.is_visible(section_state):
                continue
            widgets.append(
                self.query_one(f"#{_widget_id(self.current_section, field.key)}")
            )
        return widgets

    def _move_field_focus(self, step: int) -> bool:
        focused = self.focused
        if not self._is_form_field_widget(focused):
            return False

        if isinstance(focused, SelectionList):
            highlighted = focused.highlighted
            if highlighted is None:
                return False
            if step < 0 and highlighted != 0:
                return False
            if step > 0 and highlighted != focused.option_count - 1:
                return False

        widgets = self._visible_field_widgets()
        try:
            current_index = widgets.index(focused)
        except ValueError:
            return False

        next_index = current_index + step
        if next_index < 0 or next_index >= len(widgets):
            return False

        widgets[next_index].focus()
        return True

    def _field_from_widget_id(
        self, widget_id: str | None
    ) -> tuple[str, FieldSpec] | None:
        if not widget_id or not widget_id.startswith("field--"):
            return None
        _, section, key = widget_id.split("--", 2)
        section_spec = SECTION_SPECS.get(section)
        if section_spec is None:
            return None
        for field in section_spec.fields:
            if field.key == key:
                return section, field
        return None

    def _update_field_value(self, section: str, field: FieldSpec, value: Any) -> None:
        previous_task = self.form_state["experiment"].get("task")
        previous_pov = self.form_state["runtime"].get("pov_enabled")
        self.form_state.setdefault(section, {})[field.key] = value
        if section == "experiment" and field.key == "task":
            old_default = self._task_default_pov_enabled(previous_task)
            new_default = self._task_default_pov_enabled(value)
            if previous_pov == old_default:
                self.form_state["runtime"]["pov_enabled"] = new_default
                self._setting_fields = True
                try:
                    self.query_one(
                        "#field--runtime--pov_enabled", Switch
                    ).value = new_default
                finally:
                    self._setting_fields = False

    def _validated_config(self) -> dict[str, Any]:
        grouped = self._merged_grouped_config()
        validate_grouped_config(grouped)
        return grouped

    def _write_to_requested_path(self, raw_path: str) -> None:
        try:
            grouped = self._validated_config()
            path = self._resolve_requested_output_path(raw_path)
            written_path = self._write_grouped_config_for_path(grouped, path)
        except Exception as exc:  # noqa: BLE001
            self._notify_plain(str(exc), severity="error")
            self._set_status(f"Write failed: {exc}")
            return
        self._notify_plain(f"Wrote {written_path}", severity="information")
        self._set_status(f"Wrote config to {written_path}")

    def action_reload_file(self) -> None:
        if self.loaded_path is None:
            self._notify_plain(
                "No config loaded. Pass a file path as a CLI argument.",
                severity="warning",
            )
            return
        try:
            self._load_from_path(self.loaded_path)
        except Exception as exc:  # noqa: BLE001
            self._notify_plain(str(exc), severity="error")
            self._set_status(f"Load failed: {exc}")

    def action_validate_config(self) -> None:
        try:
            grouped = self._validated_config()
        except Exception as exc:  # noqa: BLE001
            self._notify_plain(str(exc), severity="error")
            self._set_status(f"Validation failed: {exc}")
            return
        self._refresh_previews()
        self._notify_plain("Config is valid", severity="information")
        self._set_status(f"Validation passed for {len(grouped)} populated sections")

    def action_write_timestamped(self) -> None:
        self.push_screen(
            SavePathScreen(default_path=self._default_save_path()),
            self._handle_save_path_selected,
        )

    def action_update_loaded(self) -> None:
        if self.loaded_path is None:
            self._notify_plain(
                "Load a config first to update it in place", severity="warning"
            )
            return
        try:
            grouped = self._validated_config()
            path = self._write_grouped_config_for_path(grouped, self.loaded_path)
        except Exception as exc:  # noqa: BLE001
            self._notify_plain(str(exc), severity="error")
            self._set_status(f"Update failed: {exc}")
            return
        self._notify_plain(f"Updated {path}", severity="information")
        self._set_status(f"Updated loaded config at {path}")

    def action_focus_section_list(self) -> None:
        self.query_one("#section-list", OptionList).focus()
        self._set_status("Focused section selector")

    @on(OptionList.OptionHighlighted, "#section-list")
    def handle_section_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_index is None:
            return
        self.current_section = SECTION_ORDER[event.option_index]
        self._refresh_ui()
        self._set_status(f"Editing {SECTION_SPECS[self.current_section].title}")

    @on(events.DescendantFocus)
    def handle_form_descendant_focus(self, event: events.DescendantFocus) -> None:
        widget = event.widget
        if not self._is_form_field_widget(widget):
            return
        previous = self._last_focused_field_widget
        if previous is not None and previous is not widget and not self._setting_fields:
            try:
                self._validate_widget(previous)
            except Exception as exc:  # noqa: BLE001
                self._set_field_error_status(previous, str(exc))
        self._last_focused_field_widget = widget
        widget.scroll_visible(animate=False, immediate=True)

    @on(events.DescendantBlur)
    def handle_form_descendant_blur(self, event: events.DescendantBlur) -> None:
        if self._setting_fields:
            return
        widget = event.widget
        if not self._is_form_field_widget(widget):
            return
        try:
            self._validate_widget(widget)
        except Exception as exc:  # noqa: BLE001
            self._set_field_error_status(widget, str(exc))

    @on(events.Key)
    def handle_field_arrow_navigation(self, event: events.Key) -> None:
        if event.key == "up" and self._move_field_focus(-1):
            event.stop()
            event.prevent_default()
            return
        if event.key == "down" and self._move_field_focus(1):
            event.stop()
            event.prevent_default()

    @on(Input.Changed)
    def handle_input_changed(self, event: Input.Changed) -> None:
        if self._setting_fields:
            return
        field_info = self._field_from_widget_id(event.input.id)
        if field_info is None:
            return
        section, field = field_info
        try:
            value = self._parse_input_value(field, event.value)
        except ValueError:
            self._set_status(f"{field.label} expects an integer")
            return
        self._update_field_value(section, field, value)
        self._refresh_ui()

    @on(Switch.Changed)
    def handle_switch_changed(self, event: Switch.Changed) -> None:
        if self._setting_fields:
            return
        field_info = self._field_from_widget_id(event.switch.id)
        if field_info is None:
            return
        section, field = field_info
        self._update_field_value(section, field, event.value)
        self._refresh_ui()

    @on(Select.Changed)
    def handle_select_changed(self, event: Select.Changed) -> None:
        if self._setting_fields:
            return
        field_info = self._field_from_widget_id(event.select.id)
        if field_info is None:
            return
        section, field = field_info
        self._update_field_value(section, field, event.value)
        self._refresh_ui()

    @on(SelectionList.SelectedChanged)
    def handle_selection_list_changed(
        self, event: SelectionList.SelectedChanged
    ) -> None:
        if self._setting_fields:
            return
        field_info = self._field_from_widget_id(event.selection_list.id)
        if field_info is None:
            return
        section, field = field_info
        self._update_field_value(section, field, list(event.selection_list.selected))
        self._refresh_ui()

    @on(Button.Pressed, "#reload-button")
    def handle_reload_pressed(self) -> None:
        self.action_reload_file()

    @on(Button.Pressed, "#validate-button")
    def handle_validate_pressed(self) -> None:
        self.action_validate_config()

    @on(Button.Pressed, "#write-button")
    def handle_write_pressed(self) -> None:
        self.action_write_timestamped()

    @on(Button.Pressed, "#update-button")
    def handle_update_pressed(self) -> None:
        self.action_update_loaded()


def build_argument_parser() -> ArgumentParser:
    parser = ArgumentParser(description="Edit grouped CRSBench configs with Textual.")
    parser.add_argument("config", nargs="?", type=Path, help="Optional config to load")
    return parser


def main(config_path: Path | None = None) -> int:
    app = ConfigBuilderApp(initial_path=config_path)
    app.run()
    return 0


def cli_main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()
    return main(config_path=args.config)


if __name__ == "__main__":
    raise SystemExit(cli_main())
