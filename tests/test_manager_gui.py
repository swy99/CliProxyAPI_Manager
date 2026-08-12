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
from manager_core import (
    AuthRecord,
    ClaudeCodeConnectionSettings,
    ClaudeCodeModelSettings,
)


def _record(name: str = "claude") -> AuthRecord:
    return AuthRecord(
        path=Path(f"C:/cliproxy/auth/{name}.json"),
        provider=name,
        account=f"{name}@example.test",
        expires_at=None,
        disabled=False,
        status="unknown",
    )


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
        app.apply_claude_connection_button = FakeWidget()
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
        self.assertEqual(app.apply_claude_connection_button.state, "disabled")
        self.assertEqual(app.save_claude_settings_button.state, "disabled")
        mock_thread.return_value.start.assert_called_once_with()

        with mock.patch("cliproxy_manager.messagebox.showinfo"):
            app._finish_claude_settings_save(None)
        self.assertEqual(app.subagent_model_combo.state, "normal")
        self.assertEqual(app.fable_model_combo.state, "normal")
        self.assertEqual(app.opus_model_combo.state, "normal")
        self.assertEqual(app.sonnet_model_combo.state, "normal")
        self.assertEqual(app.haiku_model_combo.state, "normal")
        self.assertEqual(app.apply_claude_connection_button.state, "normal")
        self.assertEqual(app.save_claude_settings_button.state, "normal")
        self.assertFalse(app.claude_settings_save_event.is_set())

    @mock.patch(
        "cliproxy_manager.fetch_cliproxy_model_ids",
        return_value=("gpt-5.4", "gpt-5-mini"),
    )
    def test_model_refresh_worker_posts_success_event(
        self, mock_fetch: mock.Mock
    ) -> None:
        app = ManagerApp.__new__(ManagerApp)
        current_config = object()
        app._reload_runtime_config = mock.Mock(return_value=current_config)
        app.events = queue.Queue()
        app.logger = mock.Mock()

        app._model_refresh_worker(True)

        app._reload_runtime_config.assert_called_once_with()
        mock_fetch.assert_called_once_with(current_config)
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
        app._reload_runtime_config = mock.Mock(return_value=object())
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

    @mock.patch("cliproxy_manager.threading.Thread")
    def test_connection_apply_uses_shared_settings_write_guard(
        self, mock_thread: mock.Mock
    ) -> None:
        app = ManagerApp.__new__(ManagerApp)
        app.claude_settings_status = FakeVariable()
        app.claude_settings_save_event = threading.Event()
        app.subagent_model_combo = FakeWidget()
        app.fable_model_combo = FakeWidget()
        app.opus_model_combo = FakeWidget()
        app.sonnet_model_combo = FakeWidget()
        app.haiku_model_combo = FakeWidget()
        app.apply_claude_connection_button = FakeWidget()
        app.save_claude_settings_button = FakeWidget()

        app.request_claude_connection_apply()

        self.assertTrue(app.claude_settings_save_event.is_set())
        self.assertEqual(app.apply_claude_connection_button.state, "disabled")
        self.assertEqual(app.save_claude_settings_button.state, "disabled")
        mock_thread.return_value.start.assert_called_once_with()

        with mock.patch("cliproxy_manager.messagebox.showinfo"):
            app._finish_claude_connection_apply(None, "http://127.0.0.1:8317")
        self.assertFalse(app.claude_settings_save_event.is_set())
        self.assertEqual(app.apply_claude_connection_button.state, "normal")
        self.assertEqual(app.save_claude_settings_button.state, "normal")

    @mock.patch("cliproxy_manager.save_claude_code_connection_settings")
    def test_connection_apply_worker_uses_latest_config_without_exposing_key(
        self, mock_save: mock.Mock
    ) -> None:
        app = ManagerApp.__new__(ManagerApp)
        config = mock.Mock()
        config.base_url = "http://127.0.0.1:8317"
        config.api_key = "current-proxy-key"
        app._reload_runtime_config = mock.Mock(return_value=config)
        app.claude_settings_path = Path("C:/Users/example/.claude/settings.json")
        app.events = queue.Queue()
        app.logger = mock.Mock()

        app._claude_connection_apply_worker()

        settings = mock_save.call_args.args[0]
        self.assertEqual(
            settings,
            ClaudeCodeConnectionSettings(
                "http://127.0.0.1:8317", "current-proxy-key"
            ),
        )
        self.assertNotIn("current-proxy-key", repr(settings))
        mock_save.assert_called_once_with(settings, app.claude_settings_path)
        event = app.events.get_nowait()
        self.assertEqual(
            event,
            ("claude-connection-applied", "http://127.0.0.1:8317"),
        )
        self.assertNotIn("current-proxy-key", repr(event))

    @mock.patch("cliproxy_manager.load_config")
    def test_reload_runtime_config_updates_both_consumers(
        self, mock_load: mock.Mock
    ) -> None:
        app = ManagerApp.__new__(ManagerApp)
        app.work_dir = Path("C:/cliproxy")
        app.config_lock = threading.Lock()
        app.config = object()
        app.processes = mock.Mock()
        current_config = object()
        mock_load.return_value = current_config

        self.assertIs(app._reload_runtime_config(), current_config)

        mock_load.assert_called_once_with(app.work_dir)
        app.processes.update_config.assert_called_once_with(current_config)
        self.assertIs(app.config, current_config)


