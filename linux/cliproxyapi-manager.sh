#!/usr/bin/env bash

set -Eeuo pipefail

MANAGER_SOURCE="${BASH_SOURCE[0]}"
while [[ -L "$MANAGER_SOURCE" ]]; do
    manager_link_dir="$(CDPATH='' cd -- "$(dirname -- "$MANAGER_SOURCE")" && pwd -P)"
    manager_link_target="$(readlink -- "$MANAGER_SOURCE")"
    if [[ "$manager_link_target" == /* ]]; then
        MANAGER_SOURCE="$manager_link_target"
    else
        MANAGER_SOURCE="$manager_link_dir/$manager_link_target"
    fi
done
INSTALL_DIR="$(CDPATH='' cd -- "$(dirname -- "$MANAGER_SOURCE")" && pwd -P)"
BACKEND="$INSTALL_DIR/cli-proxy-api"
CONFIG="$INSTALL_DIR/config.yaml"
PID_FILE="$INSTALL_DIR/cli-proxy-api.pid"
LOG_FILE="$INSTALL_DIR/cli-proxy-api.log"
ERROR_LOG_FILE="$INSTALL_DIR/cli-proxy-api.error.log"
INSTALLER="$INSTALL_DIR/install.sh"
SERVICE_NAME="cliproxyapi-manager.service"
SERVICE_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$SERVICE_NAME"

usage() {
    cat <<'EOF'
CLIProxyAPI Manager (Linux)

Usage:
  cliproxyapi-manager start
  cliproxyapi-manager stop
  cliproxyapi-manager restart
  cliproxyapi-manager status
  cliproxyapi-manager logs
  cliproxyapi-manager login <provider>
  cliproxyapi-manager update
  cliproxyapi-manager version

Providers are validated against the login flags supported by the installed
CLIProxyAPI binary (for example: codex, claude, gemini, antigravity).
EOF
}

die() {
    printf 'cliproxyapi-manager: %s\n' "$*" >&2
    exit 1
}

require_installation() {
    [[ -x "$BACKEND" ]] || die "backend not found: $BACKEND"
    [[ -f "$CONFIG" ]] || die "config not found: $CONFIG"
}

user_systemd_available() {
    command -v systemctl >/dev/null 2>&1 &&
        [[ -f "$SERVICE_FILE" ]] &&
        systemctl --user show-environment >/dev/null 2>&1
}

fallback_pid() {
    [[ -f "$PID_FILE" ]] || return 1
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    printf '%s\n' "$pid"
}

start_fallback() {
    local pid
    if pid="$(fallback_pid)"; then
        printf 'CLIProxyAPI is already running (PID %s).\n' "$pid"
        return 0
    fi
    rm -f -- "$PID_FILE"
    nohup "$BACKEND" -config "$CONFIG" >>"$LOG_FILE" 2>>"$ERROR_LOG_FILE" </dev/null &
    pid=$!
    printf '%s\n' "$pid" >"$PID_FILE"
    for _ in {1..30}; do
        if kill -0 "$pid" 2>/dev/null; then
            printf 'CLIProxyAPI started (PID %s).\n' "$pid"
            return 0
        fi
        sleep 0.25
    done
    rm -f -- "$PID_FILE"
    die "backend exited during startup; see $ERROR_LOG_FILE"
}

stop_fallback() {
    local pid
    if ! pid="$(fallback_pid)"; then
        rm -f -- "$PID_FILE"
        printf 'CLIProxyAPI is not running.\n'
        return 0
    fi
    kill "$pid"
    for _ in {1..40}; do
        if ! kill -0 "$pid" 2>/dev/null; then
            rm -f -- "$PID_FILE"
            printf 'CLIProxyAPI stopped.\n'
            return 0
        fi
        sleep 0.25
    done
    kill -KILL "$pid" 2>/dev/null || true
    rm -f -- "$PID_FILE"
    printf 'CLIProxyAPI stopped forcefully.\n'
}

start_server() {
    require_installation
    if user_systemd_available; then
        systemctl --user start "$SERVICE_NAME"
        systemctl --user --no-pager --full status "$SERVICE_NAME" || true
    else
        start_fallback
    fi
}

stop_server() {
    if user_systemd_available; then
        systemctl --user stop "$SERVICE_NAME"
        printf 'CLIProxyAPI stopped.\n'
    else
        stop_fallback
    fi
}

status_server() {
    require_installation
    if user_systemd_available; then
        systemctl --user --no-pager --full status "$SERVICE_NAME"
        return
    fi
    local pid
    if pid="$(fallback_pid)"; then
        printf 'CLIProxyAPI is running (PID %s, fallback supervisor).\n' "$pid"
        return 0
    fi
    printf 'CLIProxyAPI is stopped.\n'
    return 3
}

show_logs() {
    if user_systemd_available; then
        exec journalctl --user -u "$SERVICE_NAME" -f
    fi
    touch "$LOG_FILE" "$ERROR_LOG_FILE"
    exec tail -n 100 -f "$LOG_FILE" "$ERROR_LOG_FILE"
}

login_provider() {
    require_installation
    local provider="${1:-}"
    [[ "$provider" =~ ^[a-z0-9][a-z0-9-]*$ ]] || die "invalid provider name: $provider"
    local flag="-${provider}-login"
    local help_output
    help_output="$($BACKEND -help 2>&1 || true)"
    grep -Eq "^[[:space:]]+${flag//-/\\-}[[:space:]]*$" <<<"$help_output" ||
        die "installed CLIProxyAPI does not support $flag"
    exec "$BACKEND" "$flag" -config "$CONFIG"
}

update_installation() {
    [[ -x "$INSTALLER" ]] || die "installer not found: $INSTALLER"
    exec "$INSTALLER" --install-dir "$INSTALL_DIR" --skip-claude-code
}

command="${1:-status}"
case "$command" in
    run)
        require_installation
        exec "$BACKEND" -config "$CONFIG"
        ;;
    start)
        start_server
        ;;
    stop)
        stop_server
        ;;
    restart)
        stop_server
        start_server
        ;;
    status)
        status_server
        ;;
    logs)
        show_logs
        ;;
    login)
        login_provider "${2:-}"
        ;;
    update)
        update_installation
        ;;
    version)
        require_installation
        version_output="$($BACKEND -help 2>&1 || true)"
        printf '%s\n' "${version_output%%$'\n'*}"
        ;;
    -h|--help|help)
        usage
        ;;
    *)
        usage >&2
        die "unknown command: $command"
        ;;
esac
