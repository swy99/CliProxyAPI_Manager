#!/usr/bin/env bash

set -Eeuo pipefail

report_error() {
    local exit_code=$?
    printf '::error file=tests/linux_integration.sh,line=%s::Exit %s while running: %s\n' \
        "${BASH_LINENO[0]}" "$exit_code" "$BASH_COMMAND"
    exit "$exit_code"
}
trap report_error ERR

PROJECT_ROOT="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd -P)"

bash -n "$PROJECT_ROOT/install.sh"
bash -n "$PROJECT_ROOT/linux/cliproxyapi-manager.sh"
"$PROJECT_ROOT/install.sh" --skip-claude-code --skip-startup --no-launch

MANAGER="$HOME/.local/bin/cliproxyapi-manager"
[[ -L "$MANAGER" ]]
"$MANAGER" version
"$MANAGER" start
"$MANAGER" status

node <<'NODE'
const fs = require("fs");
const config = fs.readFileSync(`${process.env.HOME}/CLIProxyAPI/config.yaml`, "utf8");
if (!config.includes('alias: "gpt-5.6-sol-fast"')) {
  throw new Error("fast model alias missing from config");
}
if (!config.includes("service_tier: priority")) {
  throw new Error("priority service tier missing from config");
}
const match = /api-keys:\s*\n\s*-\s*"([^"]+)"/.exec(config);
if (!match) throw new Error("API key missing");

(async () => {
  let lastError = "no response";
  for (let attempt = 0; attempt < 30; attempt += 1) {
    try {
      const response = await fetch("http://127.0.0.1:8317/v1/models", {
        headers: { Authorization: `Bearer ${match[1]}` },
      });
      if (response.ok) {
        const models = (await response.json()).data.map((entry) => entry.id);
        console.log(`LIVE_MODEL_COUNT=${models.length}`);
        return;
      }
      lastError = `HTTP ${response.status}`;
    } catch (error) {
      lastError = error.message;
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`health check failed: ${lastError}`);
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
NODE

"$MANAGER" stop
if "$MANAGER" status; then
    printf 'manager unexpectedly remained running\n' >&2
    exit 1
fi

config_hash_before="$(sha256sum "$HOME/CLIProxyAPI/config.yaml" | awk '{print $1}')"
mkdir -p "$HOME/fakebin"
cat >"$HOME/fakebin/systemctl" <<'SYSTEMCTL'
#!/usr/bin/env bash
set -Eeuo pipefail
printf '%s\n' "$*" >>"$HOME/systemctl-calls.log"
exit 0
SYSTEMCTL
chmod 755 "$HOME/fakebin/systemctl"
PATH="$HOME/fakebin:$PATH" "$HOME/CLIProxyAPI/install.sh" \
    --skip-claude-code --no-launch
config_hash_after="$(sha256sum "$HOME/CLIProxyAPI/config.yaml" | awk '{print $1}')"
[[ "$config_hash_before" == "$config_hash_after" ]]
grep -Fq "# CLIProxyAPI_HOME=$HOME/CLIProxyAPI" \
    "$HOME/.config/systemd/user/cliproxyapi-manager.service"
grep -Fq 'ExecStart="' "$HOME/.config/systemd/user/cliproxyapi-manager.service"
grep -Fq -- '--user enable cliproxyapi-manager.service' "$HOME/systemctl-calls.log"
"$MANAGER" version
