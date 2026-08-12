"""Windows tray application for a local CLIProxyAPI installation."""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable

import pystray
from PIL import Image, ImageDraw

from manager_core import (
    AuthRecord,
    BackendUpdater,
    ClaudeCodeConnectionSettings,
    ClaudeCodeModelSettings,
    ManagerConfig,
    ServerProcessManager,
    ServerStatus,
    UpdateResult,
    claude_code_settings_path,
    fetch_cliproxy_model_ids,
    load_claude_code_model_settings,
    load_config,
    read_binary_version,
    read_startup_command,
    register_startup,
    save_claude_code_connection_settings,
    save_claude_code_model_settings,
    scan_auth_records,
    startup_command,
    supported_login_flags,
    unregister_startup,
)

APP_NAME = "CLIProxyAPI 관리자"
APP_VERSION = "1.3.1"
STATUS_INTERVAL_SECONDS = 15
UPDATE_INTERVAL_SECONDS = 6 * 60 * 60
MAX_RESTART_BACKOFF_SECONDS = 5 * 60

LOGIN_LABELS = {
    "antigravity-login": "Antigravity",
    "claude-login": "Claude",
    "codex-device-login": "Codex (기기 코드)",
    "codex-login": "Codex",
    "kimi-login": "Kimi",
    "xai-login": "xAI",
}
STATUS_LABELS = {
    "valid": "정상",
    "expiring": "곧 만료",
    "refreshing": "갱신 대기",
    "expired": "만료됨",
    "disabled": "사용 안 함",
    "unknown": "만료 정보 없음",
    "invalid": "파일 오류",
}
SUBAGENT_INHERIT_VALUE = "inherit"
MODEL_DEFAULT_VALUE = "default"
HAIKU_DEFAULT_VALUE = MODEL_DEFAULT_VALUE


def model_override_from_display(value: str, unset_value: str) -> str | None:
    normalized = value.strip()
    if not normalized or normalized.casefold() == unset_value.casefold():
        return None
    return normalized


def model_choice_values(
    unset_value: str,
    current_value: str,
    model_ids: tuple[str, ...],
) -> tuple[str, ...]:
    choices: list[str] = [unset_value]
    current = current_value.strip()
    if current and current.casefold() != unset_value.casefold():
        choices.append(current)
    choices.extend(model_ids)
    return tuple(dict.fromkeys(choices))


def configure_logging(work_dir: Path) -> None:
    logger = logging.getLogger("cliproxy-manager")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return
    handler = RotatingFileHandler(
        work_dir / "cliproxy-manager.log",
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s")
    )
    logger.addHandler(handler)


def manager_startup_command() -> str:
    return startup_command(
        Path(__file__).resolve(),
        frozen=bool(getattr(sys, "frozen", False)),
        executable=Path(sys.executable).resolve(),
    )


class WindowsSingleInstance:
    """A per-user mutex that prevents duplicate tray managers."""

    ERROR_ALREADY_EXISTS = 183

    def __init__(self, name: str):
        self.handle: int | None = None
        self.already_running = False
        if os.name != "nt":
            return
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        self.handle = kernel32.CreateMutexW(None, False, name)
        self.already_running = kernel32.GetLastError() == self.ERROR_ALREADY_EXISTS

    def close(self) -> None:
        if self.handle and os.name == "nt":
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None


def _tray_image(color: str) -> Image.Image:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((5, 5, 59, 59), radius=14, fill="#172033")
    draw.ellipse((17, 17, 47, 47), fill=color)
    draw.ellipse((25, 25, 39, 39), fill="#172033")
    return image


