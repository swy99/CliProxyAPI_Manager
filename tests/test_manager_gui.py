from __future__ import annotations

import queue
from pathlib import Path
import tempfile
import unittest

import pystray

from cliproxy_manager import ManagerApp, find_backend_dir


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
