from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.error
import zipfile

from manager_core import (
    BackendUpdater,
    ClaudeCodeConnectionSettings,
    ClaudeCodeModelSettings,
    GitHubReleaseClient,
    ManagerConfig,
    ReleaseAsset,
    ReleaseInfo,
    ServerProcessManager,
    ServerStatus,
    claude_code_settings_backup_path,
    claude_code_settings_path,
    fetch_cliproxy_model_ids,
    is_newer_version,
    load_claude_code_model_settings,
    load_config,
    parse_datetime,
    parse_version_output,
    save_claude_code_connection_settings,
    save_claude_code_model_settings,
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
            self.assertEqual(config.base_url, "https://127.0.0.1:9123")
            self.assertEqual(config.health_url, "https://127.0.0.1:9123/v1/models")

    def test_uses_first_non_empty_trimmed_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            (root / "cli-proxy-api.exe").write_bytes(b"binary")
            (root / "config.yaml").write_text(
                'api-keys:\n  - "   "\n  - " current-key "\n',
                encoding="utf-8",
            )

            config = load_config(root)

            self.assertEqual(config.api_key, "current-key")
            self.assertNotIn("current-key", repr(config))

    def test_empty_api_keys_are_unset(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            (root / "cli-proxy-api.exe").write_bytes(b"binary")
            (root / "config.yaml").write_text(
                'api-keys:\n  - " "\n',
                encoding="utf-8",
            )

            self.assertIsNone(load_config(root).api_key)

    def test_base_url_formats_ipv6(self) -> None:
        root = Path("C:/cliproxy")
        config = ManagerConfig(
            work_dir=root,
            config_path=root / "config.yaml",
            executable_path=root / "cli-proxy-api.exe",
            auth_dir=root / "auth",
            host="::1",
            port=8317,
            api_key=None,
            tls_enabled=False,
        )

        self.assertEqual(config.base_url, "http://[::1]:8317")
        self.assertEqual(config.health_url, "http://[::1]:8317/v1/models")

    def test_rejects_invalid_port(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            (root / "cli-proxy-api.exe").write_bytes(b"binary")
            (root / "config.yaml").write_text("port: 70000\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "1~65535"):
                load_config(root)


class ClaudeCodeSettingsTests(unittest.TestCase):
    def test_settings_path_uses_supplied_home(self) -> None:
        home = Path("C:/Users/example")
        self.assertEqual(
            claude_code_settings_path(home),
            home / ".claude" / "settings.json",
        )

    def test_missing_file_loads_unset_models(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / ".claude" / "settings.json"
            self.assertEqual(
                load_claude_code_model_settings(path),
                ClaudeCodeModelSettings(None, None),
            )

    def test_loads_bom_and_trims_model_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "env": {
                            "CLAUDE_CODE_SUBAGENT_MODEL": " gpt-5.4 ",
                            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "gpt-5-mini",
                        }
                    }
                ),
                encoding="utf-8-sig",
            )
            self.assertEqual(
                load_claude_code_model_settings(path),
                ClaudeCodeModelSettings("gpt-5.4", "gpt-5-mini"),
            )

    def test_saves_models_without_replacing_unrelated_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / ".claude" / "settings.json"
            path.parent.mkdir()
            original = {
                "$schema": "https://json.schemastore.org/claude-code-settings.json",
                "permissions": {"allow": ["Read"]},
                "env": {
                    "EXISTING_VALUE": "keep-me",
                    "CLAUDE_CODE_SUBAGENT_MODEL": "old-model",
                },
            }
            path.write_text(json.dumps(original), encoding="utf-8")

            save_claude_code_model_settings(
                ClaudeCodeModelSettings("gpt-5.4", "gpt-5-mini"),
                path,
            )

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["$schema"], original["$schema"])
            self.assertEqual(saved["permissions"], original["permissions"])
            self.assertEqual(saved["env"]["EXISTING_VALUE"], "keep-me")
            self.assertEqual(
                saved["env"]["CLAUDE_CODE_SUBAGENT_MODEL"], "gpt-5.4"
            )
            self.assertEqual(
                saved["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "gpt-5-mini"
            )
            backup = claude_code_settings_backup_path(path)
            self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), original)

    def test_saves_connection_without_replacing_unrelated_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / ".claude" / "settings.json"
            path.parent.mkdir()
            original = {
                "$schema": "https://json.schemastore.org/claude-code-settings.json",
                "permissions": {"allow": ["Read"]},
                "hooks": {"Stop": []},
                "env": {
                    "EXISTING_VALUE": "keep-me",
                    "ANTHROPIC_API_KEY": "preserve-existing-key",
                    "CLAUDE_CODE_SUBAGENT_MODEL": "custom-model",
                },
            }
            path.write_text(json.dumps(original), encoding="utf-8")

            settings = ClaudeCodeConnectionSettings(
                " http://127.0.0.1:8317 ",
                " current-proxy-key ",
            )
            save_claude_code_connection_settings(settings, path)

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["$schema"], original["$schema"])
            self.assertEqual(saved["permissions"], original["permissions"])
            self.assertEqual(saved["hooks"], original["hooks"])
            self.assertEqual(saved["env"]["EXISTING_VALUE"], "keep-me")
            self.assertEqual(
                saved["env"]["CLAUDE_CODE_SUBAGENT_MODEL"], "custom-model"
            )
            self.assertEqual(
                saved["env"]["ANTHROPIC_API_KEY"], "preserve-existing-key"
            )
            self.assertEqual(
                saved["env"]["ANTHROPIC_BASE_URL"], "http://127.0.0.1:8317"
            )
            self.assertEqual(
                saved["env"]["ANTHROPIC_AUTH_TOKEN"], "current-proxy-key"
            )
            self.assertNotIn("current-proxy-key", repr(settings))
            backup = claude_code_settings_backup_path(path)
            self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), original)

    def test_connection_requires_url_and_api_key_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "settings.json"
            path.write_text('{"env": {"EXISTING_VALUE": "keep"}}', encoding="utf-8")
            before = path.read_bytes()

            cases = (
                (ClaudeCodeConnectionSettings(" ", "token"), "연결 주소"),
                (ClaudeCodeConnectionSettings("http://127.0.0.1:8317", " "), "API 키"),
            )
            for settings, message in cases:
                with self.subTest(message=message):
                    with self.assertRaisesRegex(ValueError, message):
                        save_claude_code_connection_settings(settings, path)
                    self.assertEqual(path.read_bytes(), before)
                    self.assertFalse(claude_code_settings_backup_path(path).exists())

    def test_unset_models_remove_only_managed_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "env": {
                            "EXISTING_VALUE": "keep-me",
                            "CLAUDE_CODE_SUBAGENT_MODEL": "gpt-5.4",
                            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "gpt-5-mini",
                        }
                    }
                ),
                encoding="utf-8",
            )

            save_claude_code_model_settings(
                ClaudeCodeModelSettings(None, None),
                path,
            )

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["env"], {"EXISTING_VALUE": "keep-me"})

    def test_malformed_json_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "settings.json"
            path.write_bytes(b"{broken")
            before = path.read_bytes()

            with self.assertRaisesRegex(ValueError, "설정 파일을 읽을 수 없습니다"):
                save_claude_code_model_settings(
                    ClaudeCodeModelSettings("gpt-5.4", None),
                    path,
                )

            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(claude_code_settings_backup_path(path).exists())

    def test_rejects_non_object_env(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "settings.json"
            path.write_text('{"env": []}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "env 값은 객체"):
                load_claude_code_model_settings(path)

    def test_retries_and_merges_external_change_during_save(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "settings.json"
            path.write_text(
                json.dumps({"env": {"EXISTING_VALUE": "old"}}),
                encoding="utf-8",
            )
            external = {
                "permissions": {"deny": ["Write"]},
                "env": {"EXISTING_VALUE": "new"},
            }
            checks = 0

            def current_bytes(current_path: Path) -> bytes | None:
                nonlocal checks
                checks += 1
                if checks == 1:
                    current_path.write_text(json.dumps(external), encoding="utf-8")
                return current_path.read_bytes()

            with mock.patch(
                "manager_core._current_claude_settings_bytes",
                side_effect=current_bytes,
            ):
                save_claude_code_model_settings(
                    ClaudeCodeModelSettings("gpt-5.4", None),
                    path,
                )

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["permissions"], external["permissions"])
            self.assertEqual(saved["env"]["EXISTING_VALUE"], "new")
            self.assertEqual(
                saved["env"]["CLAUDE_CODE_SUBAGENT_MODEL"], "gpt-5.4"
            )
            backup = json.loads(
                claude_code_settings_backup_path(path).read_text(encoding="utf-8")
            )
            self.assertEqual(backup, external)

    @mock.patch("manager_core.os.replace", side_effect=PermissionError("locked"))
    def test_replace_failure_keeps_original_and_cleans_temporary_files(
        self, _mock_replace: mock.Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            path = root / "settings.json"
            path.write_text('{"env": {"EXISTING_VALUE": "keep"}}', encoding="utf-8")
            before = path.read_bytes()

            with self.assertRaises(PermissionError):
                save_claude_code_model_settings(
                    ClaudeCodeModelSettings("gpt-5.4", None),
                    path,
                )

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(root.glob(".*.tmp")), [])


class FakeHttpResponse:
    def __init__(self, payload: bytes, status: int = 200):
        self.payload = payload
        self.status = status
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, size: int = -1) -> bytes:
        if self.offset >= len(self.payload):
            return b""
        end = len(self.payload) if size < 0 else self.offset + size
        chunk = self.payload[self.offset : end]
        self.offset += len(chunk)
        return chunk


class ModelDiscoveryTests(unittest.TestCase):
    @staticmethod
    def _config(*, api_key: str | None = "test-key", tls: bool = False):
        root = Path("C:/cliproxy")
        return ManagerConfig(
            work_dir=root,
            config_path=root / "config.yaml",
            executable_path=root / "cli-proxy-api.exe",
            auth_dir=root / "auth",
            host="127.0.0.1",
            port=8317,
            api_key=api_key,
            tls_enabled=tls,
        )

    @mock.patch("manager_core.urllib.request.urlopen")
    def test_fetches_unique_model_ids_with_authentication(
        self, mock_urlopen: mock.Mock
    ) -> None:
        mock_urlopen.return_value = FakeHttpResponse(
            json.dumps(
                {
                    "object": "list",
                    "data": [
                        {"id": " gpt-5.4 "},
                        {"id": "gpt-5-mini"},
                        {"id": "gpt-5.4"},
                        {"id": "  "},
                    ],
                }
            ).encode("utf-8")
        )

        models = fetch_cliproxy_model_ids(self._config())

        self.assertEqual(models, ("gpt-5.4", "gpt-5-mini"))
        request = mock_urlopen.call_args.args[0]
        headers = {key.casefold(): value for key, value in request.header_items()}
        self.assertEqual(headers["authorization"], "Bearer test-key")
        self.assertEqual(headers["user-agent"], "CLIProxyAPI-Manager/1.0")
        self.assertEqual(mock_urlopen.call_args.kwargs["timeout"], 5)

    @mock.patch("manager_core.ssl._create_unverified_context")
    @mock.patch("manager_core.urllib.request.urlopen")
    def test_uses_tls_context_without_adding_empty_auth_header(
        self, mock_urlopen: mock.Mock, mock_context: mock.Mock
    ) -> None:
        tls_context = object()
        mock_context.return_value = tls_context
        mock_urlopen.return_value = FakeHttpResponse(b'{"data": [{"id": "gpt"}]}')

        self.assertEqual(
            fetch_cliproxy_model_ids(self._config(api_key=None, tls=True)),
            ("gpt",),
        )

        request = mock_urlopen.call_args.args[0]
        headers = {key.casefold(): value for key, value in request.header_items()}
        self.assertNotIn("authorization", headers)
        self.assertIs(mock_urlopen.call_args.kwargs["context"], tls_context)

    @mock.patch("manager_core.urllib.request.urlopen")
    def test_http_error_is_reported(self, mock_urlopen: mock.Mock) -> None:
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "http://127.0.0.1:8317/v1/models",
            401,
            "Unauthorized",
            None,
            None,
        )
        with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
            fetch_cliproxy_model_ids(self._config())

    def test_model_response_has_an_overall_deadline(self) -> None:
        response = FakeHttpResponse(b'{"data": [{"id": "gpt"}]}')
        with mock.patch(
            "manager_core.urllib.request.urlopen",
            return_value=response,
        ), mock.patch(
            "manager_core.time.monotonic",
            side_effect=(0.0, 0.0, 6.0),
        ):
            with self.assertRaisesRegex(RuntimeError, "응답 시간이 초과"):
                fetch_cliproxy_model_ids(self._config(), timeout=5)

    @mock.patch("manager_core.urllib.request.urlopen")
    def test_rejects_malformed_or_empty_model_responses(
        self, mock_urlopen: mock.Mock
    ) -> None:
        cases = (
            (b"{", "올바른 JSON"),
            (b"[]", "최상위 값은 객체"),
            (b'{"data": {}}', "data 값은 배열"),
            (b'{"data": [{"id": 123}]}', "id 값은 문자열"),
            (b'{"data": [{"id": " "}]}', "사용 가능한 모델"),
        )
        for payload, message in cases:
            with self.subTest(message=message):
                mock_urlopen.return_value = FakeHttpResponse(payload)
                with self.assertRaisesRegex(ValueError, message):
                    fetch_cliproxy_model_ids(self._config())


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
    @staticmethod
    def _config(*, port: int, api_key: str) -> ManagerConfig:
        root = Path("C:/cliproxy")
        return ManagerConfig(
            work_dir=root,
            config_path=root / "config.yaml",
            executable_path=root / "cli-proxy-api.exe",
            auth_dir=root / "auth",
            host="127.0.0.1",
            port=port,
            api_key=api_key,
            tls_enabled=False,
        )

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

    @mock.patch("manager_core.urllib.request.urlopen")
    def test_probe_uses_updated_url_and_api_key(
        self, mock_urlopen: mock.Mock
    ) -> None:
        mock_urlopen.return_value = FakeHttpResponse(b"{}")
        processes = ServerProcessManager(self._config(port=8317, api_key="old-key"))
        processes.update_config(self._config(port=9123, api_key="new-key"))

        healthy, status, error = processes._probe_http()

        self.assertTrue(healthy)
        self.assertEqual(status, 200)
        self.assertIsNone(error)
        request = mock_urlopen.call_args.args[0]
        headers = {key.casefold(): value for key, value in request.header_items()}
        self.assertEqual(request.full_url, "http://127.0.0.1:9123/v1/models")
        self.assertEqual(headers["authorization"], "Bearer new-key")

    def test_rejects_config_for_different_executable(self) -> None:
        processes = ServerProcessManager(self._config(port=8317, api_key="key"))
        other = self._config(port=9123, api_key="new-key")
        other = ManagerConfig(
            work_dir=Path("D:/other"),
            config_path=Path("D:/other/config.yaml"),
            executable_path=Path("D:/other/cli-proxy-api.exe"),
            auth_dir=other.auth_dir,
            host=other.host,
            port=other.port,
            api_key=other.api_key,
            tls_enabled=other.tls_enabled,
        )

        with self.assertRaisesRegex(ValueError, "다른 CLIProxyAPI"):
            processes.update_config(other)


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