class ManagerApp:
    def __init__(
        self,
        root: tk.Tk,
        config: ManagerConfig,
        *,
        start_minimized: bool,
    ):
        self.root = root
        self.work_dir = config.work_dir
        self.config = config
        self.config_lock = threading.Lock()
        self.processes = ServerProcessManager(config)
        self.logger = logging.getLogger("cliproxy-manager")
        self.events: queue.Queue[tuple[Any, ...]] = queue.Queue()
        self.stop_event = threading.Event()
        self.update_event = threading.Event()
        self.model_refresh_event = threading.Event()
        self.claude_settings_save_event = threading.Event()
        self.update_guard = threading.Lock()
        self.last_update_attempt = time.monotonic() - UPDATE_INTERVAL_SECONDS
        self.restart_backoff = STATUS_INTERVAL_SECONDS
        self.next_restart_at = 0.0
        self.unhealthy_count = 0
        self.notified_expiries: set[str] = set()
        self.auth_records: list[AuthRecord] = []
        self.server_status = ServerStatus(False, False, ())
        self.expiry_dialog: tk.Toplevel | None = None
        self.start_minimized = start_minimized
        self.current_version = read_binary_version(config.executable_path)
        self.available_model_ids: tuple[str, ...] = ()
        self.model_refresh_attempted = False
        self.login_flags = supported_login_flags(config.executable_path)
        self.login_by_label = {
            LOGIN_LABELS.get(flag, flag): flag for flag in self.login_flags
        }
        self.claude_settings_path = claude_code_settings_path()
        claude_settings_error: str | None = None
        try:
            claude_settings = load_claude_code_model_settings(
                self.claude_settings_path
            )
        except Exception as exc:
            self.logger.warning("Claude Code settings load failed: %s", exc)
            claude_settings = ClaudeCodeModelSettings(None, None, None, None, None)
            claude_settings_error = str(exc)

        self.server_text = tk.StringVar(value="확인 중…")
        self.server_detail = tk.StringVar(value="")
        self.version_text = tk.StringVar(
            value=f"설치 버전: v{self.current_version or '알 수 없음'}"
        )
        self.update_text = tk.StringVar(value="업데이트: 시작 후 자동 확인")
        self.last_checked_text = tk.StringVar(value="마지막 상태 확인: -")
        self.autostart_var = tk.BooleanVar(value=bool(read_startup_command()))
        self.login_selection = tk.StringVar(
            value=next(iter(self.login_by_label), "")
        )
        self.subagent_model_var = tk.StringVar(
            value=claude_settings.subagent_model or SUBAGENT_INHERIT_VALUE
        )
        self.fable_model_var = tk.StringVar(
            value=claude_settings.fable_model or MODEL_DEFAULT_VALUE
        )
        self.opus_model_var = tk.StringVar(
            value=claude_settings.opus_model or MODEL_DEFAULT_VALUE
        )
        self.sonnet_model_var = tk.StringVar(
            value=claude_settings.sonnet_model or MODEL_DEFAULT_VALUE
        )
        self.haiku_model_var = tk.StringVar(
            value=claude_settings.haiku_model or MODEL_DEFAULT_VALUE
        )
        initial_claude_status = (
            f"설정 파일 오류: {claude_settings_error}"
            if claude_settings_error
            else f"전역 설정: {self.claude_settings_path}"
        )
        self.claude_settings_status = tk.StringVar(value=initial_claude_status)

        self._configure_window()
        self._build_window()
        self.tray = pystray.Icon(
            "cliproxyapi-manager",
            _tray_image("#e8a23a"),
            APP_NAME,
            self._tray_menu(),
        )
        self.tray.run_detached()

        self.root.after(150, self._pump_events)
        self.root.after(250, self._initial_visibility)
        threading.Thread(
            target=self._monitor_loop,
            name="status-monitor",
            daemon=True,
        ).start()

    def _configure_window(self) -> None:
        self.root.title(f"{APP_NAME} {APP_VERSION}")
        self.root.geometry("780x720")
        self.root.minsize(700, 650)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)
        self.root.withdraw()

        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Header.TLabel", font=("Segoe UI", 17, "bold"))
        style.configure("Subtle.TLabel", foreground="#667085")
        style.configure("Status.TLabel", font=("Segoe UI", 11, "bold"))
        style.configure("Treeview", rowheight=27)

    def _build_window(self) -> None:
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(outer)
        header.pack(fill=tk.X)
        ttk.Label(header, text="CLIProxyAPI 관리자", style="Header.TLabel").pack(
            side=tk.LEFT
        )
        ttk.Label(
            header,
            text="닫아도 작업표시줄 알림 영역에서 계속 실행됩니다.",
            style="Subtle.TLabel",
        ).pack(side=tk.RIGHT, pady=(7, 0))

        status_box = ttk.LabelFrame(outer, text="서버 상태", padding=12)
        status_box.pack(fill=tk.X, pady=(16, 10))
        self.status_dot = tk.Label(
            status_box,
            text="●",
            fg="#e8a23a",
            font=("Segoe UI", 18),
        )
        self.status_dot.grid(row=0, column=0, rowspan=2, padx=(2, 12))
        ttk.Label(
            status_box, textvariable=self.server_text, style="Status.TLabel"
        ).grid(row=0, column=1, sticky="w")
        ttk.Label(
            status_box, textvariable=self.server_detail, style="Subtle.TLabel"
        ).grid(row=1, column=1, sticky="w", pady=(3, 0))
        ttk.Label(status_box, textvariable=self.version_text).grid(
            row=0, column=2, sticky="e"
        )
        ttk.Label(
            status_box, textvariable=self.last_checked_text, style="Subtle.TLabel"
        ).grid(row=1, column=2, sticky="e", pady=(3, 0))
        status_box.columnconfigure(1, weight=1)

        action_row = ttk.Frame(outer)
        action_row.pack(fill=tk.X, pady=(0, 10))
        ttk.Button(
            action_row, text="서버 다시 시작", command=self.request_restart
        ).pack(side=tk.LEFT)
        ttk.Button(
            action_row, text="지금 업데이트", command=lambda: self.request_update(True)
        ).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(
            action_row, text="설치 폴더 열기", command=self.open_install_folder
        ).pack(side=tk.LEFT, padx=(8, 0))

        login_frame = ttk.Frame(action_row)
        login_frame.pack(side=tk.RIGHT)
        self.login_combo = ttk.Combobox(
            login_frame,
            state="readonly",
            width=18,
            textvariable=self.login_selection,
            values=tuple(self.login_by_label),
        )
        self.login_combo.pack(side=tk.LEFT)
        ttk.Button(
            login_frame,
            text="다시 로그인",
            command=self.request_selected_login,
            state=tk.NORMAL if self.login_by_label else tk.DISABLED,
        ).pack(side=tk.LEFT, padx=(6, 0))

        claude_box = ttk.LabelFrame(
            outer,
            text="Claude Code 전역 모델 설정",
            padding=(10, 8),
        )
        claude_box.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(claude_box, text="전체 서브에이전트").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=2
        )
        self.subagent_model_combo = ttk.Combobox(
            claude_box,
            state="normal",
            width=34,
            textvariable=self.subagent_model_var,
            values=model_choice_values(
                SUBAGENT_INHERIT_VALUE,
                self.subagent_model_var.get(),
                self.available_model_ids,
            ),
        )
        self.subagent_model_combo.grid(row=0, column=1, sticky="ew", pady=2)

        model_rows = (
            ("Fable 기본", self.fable_model_var, "fable_model_combo"),
            ("Opus 기본", self.opus_model_var, "opus_model_combo"),
            ("Sonnet 기본", self.sonnet_model_var, "sonnet_model_combo"),
            ("Haiku 기본 / 백그라운드", self.haiku_model_var, "haiku_model_combo"),
        )
        for row, (label, variable, attribute) in enumerate(model_rows, start=1):
            ttk.Label(claude_box, text=label).grid(
                row=row, column=0, sticky="w", padx=(0, 8), pady=2
            )
            combo = ttk.Combobox(
                claude_box,
                state="normal",
                width=34,
                textvariable=variable,
                values=model_choice_values(
                    MODEL_DEFAULT_VALUE,
                    variable.get(),
                    self.available_model_ids,
                ),
            )
            combo.grid(row=row, column=1, sticky="ew", pady=2)
            setattr(self, attribute, combo)

        claude_buttons = ttk.Frame(claude_box)
        claude_buttons.grid(row=0, column=2, rowspan=5, padx=(10, 0))
        self.refresh_models_button = ttk.Button(
            claude_buttons,
            text="모델 목록 새로고침",
            command=lambda: self.request_model_refresh(True),
        )
        self.refresh_models_button.pack(fill=tk.X)
        self.apply_claude_connection_button = ttk.Button(
            claude_buttons,
            text="CLIProxyAPI 연결 적용",
            command=self.request_claude_connection_apply,
        )
        self.apply_claude_connection_button.pack(fill=tk.X, pady=(6, 0))
        self.save_claude_settings_button = ttk.Button(
            claude_buttons,
            text="전역 모델 설정 저장",
            command=self.request_claude_settings_save,
        )
        self.save_claude_settings_button.pack(fill=tk.X, pady=(6, 0))

        ttk.Label(
            claude_box,
            text=(
                "전체 서브에이전트가 inherit이면 각 에이전트의 모델 계층이 "
                "Fable·Opus·Sonnet·Haiku 기본값으로 해석됩니다."
            ),
            style="Subtle.TLabel",
            wraplength=680,
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(6, 1))
        ttk.Label(
            claude_box,
            textvariable=self.claude_settings_status,
            style="Subtle.TLabel",
            wraplength=680,
        ).grid(row=6, column=0, columnspan=3, sticky="w")
        claude_box.columnconfigure(1, weight=1)

        auth_box = ttk.LabelFrame(outer, text="인증 토큰", padding=(10, 8))
        auth_box.pack(fill=tk.BOTH, expand=True)
        columns = ("provider", "account", "expiry", "status")
        self.auth_tree = ttk.Treeview(
            auth_box,
            columns=columns,
            show="headings",
            selectmode="browse",
            height=6,
        )
        self.auth_tree.heading("provider", text="공급자")
        self.auth_tree.heading("account", text="계정")
        self.auth_tree.heading("expiry", text="만료 시각")
        self.auth_tree.heading("status", text="상태")
        self.auth_tree.column("provider", width=95, anchor=tk.W, stretch=False)
        self.auth_tree.column("account", width=255, anchor=tk.W)
        self.auth_tree.column("expiry", width=150, anchor=tk.CENTER)
        self.auth_tree.column("status", width=90, anchor=tk.CENTER, stretch=False)
        scrollbar = ttk.Scrollbar(
            auth_box, orient=tk.VERTICAL, command=self.auth_tree.yview
        )
        self.auth_tree.configure(yscrollcommand=scrollbar.set)
        self.auth_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.auth_tree.tag_configure("expired", foreground="#c7362f")
        self.auth_tree.tag_configure("invalid", foreground="#c7362f")
        self.auth_tree.tag_configure("expiring", foreground="#b26a00")
        self.auth_tree.tag_configure("refreshing", foreground="#b26a00")
        self.auth_tree.tag_configure("disabled", foreground="#7a7a7a")

        footer = ttk.Frame(outer)
        footer.pack(fill=tk.X, pady=(10, 0))
        ttk.Checkbutton(
            footer,
            text="Windows 로그인 시 자동 실행",
            variable=self.autostart_var,
            command=self.toggle_autostart,
        ).pack(side=tk.LEFT)
        ttk.Label(
            footer, textvariable=self.update_text, style="Subtle.TLabel"
        ).pack(side=tk.RIGHT)

    def _tray_menu(self) -> pystray.Menu:
        def login_action(flag: str) -> Callable[[pystray.Icon, Any], None]:
            def action(_icon: pystray.Icon, _item: Any) -> None:
                self.events.put(("ui-action", "login", flag))

            return action

        login_items = [
            pystray.MenuItem(
                label,
                login_action(flag),
            )
            for label, flag in self.login_by_label.items()
        ]
        if not login_items:
            login_items = [
                pystray.MenuItem("지원 옵션 없음", lambda: None, enabled=False)
            ]
        return pystray.Menu(
            pystray.MenuItem(
                "관리 창 열기",
                lambda _icon, _item: self.events.put(("ui-action", "show")),
                default=True,
            ),
            pystray.MenuItem(
                "서버 다시 시작",
                lambda _icon, _item: self.events.put(("ui-action", "restart")),
            ),
            pystray.MenuItem(
                "지금 업데이트",
                lambda _icon, _item: self.events.put(("ui-action", "update")),
            ),
            pystray.MenuItem("다시 로그인", pystray.Menu(*login_items)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Windows 로그인 시 자동 실행",
                lambda _icon, _item: self.events.put(("ui-action", "autostart")),
                checked=lambda _item: bool(read_startup_command()),
            ),
            pystray.MenuItem(
                "관리자 종료 (서버 유지)",
                lambda _icon, _item: self.events.put(("ui-action", "quit")),
            ),
        )

    def _initial_visibility(self) -> None:
        if not self.start_minimized:
            self.show_window()

    def _pump_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        if not self.stop_event.is_set():
            self.root.after(250, self._pump_events)

    def _handle_event(self, event: tuple[Any, ...]) -> None:
        kind = event[0]
        if kind == "snapshot":
            _, status, records, version = event
            self._render_snapshot(status, records, version)
        elif kind == "notify":
            _, title, message = event
            self._notify(title, message)
        elif kind == "expiry":
            _, records = event
            self._show_expiry_dialog(records)
        elif kind == "update-progress":
            self.update_text.set(f"업데이트: {event[1]}")
        elif kind == "update-done":
            result = event[1]
            self.update_text.set(f"업데이트: {result.message}")
            if result.status == "updated":
                self.current_version = result.latest_version
                self._notify("CLIProxyAPI 업데이트", result.message)
                self._refresh_login_options()
            elif result.status == "failed":
                self._notify("CLIProxyAPI 업데이트 실패", result.message)
        elif kind == "restart-done":
            status = event[1]
            if status.healthy:
                self._notify("CLIProxyAPI", "서버를 다시 시작했습니다.")
            else:
                self._notify(
                    "CLIProxyAPI 시작 실패", status.error or "서버 응답이 없습니다."
                )
        elif kind == "login-started":
            self.update_text.set(f"인증: {event[1]} 로그인 창을 열었습니다.")
        elif kind == "login-done":
            _, label, return_code = event
            if return_code == 0:
                self._notify("인증 완료", f"{label} 로그인이 완료되었습니다.")
            else:
                self._notify(
                    "인증 미완료",
                    f"{label} 로그인 프로세스가 코드 {return_code}(으)로 끝났습니다.",
                )
        elif kind == "models-loaded":
            _, model_ids, manual = event
            self._finish_model_refresh(model_ids, None, manual)
        elif kind == "models-load-failed":
            _, error, manual = event
            self._finish_model_refresh((), error, manual)
        elif kind == "claude-settings-saved":
            self._finish_claude_settings_save(None)
        elif kind == "claude-settings-save-failed":
            self._finish_claude_settings_save(event[1])
        elif kind == "claude-connection-applied":
            self._finish_claude_connection_apply(None, event[1])
        elif kind == "claude-connection-apply-failed":
            self._finish_claude_connection_apply(event[1], None)
        elif kind == "ui-action":
            self._handle_ui_action(*event[1:])

    def _handle_ui_action(self, action: str, value: str | None = None) -> None:
        if action == "show":
            self.show_window()
        elif action == "restart":
            self.request_restart()
        elif action == "update":
            self.request_update(True)
        elif action == "login" and value:
            self.request_login(value)
        elif action == "autostart":
            self.autostart_var.set(not bool(read_startup_command()))
            self.toggle_autostart()
        elif action == "quit":
            self.quit_manager()

    def _reload_runtime_config(self) -> ManagerConfig:
        config = load_config(self.work_dir)
        with self.config_lock:
            self.processes.update_config(config)
            self.config = config
        return config

    def _render_snapshot(
        self,
        status: ServerStatus,
        records: list[AuthRecord],
        version: str | None,
    ) -> None:
        self.server_status = status
        self.auth_records = records
        if status.running and status.healthy:
            self.status_dot.configure(fg="#2f9e62")
            self.server_text.set("실행 중")
            detail = f"PID {', '.join(map(str, status.pids))} · HTTP {status.http_status}"
            tray_color = "#2f9e62"
        elif status.running:
            self.status_dot.configure(fg="#e8a23a")
            self.server_text.set("응답 확인 중")
            detail = f"PID {', '.join(map(str, status.pids))}"
            tray_color = "#e8a23a"
        else:
            self.status_dot.configure(fg="#c7362f")
            self.server_text.set("중지됨 · 자동 복구 대기")
            detail = status.error or ""
            tray_color = "#c7362f"
        self.server_detail.set(detail)
        self.version_text.set(f"설치 버전: v{version or '알 수 없음'}")
        self.last_checked_text.set(
            f"마지막 상태 확인: {dt.datetime.now():%H:%M:%S}"
        )
        self.tray.icon = _tray_image(tray_color)
        self.tray.title = f"{APP_NAME} - {self.server_text.get()}"
        if status.healthy and not self.model_refresh_attempted:
            self.model_refresh_attempted = True
            self.request_model_refresh(False)

        for item_id in self.auth_tree.get_children():
            self.auth_tree.delete(item_id)
        for record in records:
            expiry = (
                record.expires_at.astimezone().strftime("%Y-%m-%d %H:%M")
                if record.expires_at
                else "-"
            )
            self.auth_tree.insert(
                "",
                tk.END,
                values=(
                    record.provider,
                    record.account,
                    expiry,
                    STATUS_LABELS.get(record.status, record.status),
                ),
                tags=(record.status,),
            )

    def _monitor_loop(self) -> None:
        while not self.stop_event.is_set():
            # Updating briefly stops the backend. The updater owns supervision
            # during that window, so the regular watchdog stays out of the way.
            if not self.update_event.is_set():
                try:
                    config = self._reload_runtime_config()
                    status = self.processes.inspect()
                    now = time.monotonic()
                    if not status.running and now >= self.next_restart_at:
                        try:
                            self.logger.warning("Backend is stopped; starting it")
                            self.processes.start()
                            status = self.processes.wait_healthy(timeout=15)
                            if status.healthy:
                                self.events.put(
                                    (
                                        "notify",
                                        "CLIProxyAPI 자동 복구",
                                        "꺼져 있던 서버를 다시 시작했습니다.",
                                    )
                                )
                                self.restart_backoff = STATUS_INTERVAL_SECONDS
                                self.next_restart_at = 0
                            else:
                                raise RuntimeError(status.error or "응답 없음")
                        except Exception as exc:
                            self.logger.exception("Automatic start failed")
                            self.next_restart_at = now + self.restart_backoff
                            self.restart_backoff = min(
                                self.restart_backoff * 2,
                                MAX_RESTART_BACKOFF_SECONDS,
                            )
                            status = ServerStatus(
                                False, False, (), error=f"자동 시작 실패: {exc}"
                            )
                    elif status.running and not status.healthy:
                        self.unhealthy_count += 1
                        if self.unhealthy_count >= 3:
                            self.logger.warning(
                                "Backend was unhealthy for three checks; restarting"
                            )
                            status = self.processes.restart()
                            self.unhealthy_count = 0
                            self.events.put(
                                (
                                    "notify",
                                    "CLIProxyAPI 자동 복구",
                                    (
                                        "응답이 없던 서버를 다시 시작했습니다."
                                        if status.healthy
                                        else "서버 재시작 후에도 응답이 없습니다."
                                    ),
                                )
                            )
                    else:
                        self.unhealthy_count = 0
                        self.restart_backoff = STATUS_INTERVAL_SECONDS
                        self.next_restart_at = 0

                    records = scan_auth_records(config.auth_dir)
                    # CLIProxyAPI normally refreshes short-lived access tokens.
                    # scan_auth_records gives that refresh a five-minute grace
                    # period before an account is reported as expired.
                    expired = [
                        record
                        for record in records
                        if record.status == "expired"
                        and record.notification_key not in self.notified_expiries
                    ]
                    current_expired = {
                        record.notification_key
                        for record in records
                        if record.status == "expired"
                    }
                    self.notified_expiries.intersection_update(current_expired)
                    if expired:
                        self.notified_expiries.update(
                            record.notification_key for record in expired
                        )
                        providers = ", ".join(
                            sorted({record.provider for record in expired})
                        )
                        self.events.put(
                            (
                                "notify",
                                "인증 토큰 만료",
                                f"{providers} 인증을 다시 로그인해야 합니다.",
                            )
                        )
                        self.events.put(("expiry", expired))

                    self.events.put(
                        ("snapshot", status, records, self.current_version)
                    )
                except Exception:
                    self.logger.exception("Status monitor iteration failed")

                if (
                    time.monotonic() - self.last_update_attempt
                    >= UPDATE_INTERVAL_SECONDS
                ):
                    self.request_update(False)

            self.stop_event.wait(STATUS_INTERVAL_SECONDS)

    def _refresh_model_choices(self) -> None:
        self.subagent_model_combo.configure(
            values=model_choice_values(
                SUBAGENT_INHERIT_VALUE,
                self.subagent_model_var.get(),
                self.available_model_ids,
            )
        )
        for variable, combo in (
            (self.fable_model_var, self.fable_model_combo),
            (self.opus_model_var, self.opus_model_combo),
            (self.sonnet_model_var, self.sonnet_model_combo),
            (self.haiku_model_var, self.haiku_model_combo),
        ):
            combo.configure(
                values=model_choice_values(
                    MODEL_DEFAULT_VALUE,
                    variable.get(),
                    self.available_model_ids,
                )
            )

    def request_model_refresh(self, manual: bool) -> None:
        if self.model_refresh_event.is_set():
            if manual:
                self.claude_settings_status.set("모델 목록을 이미 조회 중입니다.")
            return
        self.model_refresh_event.set()
        self.refresh_models_button.configure(state=tk.DISABLED)
        self.claude_settings_status.set("CLIProxyAPI 모델 목록 조회 중…")
        threading.Thread(
            target=self._model_refresh_worker,
            args=(manual,),
            name="model-list-refresh",
            daemon=True,
        ).start()

    def _model_refresh_worker(self, manual: bool) -> None:
        try:
            config = self._reload_runtime_config()
            model_ids = fetch_cliproxy_model_ids(config)
            self.events.put(("models-loaded", model_ids, manual))
        except Exception as exc:
            self.logger.warning("CLIProxyAPI model list refresh failed: %s", exc)
            self.events.put(("models-load-failed", str(exc), manual))

    def _finish_model_refresh(
        self,
        model_ids: tuple[str, ...],
        error: str | None,
        manual: bool,
    ) -> None:
        self.model_refresh_event.clear()
        self.refresh_models_button.configure(state=tk.NORMAL)
        if error:
            self.claude_settings_status.set(
                f"모델 목록 조회 실패: {error} · 모델 ID를 직접 입력할 수 있습니다."
            )
            if manual:
                messagebox.showerror(
                    "모델 목록 조회 실패",
                    f"{error}\n\n현재 입력값은 유지되며 모델 ID를 직접 입력할 수 있습니다.",
                )
            return

        self.available_model_ids = tuple(model_ids)
        self._refresh_model_choices()
        self.claude_settings_status.set(
            f"CLIProxyAPI에서 모델 {len(model_ids)}개를 불러왔습니다."
        )

    def _set_claude_settings_write_state(self, busy: bool) -> None:
        state = tk.DISABLED if busy else tk.NORMAL
        for combo in (
            self.subagent_model_combo,
            self.fable_model_combo,
            self.opus_model_combo,
            self.sonnet_model_combo,
            self.haiku_model_combo,
        ):
            combo.configure(state=state)
        self.apply_claude_connection_button.configure(state=state)
        self.save_claude_settings_button.configure(state=state)

    def request_claude_settings_save(self) -> None:
        if self.claude_settings_save_event.is_set():
            self.claude_settings_status.set("Claude Code 설정을 이미 저장 중입니다.")
            return
        settings = ClaudeCodeModelSettings(
            subagent_model=model_override_from_display(
                self.subagent_model_var.get(),
                SUBAGENT_INHERIT_VALUE,
            ),
            fable_model=model_override_from_display(
                self.fable_model_var.get(),
                MODEL_DEFAULT_VALUE,
            ),
            opus_model=model_override_from_display(
                self.opus_model_var.get(),
                MODEL_DEFAULT_VALUE,
            ),
            sonnet_model=model_override_from_display(
                self.sonnet_model_var.get(),
                MODEL_DEFAULT_VALUE,
            ),
            haiku_model=model_override_from_display(
                self.haiku_model_var.get(),
                MODEL_DEFAULT_VALUE,
            ),
        )
        self.claude_settings_save_event.set()
        self._set_claude_settings_write_state(True)
        self.claude_settings_status.set("Claude Code 전역 모델 설정 저장 중…")
        threading.Thread(
            target=self._claude_settings_save_worker,
            args=(settings,),
            name="claude-settings-save",
            daemon=True,
        ).start()

    def _claude_settings_save_worker(
        self,
        settings: ClaudeCodeModelSettings,
    ) -> None:
        try:
            save_claude_code_model_settings(settings, self.claude_settings_path)
            self.events.put(("claude-settings-saved",))
        except Exception as exc:
            self.logger.warning("Claude Code settings save failed: %s", exc)
            self.events.put(("claude-settings-save-failed", str(exc)))

    def _finish_claude_settings_save(self, error: str | None) -> None:
        self.claude_settings_save_event.clear()
        self._set_claude_settings_write_state(False)
        if error:
            self.claude_settings_status.set(f"전역 모델 설정 저장 실패: {error}")
            messagebox.showerror(
                "Claude Code 설정 저장 실패",
                f"{error}\n\n기존 settings.json은 변경되지 않았습니다.",
            )
            return

        self.claude_settings_status.set(
            f"전역 모델 설정을 저장했습니다: {self.claude_settings_path}"
        )
        messagebox.showinfo(
            "Claude Code 설정 저장 완료",
            (
                "전역 모델 설정을 저장했습니다.\n\n"
                "프로젝트·로컬·관리 설정이 이 값을 덮을 수 있습니다. "
                "새 Claude Code 세션을 시작해 적용 상태를 확인하세요."
            ),
        )

    def request_claude_connection_apply(self) -> None:
        if self.claude_settings_save_event.is_set():
            self.claude_settings_status.set("Claude Code 설정을 이미 저장 중입니다.")
            return
        self.claude_settings_save_event.set()
        self._set_claude_settings_write_state(True)
        self.claude_settings_status.set("CLIProxyAPI 연결을 Claude Code에 적용 중…")
        threading.Thread(
            target=self._claude_connection_apply_worker,
            name="claude-connection-apply",
            daemon=True,
        ).start()

    def _claude_connection_apply_worker(self) -> None:
        try:
            config = self._reload_runtime_config()
            save_claude_code_connection_settings(
                ClaudeCodeConnectionSettings(
                    base_url=config.base_url,
                    auth_token=config.api_key or "",
                ),
                self.claude_settings_path,
            )
            self.events.put(("claude-connection-applied", config.base_url))
        except Exception as exc:
            self.logger.warning("Claude Code connection apply failed: %s", exc)
            self.events.put(("claude-connection-apply-failed", str(exc)))

    def _finish_claude_connection_apply(
        self,
        error: str | None,
        base_url: str | None,
    ) -> None:
        self.claude_settings_save_event.clear()
        self._set_claude_settings_write_state(False)
        if error:
            self.claude_settings_status.set(f"CLIProxyAPI 연결 적용 실패: {error}")
            messagebox.showerror(
                "Claude Code 연결 적용 실패",
                f"{error}\n\n기존 settings.json은 변경되지 않았습니다.",
            )
            return

        self.claude_settings_status.set(
            f"CLIProxyAPI 연결을 적용했습니다: {base_url}"
        )
        messagebox.showinfo(
            "Claude Code 연결 적용 완료",
            (
                f"Claude Code 전역 연결을 {base_url}(으)로 설정했습니다.\n\n"
                "새 Claude Code 세션에서 /status로 연결과 인증 소스를 확인하세요."
            ),
        )

    def request_update(self, manual: bool) -> None:
        with self.update_guard:
            if self.update_event.is_set():
                if manual:
                    self.update_text.set("업데이트: 이미 확인 중입니다.")
                return
            self.update_event.set()
            self.last_update_attempt = time.monotonic()
        if manual:
            self.update_text.set("업데이트: 확인을 시작합니다.")
        threading.Thread(
            target=self._update_worker,
            name="backend-updater",
            daemon=True,
        ).start()

    def _update_worker(self) -> None:
        try:
            config = self._reload_runtime_config()
            updater = BackendUpdater(
                config,
                self.processes,
                progress=lambda message: self.events.put(
                    ("update-progress", message)
                ),
            )
            result = updater.run()
        except Exception as exc:
            self.logger.exception("Backend update setup failed")
            result = UpdateResult(
                "failed",
                self.current_version,
                None,
                f"업데이트 확인 실패: {exc}",
            )
        finally:
            self.update_event.clear()
        self.events.put(("update-done", result))

    def request_restart(self) -> None:
        if self.update_event.is_set():
            self.update_text.set("업데이트 중에는 재시작할 수 없습니다.")
            return
        self.update_text.set("서버: 다시 시작하는 중…")
        threading.Thread(
            target=self._restart_worker,
            name="manual-restart",
            daemon=True,
        ).start()

    def _restart_worker(self) -> None:
        try:
            self._reload_runtime_config()
            status = self.processes.restart()
        except Exception as exc:
            self.logger.exception("Manual restart failed")
            status = ServerStatus(False, False, (), error=str(exc))
        self.events.put(("restart-done", status))

    def request_selected_login(self) -> None:
        flag = self.login_by_label.get(self.login_selection.get())
        if flag:
            self.request_login(flag)

    def request_login(self, flag: str) -> None:
        label = LOGIN_LABELS.get(flag, flag)
        threading.Thread(
            target=self._login_worker,
            args=(flag, label),
            name=f"login-{flag}",
            daemon=True,
        ).start()
        if self.expiry_dialog and self.expiry_dialog.winfo_exists():
            self.expiry_dialog.destroy()
            self.expiry_dialog = None

    def _login_worker(self, flag: str, label: str) -> None:
        try:
            self._reload_runtime_config()
            process = self.processes.launch_login(flag)
            self.events.put(("login-started", label))
            return_code = process.wait()
            self.events.put(("login-done", label, return_code))
        except Exception as exc:
            self.logger.exception("Login launch failed")
            self.events.put(
                (
                    "notify",
                    "로그인 창 열기 실패",
                    f"{label}: {exc}",
                )
            )

    def _refresh_login_options(self) -> None:
        self.login_flags = supported_login_flags(self.config.executable_path)
        self.login_by_label = {
            LOGIN_LABELS.get(flag, flag): flag for flag in self.login_flags
        }
        values = tuple(self.login_by_label)
        self.login_combo.configure(values=values)
        self.login_selection.set(values[0] if values else "")
        self.tray.menu = self._tray_menu()
        self.tray.update_menu()

    def _show_expiry_dialog(self, records: list[AuthRecord]) -> None:
        if self.expiry_dialog and self.expiry_dialog.winfo_exists():
            self.expiry_dialog.deiconify()
            self.expiry_dialog.lift()
            return

        dialog = tk.Toplevel(self.root)
        self.expiry_dialog = dialog
        dialog.title("CLIProxyAPI 인증 만료")
        dialog.resizable(False, False)
        dialog.attributes("-topmost", True)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            frame,
            text="인증 토큰이 만료되었습니다.",
            style="Status.TLabel",
        ).pack(anchor=tk.W)
        ttk.Label(
            frame,
            text="공급자를 선택해 브라우저 로그인 절차를 다시 진행하세요.",
            style="Subtle.TLabel",
        ).pack(anchor=tk.W, pady=(4, 12))

        for record in records:
            ttk.Label(
                frame,
                text=f"• {record.provider} — {record.account}",
            ).pack(anchor=tk.W, pady=2)

        button_row = ttk.Frame(frame)
        button_row.pack(fill=tk.X, pady=(14, 0))
        available_flags: list[str] = []
        for provider in dict.fromkeys(record.provider.casefold() for record in records):
            preferred = f"{provider}-login"
            if preferred in self.login_flags:
                available_flags.append(preferred)
        for flag in available_flags:
            ttk.Button(
                button_row,
                text=f"{LOGIN_LABELS.get(flag, flag)} 다시 로그인",
                command=lambda login_flag=flag: self.request_login(login_flag),
            ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(
            button_row,
            text="나중에",
            command=dialog.destroy,
        ).pack(side=tk.RIGHT)

        dialog.update_idletasks()
        x = dialog.winfo_screenwidth() - dialog.winfo_width() - 30
        y = dialog.winfo_screenheight() - dialog.winfo_height() - 80
        dialog.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _notify(self, title: str, message: str) -> None:
        self.update_text.set(message)
        try:
            self.tray.notify(message, title)
        except Exception:
            self.logger.exception("Tray notification failed")

    def toggle_autostart(self) -> None:
        try:
            if self.autostart_var.get():
                register_startup(manager_startup_command())
                self.update_text.set("시작프로그램에 등록했습니다.")
            else:
                unregister_startup()
                self.update_text.set("시작프로그램 등록을 해제했습니다.")
            self.tray.update_menu()
        except Exception as exc:
            self.autostart_var.set(bool(read_startup_command()))
            messagebox.showerror(APP_NAME, f"시작프로그램 설정 실패:\n{exc}")

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide_window(self) -> None:
        self.root.withdraw()

    def open_install_folder(self) -> None:
        os.startfile(self.config.work_dir)

    def quit_manager(self) -> None:
        if not messagebox.askyesno(
            APP_NAME,
            "관리자만 종료할까요?\nCLIProxyAPI 서버는 계속 실행됩니다.",
        ):
            return
        self.stop_event.set()
        try:
            self.tray.stop()
        finally:
            self.root.destroy()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument(
        "--minimized",
        action="store_true",
        help="창을 열지 않고 알림 영역에서 시작",
    )
    parser.add_argument(
        "--install-startup",
        action="store_true",
        help="현재 실행 파일을 Windows 시작프로그램에 등록",
    )
    parser.add_argument(
        "--remove-startup",
        action="store_true",
        help="Windows 시작프로그램 등록 해제",
    )
    return parser.parse_args()


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def find_backend_dir(app_dir: Path) -> Path:
    """Find config.yaml and cli-proxy-api.exe beside or above the manager."""

    app_dir = app_dir.resolve()
    for candidate in (app_dir, app_dir.parent):
        if (candidate / "config.yaml").is_file() and (
            candidate / "cli-proxy-api.exe"
        ).is_file():
            return candidate
    raise FileNotFoundError(
        "cli-proxy-api.exe와 config.yaml을 찾을 수 없습니다.\n"
        "관리자 EXE를 CLIProxyAPI 폴더 또는 바로 아래 폴더에 놓아 주세요."
    )


def main() -> int:
    args = _parse_args()
    app_dir = _app_dir()
    configure_logging(app_dir)

    if args.install_startup:
        register_startup(manager_startup_command())
        return 0
    if args.remove_startup:
        unregister_startup()
        return 0

    single_instance = WindowsSingleInstance(r"Local\CLIProxyAPIManager")
    if single_instance.already_running:
        if not args.minimized:
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo(APP_NAME, "CLIProxyAPI 관리자가 이미 실행 중입니다.")
            root.destroy()
        single_instance.close()
        return 0

    try:
        config = load_config(find_backend_dir(app_dir))
    except Exception as exc:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP_NAME, str(exc))
        root.destroy()
        single_instance.close()
        return 1

    root = tk.Tk()
    app = ManagerApp(root, config, start_minimized=args.minimized)
    try:
        root.mainloop()
    finally:
        app.stop_event.set()
        single_instance.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
