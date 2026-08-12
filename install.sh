#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

INSTALLER_VERSION="1.4.0"
RELEASE_API="https://api.github.com/repos/router-for-me/CLIProxyAPI/releases/latest"
SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)"
INSTALL_DIR="$HOME/CLIProxyAPI"
INSTALL_DIR_EXPLICIT=false
SKIP_CLAUDE_CODE=false
SKIP_STARTUP=false
NO_LAUNCH=false
SERVICE_NAME="cliproxyapi-manager.service"
TEMP_DIR=""
BACKUP_PATH=""
BACKEND_REPLACED=false
WAS_RUNNING=false

usage() {
    cat <<'EOF'
CLIProxyAPI Manager Linux installer

Usage:
  npx -y cliproxyapi-manager [options]

Options:
  --install-dir <path>  Installation directory (default: $HOME/CLIProxyAPI)
  --skip-claude-code    Do not install Claude Code
  --skip-startup        Do not register a systemd user service
  --no-launch           Do not launch CLIProxyAPI after installation
  -h, --help            Show this help
EOF
}

log() {
    printf '[CLIProxyAPI] %s\n' "$*"
}

warn() {
    printf '[CLIProxyAPI] warning: %s\n' "$*" >&2
}

die() {
    printf '[CLIProxyAPI] error: %s\n' "$*" >&2
    exit 1
}

cleanup() {
    local exit_code=$?
    if [[ "$exit_code" -ne 0 && "$BACKEND_REPLACED" == true && -n "$BACKUP_PATH" && -f "$BACKUP_PATH" ]]; then
        warn "installation failed; restoring the previous backend"
        cp -f -- "$BACKUP_PATH" "$INSTALL_DIR/cli-proxy-api" || true
        chmod 755 "$INSTALL_DIR/cli-proxy-api" || true
    fi
    if [[ "$exit_code" -ne 0 && "$WAS_RUNNING" == true && -x "$INSTALL_DIR/cliproxyapi-manager" ]]; then
        "$INSTALL_DIR/cliproxyapi-manager" start >/dev/null 2>&1 || true
    fi
    if [[ -n "$TEMP_DIR" && -d "$TEMP_DIR" ]]; then
        rm -rf -- "$TEMP_DIR"
    fi
    return "$exit_code"
}
trap cleanup EXIT

while (($#)); do
    case "$1" in
        --install-dir)
            (($# >= 2)) || die "--install-dir requires a path"
            INSTALL_DIR="$2"
            INSTALL_DIR_EXPLICIT=true
            shift 2
            ;;
        --skip-claude-code)
            SKIP_CLAUDE_CODE=true
            shift
            ;;
        --skip-startup)
            SKIP_STARTUP=true
            shift
            ;;
        --no-launch)
            NO_LAUNCH=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown option: $1"
            ;;
    esac
done

[[ "$(uname -s)" == "Linux" ]] || die "this installer supports Linux only"
command -v node >/dev/null 2>&1 || die "Node.js 18 or newer is required"
command -v curl >/dev/null 2>&1 || die "curl is required"
command -v tar >/dev/null 2>&1 || die "tar is required"
command -v sha256sum >/dev/null 2>&1 || die "sha256sum is required"
command -v realpath >/dev/null 2>&1 || die "realpath is required"

node_major="$(node -p 'Number(process.versions.node.split(".")[0])')"
((node_major >= 18)) || die "Node.js 18 or newer is required"

if [[ "$INSTALL_DIR" == '~' ]]; then
    INSTALL_DIR="$HOME"
