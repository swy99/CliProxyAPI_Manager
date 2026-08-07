"""Process, authentication, update, and startup helpers."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import psutil
import yaml

LOGGER = logging.getLogger("cliproxy-manager")

GITHUB_LATEST_RELEASE_URL = (
    "https://api.github.com/repos/router-for-me/CLIProxyAPI/releases/latest"
)
STARTUP_REGISTRY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_VALUE_NAME = "CLIProxyAPI Manager"
VERSION_PATTERN = re.compile(r"CLIProxyAPI Version:\s*v?([^,\s]+)", re.IGNORECASE)
LOGIN_FLAG_PATTERN = re.compile(r"^\s+-(?P<flag>[\w-]+-login)\s*$", re.MULTILINE)
CLAUDE_CODE_SUBAGENT_MODEL_KEY = "CLAUDE_CODE_SUBAGENT_MODEL"
ANTHROPIC_DEFAULT_FABLE_MODEL_KEY = "ANTHROPIC_DEFAULT_FABLE_MODEL"
ANTHROPIC_DEFAULT_OPUS_MODEL_KEY = "ANTHROPIC_DEFAULT_OPUS_MODEL"
ANTHROPIC_DEFAULT_SONNET_MODEL_KEY = "ANTHROPIC_DEFAULT_SONNET_MODEL"
ANTHROPIC_DEFAULT_HAIKU_MODEL_KEY = "ANTHROPIC_DEFAULT_HAIKU_MODEL"
CLAUDE_SETTINGS_BACKUP_SUFFIX = ".cliproxy-manager.bak"
MAX_CLAUDE_SETTINGS_WRITE_ATTEMPTS = 3
MAX_MODELS_RESPONSE_BYTES = 2_000_000
MODELS_RESPONSE_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True)
class ManagerConfig:
    work_dir: Path
    config_path: Path
    executable_path: Path
    auth_dir: Path
    host: str
    port: int
    api_key: str | None
    tls_enabled: bool

    @property
    def health_url(self) -> str:
        host = self.host.strip()
        if host in {"", "0.0.0.0", "::", "[::]"}:
            host = "127.0.0.1"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        scheme = "https" if self.tls_enabled else "http"
        return f"{scheme}://{host}:{self.port}/v1/models"


@dataclass(frozen=True)
class ClaudeCodeModelSettings:
    subagent_model: str | None
    haiku_model: str | None
    fable_model: str | None = None
    opus_model: str | None = None
    sonnet_model: str | None = None


@dataclass(frozen=True)
class AuthRecord:
    path: Path
    provider: str
    account: str
    expires_at: dt.datetime | None
    disabled: bool
    status: str
    error: str | None = None

    @property
    def notification_key(self) -> str:
        marker = self.expires_at.isoformat() if self.expires_at else self.status
        return f"{self.path.resolve()}|{marker}"


@dataclass(frozen=True)
class ServerStatus:
    running: bool
    healthy: bool
    pids: tuple[int, ...]
    http_status: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    url: str
    size: int
    digest: str | None


@dataclass(frozen=True)
class ReleaseInfo:
    tag: str
    assets: tuple[ReleaseAsset, ...]


@dataclass(frozen=True)
class UpdateResult:
    status: str
    current_version: str | None
    latest_version: str | None
    message: str
    backup_path: Path | None = None


def load_config(work_dir: Path) -> ManagerConfig:
    """Load the small subset of config.yaml needed by the manager."""

    work_dir = work_dir.resolve()
    config_path = work_dir / "config.yaml"
    executable_path = work_dir / "cli-proxy-api.exe"

    if not config_path.is_file():
        raise FileNotFoundError(f"설정 파일을 찾을 수 없습니다: {config_path}")
    if not executable_path.is_file():
        raise FileNotFoundError(f"실행 파일을 찾을 수 없습니다: {executable_path}")

    with config_path.open("r", encoding="utf-8-sig") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError("config.yaml의 최상위 값은 객체여야 합니다.")

    host = str(loaded.get("host") or "127.0.0.1")
    try:
        port = int(loaded.get("port", 8317))
    except (TypeError, ValueError) as exc:
        raise ValueError("config.yaml의 port가 올바른 숫자가 아닙니다.") from exc
    if not 1 <= port <= 65535:
        raise ValueError("config.yaml의 port는 1~65535 범위여야 합니다.")

    raw_auth_dir = str(loaded.get("auth-dir") or "~/.cli-proxy-api")
    expanded_auth_dir = Path(os.path.expandvars(raw_auth_dir)).expanduser()
    if not expanded_auth_dir.is_absolute():
        expanded_auth_dir = config_path.parent / expanded_auth_dir

    raw_keys = loaded.get("api-keys") or []
    api_key = None
    if isinstance(raw_keys, list):
        api_key = next(
            (str(item) for item in raw_keys if isinstance(item, (str, int, float))),
            None,
        )

    tls = loaded.get("tls") or {}
    tls_enabled = bool(tls.get("enable", False)) if isinstance(tls, dict) else False

    return ManagerConfig(
        work_dir=work_dir,
        config_path=config_path,
        executable_path=executable_path,
        auth_dir=expanded_auth_dir.resolve(),
        host=host,
        port=port,
        api_key=api_key,
        tls_enabled=tls_enabled,
    )


def claude_code_settings_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".claude" / "settings.json"


def claude_code_settings_backup_path(path: Path) -> Path:
    return path.with_name(f"{path.name}{CLAUDE_SETTINGS_BACKUP_SUFFIX}")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"지원하지 않는 JSON 상수입니다: {value}")


def _parse_claude_settings_document(raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8-sig")
        if not text.strip():
            raise ValueError("설정 파일이 비어 있습니다.")
        loaded = json.loads(text, parse_constant=_reject_json_constant)
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Claude Code 설정 파일을 읽을 수 없습니다: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError("Claude Code settings.json의 최상위 값은 객체여야 합니다.")
    env = loaded.get("env")
    if env is not None and not isinstance(env, dict):
        raise ValueError("Claude Code settings.json의 env 값은 객체여야 합니다.")
    return loaded


def _read_claude_settings_state(
    path: Path,
) -> tuple[dict[str, Any], bytes | None]:
    if not path.exists():
        return {}, None
    if not path.is_file():
        raise ValueError(f"Claude Code 설정 경로가 파일이 아닙니다: {path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"Claude Code 설정 파일을 읽을 수 없습니다: {exc}") from exc
    return _parse_claude_settings_document(raw), raw


def _load_claude_settings_document(path: Path) -> dict[str, Any]:
    return _read_claude_settings_state(path)[0]


def _model_setting_value(env: dict[str, Any], key: str) -> str | None:
    if key not in env:
        return None
    value = env[key]
    if not isinstance(value, str):
        raise ValueError(f"Claude Code settings.json의 env.{key} 값은 문자열이어야 합니다.")
    normalized = value.strip()
    return normalized or None


def load_claude_code_model_settings(
    path: Path | None = None,
) -> ClaudeCodeModelSettings:
    settings_path = path or claude_code_settings_path()
    document = _load_claude_settings_document(settings_path)
    env = document.get("env") or {}
    return ClaudeCodeModelSettings(
        subagent_model=_model_setting_value(env, CLAUDE_CODE_SUBAGENT_MODEL_KEY),
        fable_model=_model_setting_value(env, ANTHROPIC_DEFAULT_FABLE_MODEL_KEY),
        opus_model=_model_setting_value(env, ANTHROPIC_DEFAULT_OPUS_MODEL_KEY),
        sonnet_model=_model_setting_value(env, ANTHROPIC_DEFAULT_SONNET_MODEL_KEY),
        haiku_model=_model_setting_value(env, ANTHROPIC_DEFAULT_HAIKU_MODEL_KEY),
    )


def _normalized_model_override(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _write_claude_settings_temp(
    path: Path,
    document: dict[str, Any],
) -> Path:
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                document,
                handle,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return temp_path


def _write_claude_settings_backup_temp(path: Path, raw: bytes) -> Path:
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}{CLAUDE_SETTINGS_BACKUP_SUFFIX}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return temp_path


def _current_claude_settings_bytes(path: Path) -> bytes | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise RuntimeError(f"Claude Code 설정 경로가 파일이 아닙니다: {path}")
    return path.read_bytes()


def save_claude_code_model_settings(
    settings: ClaudeCodeModelSettings,
    path: Path | None = None,
) -> None:
    settings_path = path or claude_code_settings_path()
    subagent_model = _normalized_model_override(settings.subagent_model)
    fable_model = _normalized_model_override(settings.fable_model)
    opus_model = _normalized_model_override(settings.opus_model)
    sonnet_model = _normalized_model_override(settings.sonnet_model)
    haiku_model = _normalized_model_override(settings.haiku_model)
    model_overrides = (
        subagent_model,
        fable_model,
        opus_model,
        sonnet_model,
        haiku_model,
    )

    for _attempt in range(MAX_CLAUDE_SETTINGS_WRITE_ATTEMPTS):
        document, original_raw = _read_claude_settings_state(settings_path)
        if original_raw is None and not any(model_overrides):
            return
        settings_path.parent.mkdir(parents=True, exist_ok=True)

        env_was_present = "env" in document
        env = dict(document.get("env") or {})
        updates = {
            CLAUDE_CODE_SUBAGENT_MODEL_KEY: subagent_model,
            ANTHROPIC_DEFAULT_FABLE_MODEL_KEY: fable_model,
            ANTHROPIC_DEFAULT_OPUS_MODEL_KEY: opus_model,
            ANTHROPIC_DEFAULT_SONNET_MODEL_KEY: sonnet_model,
            ANTHROPIC_DEFAULT_HAIKU_MODEL_KEY: haiku_model,
        }
        for key, value in updates.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        if env or env_was_present:
            document["env"] = env

        temp_path = _write_claude_settings_temp(settings_path, document)
        backup_temp_path: Path | None = None
        try:
            if _current_claude_settings_bytes(settings_path) != original_raw:
                continue
            if original_raw is not None:
                backup_path = claude_code_settings_backup_path(settings_path)
                backup_temp_path = _write_claude_settings_backup_temp(
                    settings_path,
                    original_raw,
                )
                os.replace(backup_temp_path, backup_path)
                backup_temp_path = None
                if _current_claude_settings_bytes(settings_path) != original_raw:
                    continue
            os.replace(temp_path, settings_path)
            temp_path = None
            return
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            if backup_temp_path is not None:
                backup_temp_path.unlink(missing_ok=True)

    raise RuntimeError(
        "Claude Code settings.json이 다른 프로그램에서 계속 변경되어 저장하지 못했습니다."
    )


def _read_models_response(response: Any, deadline: float) -> bytes:
    chunks: list[bytes] = []
    total = 0
    read_chunk = getattr(response, "read1", response.read)
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError("모델 목록 응답 시간이 초과되었습니다.")
        chunk = read_chunk(MODELS_RESPONSE_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_MODELS_RESPONSE_BYTES:
            raise ValueError("모델 목록 응답이 허용 크기를 초과했습니다.")
        chunks.append(chunk)
        if time.monotonic() >= deadline:
            raise TimeoutError("모델 목록 응답 시간이 초과되었습니다.")
    return b"".join(chunks)


def fetch_cliproxy_model_ids(
    config: ManagerConfig,
    timeout: float = 5,
) -> tuple[str, ...]:
    request = urllib.request.Request(
        config.health_url,
        headers={"User-Agent": "CLIProxyAPI-Manager/1.0"},
    )
    if config.api_key:
        request.add_header("Authorization", f"Bearer {config.api_key}")
    context = ssl._create_unverified_context() if config.tls_enabled else None
    deadline = time.monotonic() + timeout
    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout,
            context=context,
        ) as response:
            status = int(response.status)
            if not 200 <= status < 300:
                raise RuntimeError(f"모델 목록 조회 실패: HTTP {status}")
            body = _read_models_response(response, deadline)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"모델 목록 조회 실패: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"모델 목록 조회 실패: {exc}") from exc

    try:
        loaded = json.loads(
            body.decode("utf-8-sig"),
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"모델 목록 응답이 올바른 JSON이 아닙니다: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError("모델 목록 응답의 최상위 값은 객체여야 합니다.")
    entries = loaded.get("data")
    if not isinstance(entries, list):
        raise ValueError("모델 목록 응답의 data 값은 배열이어야 합니다.")

    model_ids: list[str] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"모델 목록의 {index + 1}번째 항목은 객체여야 합니다.")
        model_id = entry.get("id")
        if not isinstance(model_id, str):
            raise ValueError(
                f"모델 목록의 {index + 1}번째 id 값은 문자열이어야 합니다."
            )
        normalized = model_id.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        model_ids.append(normalized)
    if not model_ids:
        raise ValueError("CLIProxyAPI가 사용 가능한 모델을 반환하지 않았습니다.")
    return tuple(model_ids)


def parse_datetime(value: Any) -> dt.datetime | None:
    """Parse the expiry formats used by CLIProxyAPI auth files."""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None

    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
    return parsed


def _expiry_value(data: dict[str, Any]) -> Any:
    for key in ("expired", "expires_at", "expires", "expiry", "expiration"):
        if key in data:
            return data[key]
    token = data.get("token")
    if isinstance(token, dict):
        for key in ("expired", "expires_at", "expires", "expiry", "expiration"):
            if key in token:
                return token[key]
    return None


def scan_auth_records(
    auth_dir: Path,
    *,
    now: dt.datetime | None = None,
    warning_window: dt.timedelta = dt.timedelta(hours=1),
    expiry_grace: dt.timedelta = dt.timedelta(minutes=5),
) -> list[AuthRecord]:
    """Read auth metadata without returning access or refresh token values."""

    if now is None:
        now = dt.datetime.now().astimezone()
    elif now.tzinfo is None:
        now = now.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)

    records: list[AuthRecord] = []
    if not auth_dir.is_dir():
        return records

    for path in sorted(auth_dir.glob("*.json"), key=lambda item: item.name.casefold()):
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError("JSON 최상위 값이 객체가 아닙니다.")

            provider = str(data.get("type") or data.get("provider") or "unknown")
            account = str(
                data.get("email")
                or data.get("account")
                or data.get("name")
                or path.stem
            )
            disabled = bool(data.get("disabled", False))
            raw_expiry = _expiry_value(data)
            expires_at = parse_datetime(raw_expiry)

            if disabled:
                status = "disabled"
            elif raw_expiry is True:
                status = "expired"
            elif expires_at is None:
                status = "unknown"
            else:
                comparable_now = now.astimezone(expires_at.tzinfo)
                if comparable_now >= expires_at + expiry_grace:
                    status = "expired"
                elif comparable_now >= expires_at:
                    status = "refreshing"
                elif expires_at - comparable_now <= warning_window:
                    status = "expiring"
                else:
                    status = "valid"

            records.append(
                AuthRecord(
                    path=path,
                    provider=provider,
                    account=account,
                    expires_at=expires_at,
                    disabled=disabled,
                    status=status,
                )
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            records.append(
                AuthRecord(
                    path=path,
                    provider="unknown",
                    account=path.stem,
                    expires_at=None,
                    disabled=False,
                    status="invalid",
                    error=str(exc),
                )
            )
    return records


def parse_version_output(output: str) -> str | None:
    match = VERSION_PATTERN.search(output)
    return match.group(1) if match else None


def version_key(version: str | None) -> tuple[int, ...]:
    if not version:
        return ()
    core = version.strip().lstrip("vV").split("-", 1)[0]
    parts: list[int] = []
    for item in core.split("."):
        match = re.match(r"\d+", item)
        if not match:
            break
        parts.append(int(match.group(0)))
    return tuple(parts)


def is_newer_version(candidate: str, current: str | None) -> bool:
    candidate_key = version_key(candidate)
    current_key = version_key(current)
    width = max(len(candidate_key), len(current_key))
    return candidate_key + (0,) * (width - len(candidate_key)) > current_key + (
        0,
    ) * (width - len(current_key))


def read_binary_help(executable_path: Path, timeout: float = 10) -> str:
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    completed = subprocess.run(
        [str(executable_path), "-help"],
        cwd=str(executable_path.parent),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        creationflags=creation_flags,
        check=False,
    )
    return f"{completed.stdout}\n{completed.stderr}"


def read_binary_version(executable_path: Path) -> str | None:
    try:
        return parse_version_output(read_binary_help(executable_path))
    except (OSError, subprocess.SubprocessError):
        return None


def supported_login_flags(executable_path: Path) -> tuple[str, ...]:
    try:
        output = read_binary_help(executable_path)
    except (OSError, subprocess.SubprocessError):
        return ()
    return tuple(dict.fromkeys(LOGIN_FLAG_PATTERN.findall(output)))


def _normalized_path(path: str | Path) -> str:
    try:
        return os.path.normcase(str(Path(path).resolve()))
    except (OSError, RuntimeError):
        return os.path.normcase(os.path.abspath(str(path)))


class ServerProcessManager:
    """Manage only server processes launched from the configured executable."""

    def __init__(self, config: ManagerConfig):
        self.config = config
        self._target_path = _normalized_path(config.executable_path)

    @staticmethod
    def _is_server_command(command_line: Iterable[str]) -> bool:
        flags = {str(arg).casefold() for arg in command_line}
        excluded = {
            "-help",
            "--help",
            "-tui",
            "-vertex-import",
        }
        if flags & excluded:
            return False
        return not any(flag.endswith("-login") for flag in flags)

    def related_processes(self) -> list[psutil.Process]:
        # Match the resolved executable path, not only the process name. A user
        # may run other CLIProxyAPI installations at the same time.
        matches: list[psutil.Process] = []
        for process in psutil.process_iter(["exe", "cmdline"]):
            try:
                executable = process.info.get("exe")
                if executable and _normalized_path(executable) == self._target_path:
                    matches.append(process)
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                continue
        return matches

    def server_processes(self) -> list[psutil.Process]:
        servers: list[psutil.Process] = []
        for process in self.related_processes():
            try:
                if self._is_server_command(process.cmdline()):
                    servers.append(process)
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
        return servers

    def interactive_processes(self) -> list[psutil.Process]:
        servers = {process.pid for process in self.server_processes()}
        return [
            process
            for process in self.related_processes()
            if process.pid not in servers
        ]

    def _probe_http(self, timeout: float = 3) -> tuple[bool, int | None, str | None]:
        request = urllib.request.Request(
            self.config.health_url,
            headers={"User-Agent": "CLIProxyAPI-Manager/1.0"},
        )
        if self.config.api_key:
            request.add_header("Authorization", f"Bearer {self.config.api_key}")
        context = ssl._create_unverified_context() if self.config.tls_enabled else None
        try:
            with urllib.request.urlopen(
                request, timeout=timeout, context=context
            ) as response:
                status = int(response.status)
                return status < 500, status, None
        except urllib.error.HTTPError as exc:
            return exc.code < 500, exc.code, str(exc)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return False, None, str(exc)

    def inspect(self, timeout: float = 3) -> ServerStatus:
        processes = self.server_processes()
        if not processes:
            return ServerStatus(False, False, (), error="프로세스가 없습니다.")
        healthy, http_status, error = self._probe_http(timeout)
        return ServerStatus(
            True,
            healthy,
            tuple(sorted(process.pid for process in processes)),
            http_status,
            error,
        )

    def start(self) -> int:
        running = self.server_processes()
        if running:
            return running[0].pid

        stdout_path = self.config.work_dir / "cli-proxy-api.log"
        stderr_path = self.config.work_dir / "cli-proxy-api.error.log"
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
        with stdout_path.open("ab") as stdout_handle, stderr_path.open(
            "ab"
        ) as stderr_handle:
            process = subprocess.Popen(
                [
                    str(self.config.executable_path),
                    "-config",
                    str(self.config.config_path),
                ],
                cwd=str(self.config.work_dir),
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                creationflags=creation_flags,
                close_fds=True,
            )
        return process.pid

    def stop(self, timeout: float = 10) -> None:
        processes = self.server_processes()
        for process in processes:
            try:
                process.terminate()
            except psutil.NoSuchProcess:
                continue
        _, alive = psutil.wait_procs(processes, timeout=timeout)
        for process in alive:
            try:
                process.kill()
            except psutil.NoSuchProcess:
                continue
        if alive:
            psutil.wait_procs(alive, timeout=3)

    def wait_healthy(self, timeout: float = 15) -> ServerStatus:
        deadline = time.monotonic() + timeout
        last_status = self.inspect(timeout=1)
        while time.monotonic() < deadline:
            if last_status.running and last_status.healthy:
                return last_status
            time.sleep(0.4)
            last_status = self.inspect(timeout=1)
        return last_status

    def restart(self) -> ServerStatus:
        self.stop()
        self.start()
        return self.wait_healthy()

    def launch_login(self, flag: str) -> subprocess.Popen[bytes]:
        if flag not in supported_login_flags(self.config.executable_path):
            raise ValueError(f"현재 바이너리가 지원하지 않는 로그인 옵션입니다: {flag}")
        creation_flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        return subprocess.Popen(
            [
                str(self.config.executable_path),
                f"-{flag}",
                "-config",
                str(self.config.config_path),
            ],
            cwd=str(self.config.work_dir),
            creationflags=creation_flags,
        )


class GitHubReleaseClient:
    def __init__(self, api_url: str = GITHUB_LATEST_RELEASE_URL):
        self.api_url = api_url
        self.headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "CLIProxyAPI-Manager/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def latest(self, timeout: float = 20) -> ReleaseInfo:
        request = urllib.request.Request(self.api_url, headers=self.headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        tag = str(payload.get("tag_name") or "")
        if not tag:
            raise ValueError("최신 릴리스 응답에 tag_name이 없습니다.")

        assets: list[ReleaseAsset] = []
        for raw_asset in payload.get("assets") or []:
            if not isinstance(raw_asset, dict):
                continue
            name = str(raw_asset.get("name") or "")
            url = str(raw_asset.get("browser_download_url") or "")
            if not name or not url:
                continue
            assets.append(
                ReleaseAsset(
                    name=name,
                    url=url,
                    size=int(raw_asset.get("size") or 0),
                    digest=(
                        str(raw_asset["digest"]) if raw_asset.get("digest") else None
                    ),
                )
            )
        return ReleaseInfo(tag=tag, assets=tuple(assets))

    def checksum_from_release(
        self, release: ReleaseInfo, target_name: str, timeout: float = 20
    ) -> str | None:
        checksum_asset = next(
            (
                asset
                for asset in release.assets
                if asset.name.casefold() in {"checksums.txt", "sha256sums.txt"}
            ),
            None,
        )
        if checksum_asset is None:
            return None
        request = urllib.request.Request(checksum_asset.url, headers=self.headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content = response.read(1_000_000).decode("utf-8", errors="replace")
        for line in content.splitlines():
            if target_name.casefold() not in line.casefold():
                continue
            match = re.search(r"\b([a-fA-F0-9]{64})\b", line)
            if match:
                return match.group(1).lower()
        return None

    def download(
        self,
        asset: ReleaseAsset,
        destination: Path,
        expected_sha256: str,
        timeout: float = 120,
    ) -> str:
        request = urllib.request.Request(asset.url, headers=self.headers)
        digest = hashlib.sha256()
        total = 0
        maximum_size = 256 * 1024 * 1024
        with urllib.request.urlopen(request, timeout=timeout) as response, destination.open(
            "wb"
        ) as output:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > maximum_size:
                    raise ValueError("업데이트 파일이 허용 크기(256MB)를 초과했습니다.")
                digest.update(chunk)
                output.write(chunk)
        actual = digest.hexdigest()
        if actual.casefold() != expected_sha256.casefold():
            destination.unlink(missing_ok=True)
            raise ValueError(
                f"SHA-256 검증 실패: 예상 {expected_sha256}, 실제 {actual}"
            )
        return actual


def windows_release_architecture() -> str:
    machine = platform.machine().casefold()
    if machine in {"amd64", "x86_64", "x64"}:
        return "amd64"
    if machine in {"arm64", "aarch64"}:
        return "aarch64"
    raise RuntimeError(f"지원하지 않는 Windows 아키텍처입니다: {platform.machine()}")


def select_windows_asset(
    release: ReleaseInfo, architecture: str | None = None
) -> ReleaseAsset:
    architecture = architecture or windows_release_architecture()
    suffix = f"_windows_{architecture}.zip".casefold()
    matches = [
        asset for asset in release.assets if asset.name.casefold().endswith(suffix)
    ]
    if len(matches) != 1:
        raise ValueError(
            f"릴리스 {release.tag}에서 Windows {architecture} ZIP을 하나만 찾을 수 없습니다."
        )
    return matches[0]


def _extract_backend_binary(archive_path: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        matches = [
            info
            for info in archive.infolist()
            if not info.is_dir()
            and Path(info.filename.replace("\\", "/")).name.casefold()
            == "cli-proxy-api.exe"
        ]
        if len(matches) != 1:
            raise ValueError(
                "업데이트 ZIP에서 cli-proxy-api.exe를 하나만 찾을 수 없습니다."
            )
        if matches[0].file_size > 256 * 1024 * 1024:
            raise ValueError("압축된 실행 파일이 허용 크기(256MB)를 초과했습니다.")
        with archive.open(matches[0]) as source, destination.open("wb") as output:
            shutil.copyfileobj(source, output)


class BackendUpdater:
    """Transactional backend updater with checksum validation and rollback."""

    def __init__(
        self,
        config: ManagerConfig,
        processes: ServerProcessManager,
        client: GitHubReleaseClient | None = None,
        progress: Callable[[str], None] | None = None,
    ):
        self.config = config
        self.processes = processes
        self.client = client or GitHubReleaseClient()
        self.progress = progress or (lambda _message: None)

    def _report(self, message: str) -> None:
        LOGGER.info(message)
        self.progress(message)

    def run(self) -> UpdateResult:
        current = read_binary_version(self.config.executable_path)
        latest_version: str | None = None
        try:
            self._report("공식 릴리스 확인 중")
            release = self.client.latest()
            latest_version = release.tag.lstrip("vV")
            if current and not is_newer_version(latest_version, current):
                return UpdateResult(
                    "up_to_date",
                    current,
                    latest_version,
                    f"최신 버전입니다 (v{current}).",
                )

            asset = select_windows_asset(release)
            digest = None
            # GitHub exposes a digest for current release assets. Older
            # releases use checksums.txt, which remains supported as fallback.
            if asset.digest and asset.digest.casefold().startswith("sha256:"):
                digest = asset.digest.split(":", 1)[1]
            if not digest or not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
                digest = self.client.checksum_from_release(release, asset.name)
            if not digest:
                raise ValueError("공식 SHA-256 체크섬을 찾을 수 없어 업데이트를 중단했습니다.")

            with tempfile.TemporaryDirectory(prefix="cliproxyapi-update-") as temp_name:
                temp_dir = Path(temp_name)
                archive_path = temp_dir / asset.name
                candidate_path = temp_dir / "cli-proxy-api.exe"

                self._report(f"v{latest_version} 다운로드 및 SHA-256 검증 중")
                self.client.download(asset, archive_path, digest)
                _extract_backend_binary(archive_path, candidate_path)
                candidate_version = read_binary_version(candidate_path)
                if version_key(candidate_version) != version_key(latest_version):
                    raise ValueError(
                        "다운로드한 실행 파일 버전이 릴리스 태그와 일치하지 않습니다 "
                        f"({candidate_version!r} != {latest_version!r})."
                    )

                if self.processes.interactive_processes():
                    raise RuntimeError(
                        "로그인 창이 실행 중이어서 업데이트를 다음 주기로 미뤘습니다."
                    )

                staged_path = (
                    self.config.work_dir
                    / f".cli-proxy-api.update-{uuid.uuid4().hex}.exe"
                )
                shutil.copy2(candidate_path, staged_path)

                backup_path = self.config.work_dir / (
                    f"cli-proxy-api.exe.backup-{current or 'unknown'}"
                )
                if backup_path.exists():
                    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
                    backup_path = backup_path.with_name(f"{backup_path.name}-{stamp}")

                backup_moved = False
                server_was_running = bool(self.processes.server_processes())
                self._report("서버를 안전하게 중지하고 업데이트 적용 중")
                try:
                    # Keep the previous binary on the same volume so both the
                    # replacement and a possible rollback stay atomic.
                    self.processes.stop()
                    os.replace(self.config.executable_path, backup_path)
                    backup_moved = True
                    os.replace(staged_path, self.config.executable_path)
                    self.processes.start()
                    status = self.processes.wait_healthy(timeout=20)
                    if not status.healthy:
                        raise RuntimeError(
                            f"업데이트 후 서버 상태 확인 실패: {status.error or '응답 없음'}"
                        )
                except Exception:
                    if backup_moved:
                        self._report("업데이트 실패로 이전 버전 복구 중")
                        self.processes.stop()
                        failed_path = self.config.work_dir / (
                            f"cli-proxy-api.exe.failed-{latest_version}-"
                            f"{dt.datetime.now():%Y%m%d-%H%M%S}"
                        )
                        if self.config.executable_path.exists():
                            os.replace(self.config.executable_path, failed_path)
                        os.replace(backup_path, self.config.executable_path)
                        self.processes.start()
                        self.processes.wait_healthy(timeout=15)
                    elif server_was_running and not self.processes.server_processes():
                        self.processes.start()
                    staged_path.unlink(missing_ok=True)
                    raise

            return UpdateResult(
                "updated",
                current,
                latest_version,
                f"CLIProxyAPI를 v{latest_version}(으)로 업데이트했습니다.",
                backup_path,
            )
        except Exception as exc:
            LOGGER.exception("Backend update failed")
            return UpdateResult(
                "failed",
                current,
                latest_version,
                f"업데이트 실패: {exc}",
            )


def startup_command(entry_script: Path, *, frozen: bool, executable: Path) -> str:
    if frozen:
        arguments = [str(executable), "--minimized"]
    else:
        pythonw = executable.with_name("pythonw.exe")
        interpreter = pythonw if pythonw.exists() else executable
        arguments = [str(interpreter), str(entry_script), "--minimized"]
    return subprocess.list2cmdline(arguments)


def register_startup(command: str) -> None:
    if os.name != "nt":
        raise RuntimeError("시작프로그램 등록은 Windows에서만 지원합니다.")
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, STARTUP_REGISTRY_KEY) as key:
        winreg.SetValueEx(key, STARTUP_VALUE_NAME, 0, winreg.REG_SZ, command)


def unregister_startup() -> None:
    if os.name != "nt":
        return
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, STARTUP_REGISTRY_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, STARTUP_VALUE_NAME)
    except FileNotFoundError:
        return


def read_startup_command() -> str | None:
    if os.name != "nt":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REGISTRY_KEY) as key:
            value, _ = winreg.QueryValueEx(key, STARTUP_VALUE_NAME)
            return str(value)
    except FileNotFoundError:
        return None
