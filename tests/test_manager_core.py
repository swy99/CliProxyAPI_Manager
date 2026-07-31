from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

from manager_core import (
    BackendUpdater,
    GitHubReleaseClient,
    ReleaseAsset,
    ReleaseInfo,
    ServerProcessManager,
    ServerStatus,
    is_newer_version,
    load_config,
    parse_datetime,
    parse_version_output,
    scan_auth_records,
    select_windows_asset,
    startup_command,
    supported_login_flags,
    version_key,
)


class ConfigTests(unittest.TestCase):
    def test_loads_runtime_paths_and_health_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            (root / "cli-proxy-api.exe").write_bytes(b"binary")
            (root / "config.yaml").write_text(
                "\n".join(
                    [
                        'host: "0.0.0.0"',
                        "port: 9123",
                        'auth-dir: "credentials"',
                        "api-keys:",
                        '  - "test-key"',
                        "tls:",
                        "  enable: true",
                    ]
                ),
                encoding="utf-8",
            )

            config = load_config(root)

            self.assertEqual(config.auth_dir, (root / "credentials").resolve())
            self.assertEqual(config.api_key, "test-key")
            self.assertEqual(config.health_url, "https://127.0.0.1:9123/v1/models")

    def test_rejects_invalid_port(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            (root / "cli-proxy-api.exe").write_bytes(b"binary")
            (root / "config.yaml").write_text("port: 70000\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "1~65535"):
                load_config(root)


class AuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = dt.datetime(2026, 7, 31, 12, 0, tzinfo=dt.timezone.utc)

    def _write_auth(
        self, root: Path, name: str, *, expiry: object, disabled: bool = False
    ) -> None:
        payload = {
            "type": name,
            "email": f"{name}@example.test",
            "expired": expiry,
            "disabled": disabled,
            "access_token": "sensitive-test-value",
            "refresh_token": "sensitive-refresh-value",
        }
        (root / f"{name}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def test_classifies_expiry_with_refresh_grace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            self._write_auth(root, "expired", expiry="2026-07-31T11:50:00Z")
            self._write_auth(root, "refreshing", expiry="2026-07-31T11:58:00Z")
            self._write_auth(root, "expiring", expiry="2026-07-31T12:30:00Z")
            self._write_auth(root, "valid", expiry="2026-07-31T14:00:00Z")
            self._write_auth(
                root,
                "disabled",
                expiry="2026-07-31T11:00:00Z",
                disabled=True,
            )

            records = scan_auth_records(root, now=self.now)
            statuses = {record.provider: record.status for record in records}

            self.assertEqual(statuses["expired"], "expired")
            self.assertEqual(statuses["refreshing"], "refreshing")
            self.assertEqual(statuses["expiring"], "expiring")
            self.assertEqual(statuses["valid"], "valid")
            self.assertEqual(statuses["disabled"], "disabled")
            self.assertFalse(
                any(
                    "sensitive-test-value" in repr(record)
                    for record in records
                )
            )

    def test_invalid_json_is_reported_without_stopping_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            (root / "broken.json").write_text("{", encoding="utf-8")
            self._write_auth(root, "valid", expiry="2026-07-31T14:00:00Z")

            records = scan_auth_records(root, now=self.now)

            self.assertEqual(len(records), 2)
            self.assertIn("invalid", {record.status for record in records})
            self.assertIn("valid", {record.status for record in records})

    def test_parses_epoch_milliseconds_and_iso_z(self) -> None:
        milliseconds = 1_775_000_000_000
        self.assertEqual(
            parse_datetime(milliseconds),
            dt.datetime.fromtimestamp(
                milliseconds / 1000, tz=dt.timezone.utc
            ),
        )
        self.assertEqual(
            parse_datetime("2026-07-31T12:00:00Z"),
            dt.datetime(2026, 7, 31, 12, 0, tzinfo=dt.timezone.utc),
        )


class VersionAndReleaseTests(unittest.TestCase):
    def test_parses_and_compares_versions(self) -> None:
        output = (
            "CLIProxyAPI Version: 7.2.104, Commit: abc, "
            "BuiltAt: 2026-07-28T08:36:42Z"
        )
        self.assertEqual(parse_version_output(output), "7.2.104")
        self.assertEqual(version_key("v7.2.104"), (7, 2, 104))
        self.assertTrue(is_newer_version("7.2.111", "7.2.104"))
        self.assertFalse(is_newer_version("7.2.104", "7.2.104"))
        self.assertFalse(is_newer_version("7.1.99", "7.2.0"))

    def test_selects_exact_windows_architecture_asset(self) -> None:
        release = ReleaseInfo(
            "v7.2.111",
            (
                ReleaseAsset(
                    "CLIProxyAPI_7.2.111_windows_aarch64.zip",
                    "https://example.test/arm.zip",
                    1,
                    None,
                ),
                ReleaseAsset(
                    "CLIProxyAPI_7.2.111_windows_amd64.zip",
                    "https://example.test/amd.zip",
                    1,
                    "sha256:" + "a" * 64,
                ),
            ),
        )
        selected = select_windows_asset(release, "amd64")
        self.assertEqual(selected.url, "https://example.test/amd.zip")

    @mock.patch(
        "manager_core.read_binary_help",
        return_value="\n  -claude-login\n    Login\n  -codex-login\n    Login\n",
    )
    def test_discovers_supported_login_flags(self, _mock_help: mock.Mock) -> None:
        self.assertEqual(
            supported_login_flags(Path("cli-proxy-api.exe")),
            ("claude-login", "codex-login"),
        )


class ProcessClassificationTests(unittest.TestCase):
    def test_login_commands_are_not_server_commands(self) -> None:
        self.assertTrue(
            ServerProcessManager._is_server_command(
                ["cli-proxy-api.exe", "-config", "config.yaml"]
            )
        )
        self.assertFalse(
            ServerProcessManager._is_server_command(
                ["cli-proxy-api.exe", "-codex-login", "-config", "config.yaml"]
            )
        )


class FakeProcesses:
    def __init__(self, healthy_after_update: bool = True):
        self.healthy_after_update = healthy_after_update
        self.running = True
        self.start_calls = 0

    def interactive_processes(self) -> list[object]:
        return []

    def server_processes(self) -> list[object]:
        return [object()] if self.running else []

    def stop(self, timeout: float = 10) -> None:
        self.running = False

    def start(self) -> int:
        self.running = True
        self.start_calls += 1
        return 100

    def wait_healthy(self, timeout: float = 15) -> ServerStatus:
        healthy = self.healthy_after_update
        if self.start_calls > 1:
            healthy = True
        return ServerStatus(
            running=self.running,
            healthy=healthy,
            pids=(100,) if self.running else (),
            http_status=200 if healthy else None,
            error=None if healthy else "test failure",
        )


class FakeReleaseClient(GitHubReleaseClient):
    def __init__(self):
        super().__init__("https://example.test/latest")
        self.asset = ReleaseAsset(
            "CLIProxyAPI_1.1.0_windows_amd64.zip",
            "https://example.test/backend.zip",
            100,
            "sha256:" + "a" * 64,
        )

    def latest(self, timeout: float = 20) -> ReleaseInfo:
        return ReleaseInfo("v1.1.0", (self.asset,))

    def download(
        self,
        asset: ReleaseAsset,
        destination: Path,
        expected_sha256: str,
        timeout: float = 120,
    ) -> str:
        with zipfile.ZipFile(destination, "w") as archive:
            archive.writestr("CLIProxyAPI_1.1.0/cli-proxy-api.exe", b"new")
        return expected_sha256


class UpdaterTests(unittest.TestCase):
    def _config(self, root: Path):
        (root / "cli-proxy-api.exe").write_bytes(b"old")
        (root / "config.yaml").write_text(
            'host: "127.0.0.1"\nport: 8317\nauth-dir: "auth"\n',
            encoding="utf-8",
        )
        return load_config(root)

    @mock.patch(
        "manager_core.read_binary_version",
        side_effect=lambda path: (
            "1.0.0"
            if "cliproxyapi-update-" not in str(path.parent)
            else "1.1.0"
        ),
    )
    @mock.patch("manager_core.windows_release_architecture", return_value="amd64")
    def test_transactionally_replaces_backend(
        self, _mock_arch: mock.Mock, _mock_version: mock.Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config = self._config(root)
            processes = FakeProcesses()
            updater = BackendUpdater(config, processes, FakeReleaseClient())

            result = updater.run()

            self.assertEqual(result.status, "updated")
            self.assertEqual((root / "cli-proxy-api.exe").read_bytes(), b"new")
            self.assertEqual(
                (root / "cli-proxy-api.exe.backup-1.0.0").read_bytes(), b"old"
            )

    @mock.patch(
        "manager_core.read_binary_version",
        side_effect=lambda path: (
            "1.0.0"
            if "cliproxyapi-update-" not in str(path.parent)
            else "1.1.0"
        ),
    )
    @mock.patch("manager_core.windows_release_architecture", return_value="amd64")
    def test_rolls_back_when_new_backend_is_unhealthy(
        self, _mock_arch: mock.Mock, _mock_version: mock.Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config = self._config(root)
            processes = FakeProcesses(healthy_after_update=False)
            updater = BackendUpdater(config, processes, FakeReleaseClient())

            with self.assertLogs("cliproxy-manager", level="ERROR"):
                result = updater.run()

            self.assertEqual(result.status, "failed")
            self.assertEqual((root / "cli-proxy-api.exe").read_bytes(), b"old")
            self.assertTrue(processes.running)


class StartupTests(unittest.TestCase):
    def test_source_startup_uses_pythonw_and_minimized(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            python = root / "python.exe"
            (root / "pythonw.exe").write_bytes(b"")
            script = root / "CLIProxy Manager" / "cliproxy_manager.py"
            command = startup_command(
                script,
                frozen=False,
                executable=python,
            )
            self.assertIn("pythonw.exe", command)
            self.assertIn("cliproxy_manager.py", command)
            self.assertIn("--minimized", command)


if __name__ == "__main__":
    unittest.main()