elif [[ "$INSTALL_DIR" == [~]/* ]]; then
    INSTALL_DIR="$HOME/${INSTALL_DIR:2}"
fi
INSTALL_DIR="$(realpath -m -- "$INSTALL_DIR")"
HOME_INSTALL_DIR="$(realpath -m -- "$HOME/CLIProxyAPI")"

is_installation() {
    local candidate="$1"
    [[ -d "$candidate" && -x "$candidate/cli-proxy-api" && -f "$candidate/config.yaml" ]]
}

declare -A SEEN_INSTALLATIONS=()
EXISTING_INSTALLATIONS=()
add_existing_installation() {
    local candidate="${1:-}"
    [[ -n "$candidate" ]] || return 0
    candidate="$(realpath -m -- "$candidate")"
    if is_installation "$candidate" && [[ -z "${SEEN_INSTALLATIONS[$candidate]+x}" ]]; then
        SEEN_INSTALLATIONS["$candidate"]=1
        EXISTING_INSTALLATIONS+=("$candidate")
    fi
}

add_existing_installation "$HOME_INSTALL_DIR"
GLOBAL_LINK="$HOME/.local/bin/cliproxyapi-manager"
if [[ -L "$GLOBAL_LINK" ]]; then
    linked_manager="$(readlink -f -- "$GLOBAL_LINK" 2>/dev/null || true)"
    [[ -n "$linked_manager" ]] && add_existing_installation "$(dirname -- "$linked_manager")"
fi
SERVICE_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$SERVICE_NAME"
if [[ -f "$SERVICE_FILE" ]]; then
    service_home="$(sed -n 's/^# CLIProxyAPI_HOME=//p' "$SERVICE_FILE" | head -n 1)"
    add_existing_installation "$service_home"
fi
for process_exe in /proc/[0-9]*/exe; do
    resolved_exe="$(readlink "$process_exe" 2>/dev/null || true)"
    if [[ "$(basename -- "$resolved_exe" 2>/dev/null || true)" == "cli-proxy-api" ]]; then
        add_existing_installation "$(dirname -- "$resolved_exe")"
    fi
done

if [[ "$INSTALL_DIR_EXPLICIT" == true ]]; then
    conflicts=()
    for existing in "${EXISTING_INSTALLATIONS[@]}"; do
        [[ "$existing" == "$INSTALL_DIR" ]] || conflicts+=("$existing")
    done
    ((${#conflicts[@]} == 0)) || die "another installation exists at ${conflicts[*]}; move/remove it or use that path"
elif ((${#EXISTING_INSTALLATIONS[@]} == 1)); then
    INSTALL_DIR="${EXISTING_INSTALLATIONS[0]}"
    log "reusing existing installation: $INSTALL_DIR"
elif ((${#EXISTING_INSTALLATIONS[@]} > 1)); then
    die "multiple installations found: ${EXISTING_INSTALLATIONS[*]}; select one with --install-dir after removing the others"
fi

case "$(uname -m)" in
    x86_64|amd64) RELEASE_ARCH="amd64" ;;
    aarch64|arm64) RELEASE_ARCH="aarch64" ;;
    *) die "unsupported Linux architecture: $(uname -m)" ;;
esac

TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/cliproxyapi-install.XXXXXXXX")"
RELEASE_JSON="$TEMP_DIR/release.json"
log "checking the latest CLIProxyAPI release for linux/$RELEASE_ARCH"
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
    -H 'Accept: application/vnd.github+json' \
    -H 'X-GitHub-Api-Version: 2022-11-28' \
    -H "User-Agent: CLIProxyAPI-Manager-Installer/$INSTALLER_VERSION" \
    --output "$RELEASE_JSON" "$RELEASE_API"

release_tag="$(node -e '
const fs=require("fs"); const r=JSON.parse(fs.readFileSync(process.argv[1],"utf8"));
if(typeof r.tag_name!=="string" || !/^v[0-9]+(?:\.[0-9]+)+$/.test(r.tag_name)) process.exit(2);
process.stdout.write(r.tag_name);
' "$RELEASE_JSON")" || die "invalid GitHub release response"
release_version="${release_tag#v}"
asset_name="CLIProxyAPI_${release_version}_linux_${RELEASE_ARCH}.tar.gz"
mapfile -t asset_info < <(node -e '
const fs=require("fs"); const r=JSON.parse(fs.readFileSync(process.argv[1],"utf8"));
const a=Array.isArray(r.assets)&&r.assets.find(x=>x&&x.name===process.argv[2]);
if(!a || typeof a.browser_download_url!=="string" || typeof a.digest!=="string") process.exit(2);
console.log(a.browser_download_url); console.log(a.digest); console.log(a.size);
' "$RELEASE_JSON" "$asset_name")
((${#asset_info[@]} == 3)) || die "release asset not found: $asset_name"
asset_url="${asset_info[0]}"
asset_digest="${asset_info[1]}"
asset_size="${asset_info[2]}"
[[ "$asset_url" == "https://github.com/router-for-me/CLIProxyAPI/releases/download/"* ]] || die "unexpected asset URL"
[[ "$asset_digest" =~ ^sha256:([0-9a-fA-F]{64})$ ]] || die "release asset has no valid SHA-256 digest"
expected_sha256="${BASH_REMATCH[1],,}"
if [[ ! "$asset_size" =~ ^[0-9]+$ ]] || ((asset_size <= 0 || asset_size > 268435456)); then
    die "invalid release asset size"
fi

archive="$TEMP_DIR/$asset_name"
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
    --output "$archive" "$asset_url"
actual_size="$(stat -c '%s' "$archive")"
[[ "$actual_size" == "$asset_size" ]] || die "downloaded asset size mismatch"
actual_sha256="$(sha256sum "$archive" | awk '{print tolower($1)}')"
[[ "$actual_sha256" == "$expected_sha256" ]] || die "downloaded asset SHA-256 mismatch"

while IFS= read -r entry; do
    case "$entry" in
        /*|../*|*/../*|*/..) die "unsafe archive path: $entry" ;;
    esac
done < <(tar -tzf "$archive")
extract_dir="$TEMP_DIR/extracted"
mkdir -p "$extract_dir"
tar -xzf "$archive" -C "$extract_dir"
mapfile -t backend_candidates < <(find "$extract_dir" -type f -name 'cli-proxy-api' -print)
((${#backend_candidates[@]} == 1)) || die "archive must contain exactly one cli-proxy-api binary"
backend_candidate="${backend_candidates[0]}"
chmod 755 "$backend_candidate"
version_output="$($backend_candidate -help 2>&1 || true)"
grep -Eqi "CLIProxyAPI Version:[[:space:]]*v?${release_version//./\\.}([,[:space:]]|$)" <<<"$version_output" ||
    die "downloaded backend version does not match $release_tag"

mkdir -p "$INSTALL_DIR"
MANAGER="$INSTALL_DIR/cliproxyapi-manager"
if [[ -x "$MANAGER" ]] && "$MANAGER" status >/dev/null 2>&1; then
    WAS_RUNNING=true
    "$MANAGER" stop
fi
if [[ -f "$INSTALL_DIR/cli-proxy-api" ]]; then
    BACKUP_PATH="$INSTALL_DIR/cli-proxy-api.$(date -u +%Y%m%d%H%M%S).bak"
    cp -p -- "$INSTALL_DIR/cli-proxy-api" "$BACKUP_PATH"
fi
install -m 755 "$backend_candidate" "$INSTALL_DIR/.cli-proxy-api.new"
mv -f -- "$INSTALL_DIR/.cli-proxy-api.new" "$INSTALL_DIR/cli-proxy-api"
BACKEND_REPLACED=true

manager_source="$SCRIPT_DIR/linux/cliproxyapi-manager.sh"
if [[ ! -f "$manager_source" && -f "$SCRIPT_DIR/cliproxyapi-manager" ]]; then
    manager_source="$SCRIPT_DIR/cliproxyapi-manager"
fi
[[ -f "$manager_source" ]] || die "Linux manager source is missing"
if [[ "$(realpath -m -- "$manager_source")" != "$(realpath -m -- "$MANAGER")" ]]; then
    install -m 755 "$manager_source" "$MANAGER"
fi
if [[ "$(realpath -m -- "$SCRIPT_DIR/install.sh")" != "$(realpath -m -- "$INSTALL_DIR/install.sh")" ]]; then
    install -m 755 "$SCRIPT_DIR/install.sh" "$INSTALL_DIR/install.sh"
fi

CONFIG="$INSTALL_DIR/config.yaml"
if [[ ! -f "$CONFIG" ]]; then
    api_key="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
    cat >"$CONFIG" <<EOF
host: "127.0.0.1"
port: 8317

tls:
  enable: false

remote-management:
  allow-remote: false
  secret-key: ""

auth-dir: "~/.cli-proxy-api"

api-keys:
  - "$api_key"

debug: false
pprof:
  enable: false
ws-auth: true

oauth-model-alias:
  codex:
    - name: "gpt-5.6-sol"
      alias: "gpt-5.6-sol-fast"
      display-name: "GPT-5.6 Sol Fast"
      fork: true
      force-mapping: true

payload:
  override:
    - models:
        - name: "gpt-5.6-sol-fast"
          protocol: "codex"
      params:
        service_tier: priority
EOF
    chmod 600 "$CONFIG"
    log "created $CONFIG"
else
    log "preserving existing $CONFIG"
fi

mkdir -p "$HOME/.local/bin"
if [[ ! -e "$GLOBAL_LINK" || -L "$GLOBAL_LINK" ]]; then
    ln -sfn -- "$MANAGER" "$GLOBAL_LINK"
else
    warn "$GLOBAL_LINK exists and is not a symlink; leaving it unchanged"
fi

if [[ "$SKIP_CLAUDE_CODE" == false ]] && ! command -v claude >/dev/null 2>&1; then
    claude_installer="$TEMP_DIR/claude-install.sh"
    log "downloading the official Claude Code installer"
    curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
        --max-filesize 5242880 --output "$claude_installer" https://claude.ai/install.sh
    bash "$claude_installer"
fi

mkdir -p "$HOME/.claude"
CLAUDE_SETTINGS="$HOME/.claude/settings.json" node <<'NODE'
const fs = require('fs'); const path = process.env.CLAUDE_SETTINGS;
let data = {}; if (fs.existsSync(path)) { const raw=fs.readFileSync(path,'utf8').replace(/^\uFEFF/,''); data=JSON.parse(raw); }
if (!data || Array.isArray(data) || typeof data !== 'object') throw new Error('Claude settings must be an object');
data.autoCompactEnabled = true;
const temp = `${path}.${process.pid}.tmp`; fs.writeFileSync(temp, `${JSON.stringify(data,null,2)}\n`, {mode:0o600}); fs.renameSync(temp,path);
NODE

user_systemd_available=false
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    user_systemd_available=true
fi
if [[ "$SKIP_STARTUP" == false && "$user_systemd_available" == true ]]; then
    mkdir -p "$(dirname -- "$SERVICE_FILE")"
    escaped_manager="${MANAGER//\\/\\\\}"
    escaped_manager="${escaped_manager//\"/\\\"}"
    escaped_manager="${escaped_manager//%/%%}"
    cat >"$SERVICE_FILE" <<EOF
# CLIProxyAPI_HOME=$INSTALL_DIR
[Unit]
Description=CLIProxyAPI Manager
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart="$escaped_manager" run
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable "$SERVICE_NAME" >/dev/null
    log "registered systemd user service: $SERVICE_NAME"
elif [[ "$SKIP_STARTUP" == false ]]; then
    warn "user systemd is unavailable; use '$MANAGER start' or enable systemd for automatic startup"
fi

if [[ "$NO_LAUNCH" == false ]]; then
    "$MANAGER" start
fi

BACKEND_REPLACED=false
log "installation complete: $INSTALL_DIR"
log "manager command: $GLOBAL_LINK"
log "try: cliproxyapi-manager status"