class AuthDisableTests(unittest.TestCase):
    @mock.patch("cliproxy_manager.messagebox.askyesno", return_value=True)
    @mock.patch("cliproxy_manager.threading.Thread")
    def test_request_starts_worker_after_confirmation(
        self, mock_thread: mock.Mock, mock_ask: mock.Mock
    ) -> None:
        app = ManagerApp.__new__(ManagerApp)
        record = _record()
        app.auth_tree = mock.Mock()
        app.auth_tree.selection.return_value = ("item-1",)
        app.auth_items = {"item-1": record}
        app.auth_disable_button = FakeWidget()

        app.request_auth_disable()

        self.assertIn(record.account, mock_ask.call_args.args[1])
        self.assertEqual(mock_thread.call_args.kwargs["args"], (record,))
        self.assertEqual(app.auth_disable_button.state, "disabled")
        mock_thread.return_value.start.assert_called_once_with()

    @mock.patch("cliproxy_manager.messagebox.askyesno", return_value=False)
    @mock.patch("cliproxy_manager.threading.Thread")
    def test_request_cancel_keeps_token(
        self, mock_thread: mock.Mock, _mock_ask: mock.Mock
    ) -> None:
        app = ManagerApp.__new__(ManagerApp)
        app.auth_tree = mock.Mock()
        app.auth_tree.selection.return_value = ("item-1",)
        app.auth_items = {"item-1": _record()}
        app.auth_disable_button = FakeWidget()

        app.request_auth_disable()

        mock_thread.assert_not_called()

    @mock.patch("cliproxy_manager.threading.Thread")
    def test_request_without_selection_does_nothing(
        self, mock_thread: mock.Mock
    ) -> None:
        app = ManagerApp.__new__(ManagerApp)
        app.auth_tree = mock.Mock()
        app.auth_tree.selection.return_value = ()
        app.auth_items = {}

        app.request_auth_disable()

        mock_thread.assert_not_called()

    @mock.patch("cliproxy_manager.scan_auth_records")
    @mock.patch("cliproxy_manager.disable_auth_file")
    def test_worker_posts_success_event_with_rescan(
        self, mock_disable: mock.Mock, mock_scan: mock.Mock
    ) -> None:
        app = ManagerApp.__new__(ManagerApp)
        config = mock.Mock()
        config.auth_dir = Path("C:/cliproxy/auth")
        app._reload_runtime_config = mock.Mock(return_value=config)
        app.events = queue.Queue()
        app.logger = mock.Mock()
        record = _record()
        remaining = [_record("codex")]
        mock_scan.return_value = remaining

        app._auth_disable_worker(record)

        mock_disable.assert_called_once_with(config.auth_dir, record.path)
        mock_scan.assert_called_once_with(config.auth_dir)
        self.assertEqual(
            app.events.get_nowait(),
            ("auth-disable-done", record, remaining),
        )

    @mock.patch(
        "cliproxy_manager.disable_auth_file",
        side_effect=ValueError("이동 실패"),
    )
    def test_worker_posts_failure_notification(
        self, _mock_disable: mock.Mock
    ) -> None:
        app = ManagerApp.__new__(ManagerApp)
        config = mock.Mock()
        config.auth_dir = Path("C:/cliproxy/auth")
        app._reload_runtime_config = mock.Mock(return_value=config)
        app.events = queue.Queue()
        app.logger = mock.Mock()
        record = _record()

        app._auth_disable_worker(record)

        event = app.events.get_nowait()
        self.assertEqual(event[0], "notify")
        self.assertEqual(event[1], "토큰 사용 중지 실패")
        self.assertIn("이동 실패", event[2])


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
