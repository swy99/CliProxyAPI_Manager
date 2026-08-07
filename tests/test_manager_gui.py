from __future__ import annotations

import queue
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import pystray

from cliproxy_manager import (
    MODEL_DEFAULT_VALUE,
    SUBAGENT_INHERIT_VALUE,
    ManagerApp,
    find_backend_dir,
    model_choice_values,
    model_override_from_display,
)
from manager_core import ClaudeCodeModelSettings


class FakeVariable:
    def __init__(self, value: str = ""):
        self.value = value

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


class FakeWidget:
    def __init__(self):
        self.state = None

    def configure(self, **kwargs) -> None:
        if "state" in kwargs:
            self.state = kwargs["state"]


class TrayMenuTests(unittest.TestCase):
    def test_login_submenu_callbacks_have_supported_signature(self) -> None:
        app = ManagerApp.__new__(ManagerApp)
        app.events = queue.Queue()
        app.login_by_label = {
            "Claude": "claude-login",
            "Codex": "codex-login",
        }

        menu = app._tray_menu()

        self.assertIsInstance(menu, pystray.Menu)
        self.assertEqual(len(menu.items), 7)


class ModelSelectionTests(unittest.TestCase):
    def test_unset_display_values_remove_model_overrides(self) -> None:
        self.assertIsNone(
            model_override_from_display(" inherit ", SUBAGENT_INHERIT_VALUE)
        )
        self.assertIsNone(
            model_override_from_display(" DEFAULT ", MODEL_DEFAULT_VALUE)
        )
        self.assertIsNone(model_override_from_display("  ", MODEL_DEFAULT_VALUE))
        self.assertEqual(
            model_override_from_display(" gpt-5.4 ", SUBAGENT_INHERIT_VALUE),
            "gpt-5.4",
        )

    def test_model_choices_preserve_custom_value_and_remove_duplicates(self) -> None:
        self.assertEqual(
            model_choice_values(
                SUBAGENT_INHERIT_VALUE,
                "custom-gpt",
                ("gpt-5.4", "custom-gpt", "gpt-5-mini"),
            ),
            ("inherit", "custom-gpt", "gpt-5.4", "gpt-5-mini"),
        )

    @mock.patch("cliproxy_manager.threading.Thread")
    def test_save_locks_model_inputs_until_completion(
        self, mock_thread: mock.Mock
    ) -> None:
        app = ManagerApp.__new__(ManagerApp)
        app.subagent_model_var = FakeVariable("gpt-5.4")
        app.fable_model_var = FakeVariable("gpt-5.4")
        app.opus_model_var = FakeVariable("gpt-5.3-codex")
        app.sonnet_model_var = FakeVariable("gpt-5.2-codex")
        app.haiku_model_var = FakeVariable("gpt-5-mini")
        app.claude_settings_status = FakeVariable()
        app.claude_settings_save_event = threading.Event()
        app.subagent_model_combo = FakeWidget()
        app.fable_model_combo = FakeWidget()
        app.opus_model_combo = FakeWidget()
        app.sonnet_model_combo = FakeWidget()
        app.haiku_model_combo = FakeWidget()
        app.save_claude_settings_button = FakeWidget()
        app.claude_settings_path = Path("C:/Users/example/.claude/settings.json")

        app.request_claude_settings_save()

        expected_settings = ClaudeCodeModelSettings(
            subagent_model="gpt-5.4",
            fable_model="gpt-5.4",
            opus_model="gpt-5.3-codex",
            sonnet_model="gpt-5.2-codex",
            haiku_model="gpt-5-mini",
        )
        self.assertEqual(mock_thread.call_args.kwargs["args"], (expected_settings,))
        self.assertEqual(app.subagent_model_combo.state, "disabled")
        self.assertEqual(app.fable_model_combo.state, "disabled")
        self.assertEqual(app.opus_model_combo.state, "disabled")
        self.assertEqual(app.sonnet_model_combo.state, "disabled")
        self.assertEqual(app.haiku_model_combo.state, "disabled")
        self.assertEqual(app.save_claude_settings_button.state, "disabled")
        mock_thread.return_value.start.assert_called_once_with()

        with mock.patch("cliproxy_manager.messagebox.showinfo"):
            app._finish_claude_settings_save(None)
        self.assertEqual(app.subagent_model_combo.state, "normal")
        self.assertEqual(app.fable_model_combo.state, "normal")
        self.assertEqual(app.opus_model_combo.state, "normal")
        self.assertEqual(app.sonnet_model_combo.state, "normal")
        self.assertEqual(app.haiku_model_combo.state, "normal")
        self.assertEqual(app.save_claude_settings_button.state, "normal")
        self.assertFalse(app.claude_settings_save_event.is_set())

    @mock.patch(
        "cliproxy_manager.fetch_cliproxy_model_ids",
        return_value=("gpt-5.4", "gpt-5-mini"),
    )
    def test_model_refresh_worker_posts_success_event(
        self, _mock_fetch: mock.Mock
    ) -> None:
        app = ManagerApp.__new__(ManagerApp)
        app.config = object()
        app.events = queue.Queue()
        app.logger = mock.Mock()

        app._model_refresh_worker(True)

        self.assertEqual(
            app.events.get_nowait(),
            ("models-loaded", ("gpt-5.4", "gpt-5-mini"), True),
        )

    @mock.patch(
        "cliproxy_manager.fetch_cliproxy_model_ids",
        side_effect=RuntimeError("offline"),
    )
    def test_model_refresh_worker_posts_failure_event(
        self, _mock_fetch: mock.Mock
    ) -> None:
        app = ManagerApp.__new__(ManagerApp)
        app.config = object()
        app.events = queue.Queue()
        app.logger = mock.Mock()

        app._model_refresh_worker(False)

        self.assertEqual(
            app.events.get_nowait(),
            ("models-load-failed", "offline", False),
        )

    @mock.patch("cliproxy_manager.save_claude_code_model_settings")
    def test_settings_save_worker_posts_success_event(
        self, mock_save: mock.Mock
    ) -> None:
        app = ManagerApp.__new__(ManagerApp)
        app.claude_settings_path = Path("C:/Users/example/.claude/settings.json")
        app.events = queue.Queue()
        app.logger = mock.Mock()
        settings = ClaudeCodeModelSettings(
            subagent_model="gpt-5.4",
            fable_model="gpt-5.4",
            opus_model="gpt-5.3-codex",
            sonnet_model="gpt-5.2-codex",
            haiku_model="gpt-5-mini",
        )

        app._claude_settings_save_worker(settings)

        mock_save.assert_called_once_with(settings, app.claude_settings_path)
        self.assertEqual(app.events.get_nowait(), ("claude-settings-saved",))


class BackendDirectoryTests(unittest.TestCase):
    def test_finds_backend_in_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            app_dir = root / "manager"
            app_dir.mkdir()
            (root / "config.yaml").write_text("port: 8317\n", encoding="utf-8")
            (root / "cli-proxy-api.exe").write_bytes(b"test")

            self.assertEqual(find_backend_dir(app_dir), root.resolve())

    def test_prefers_backend_next_to_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            app_dir = Path(temp_name)
            (app_dir / "config.yaml").write_text("port: 8317\n", encoding="utf-8")
            (app_dir / "cli-proxy-api.exe").write_bytes(b"test")

            self.assertEqual(find_backend_dir(app_dir), app_dir.resolve())


if __name__ == "__main__":
    unittest.main()
