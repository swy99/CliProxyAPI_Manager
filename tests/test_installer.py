from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class InstallerContractTests(unittest.TestCase):
    def test_package_manifest_matches_application_version(self) -> None:
        manifest = json.loads(
            (PROJECT_ROOT / "package.json").read_text(encoding="utf-8")
        )
        source = (PROJECT_ROOT / "cliproxy_manager.py").read_text(
            encoding="utf-8"
        )
        match = re.search(r'^APP_VERSION = "([^"]+)"$', source, re.MULTILINE)

        self.assertIsNotNone(match)
        self.assertEqual(manifest["name"], "cliproxyapi-manager")
        self.assertEqual(manifest["version"], match.group(1))
        self.assertEqual(manifest["os"], ["win32"])
        self.assertEqual(
            manifest["bin"], {"cliproxyapi-manager": "bin/cli.js"}
        )
        self.assertEqual(
            manifest["files"],
            ["bin/", "install.ps1", "README.md", "LICENSE"],
        )

    def test_installer_has_verified_release_and_config_preservation_contract(
        self,
    ) -> None:
        installer = (PROJECT_ROOT / "install.ps1").read_text(encoding="utf-8")

        self.assertIn(
            "https://api.github.com/repos/router-for-me/CLIProxyAPI/releases/latest",
            installer,
        )
        self.assertIn(
            "https://api.github.com/repos/swy99/CliProxyAPI_Manager/releases/latest",
            installer,
        )
        self.assertIn("https://claude.ai/install.ps1", installer)
        self.assertIn("function Save-VerifiedReleaseAsset", installer)
        self.assertIn("function Get-ExpectedSha256", installer)
        self.assertIn("$response.ResponseUri.AbsoluteUri", installer)
        self.assertIn('$ManagerAssetName = "CLIProxyAPI-Manager.exe"', installer)
        self.assertIn('"$targetName.sha256"', installer)
        self.assertIn(
            "if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf))",
            installer,
        )
        self.assertIn("기존 config.yaml을 변경하지 않고 보존합니다.", installer)
        self.assertIn('$ManagerDir = Join-Path $InstallDir "manager"', installer)

    def test_default_path_uses_user_home_and_detects_conflicting_installs(
        self,
    ) -> None:
        installer = (PROJECT_ROOT / "install.ps1").read_text(encoding="utf-8")

        self.assertRegex(
            installer,
            r'\[string\]\$InstallDir = \(Join-Path '
            r'\(\[Environment\]::GetFolderPath\("UserProfile"\)\) '
            r'"CLIProxyAPI"\)',
        )
        self.assertIn("function Get-ExistingInstallationDirectories", installer)
        self.assertIn('$PSBoundParameters.ContainsKey("InstallDir")', installer)
        self.assertIn("기존 CLIProxyAPI 설치를 재사용합니다", installer)
        self.assertIn("CLIProxyAPI 설치가 여러 곳에서 발견됐습니다", installer)
        self.assertIn("지정한 경로와 다른 CLIProxyAPI 설치가 발견됐습니다", installer)

    def test_shell_shortcuts_use_model_aware_auto_compaction(self) -> None:
        installer = (PROJECT_ROOT / "install.ps1").read_text(encoding="utf-8")

        self.assertEqual(installer.count("--autocompact auto"), 6)
        self.assertNotIn("--autocompact 230k", installer)

    def test_default_config_exposes_standard_and_fast_sol_models(self) -> None:
        installer = (PROJECT_ROOT / "install.ps1").read_text(encoding="utf-8")
        match = re.search(
            r'\$configuration = @"\n(?P<yaml>.*?)\n"@',
            installer,
            re.DOTALL,
        )

        self.assertIsNotNone(match)
        config = yaml.safe_load(match.group("yaml"))
        self.assertEqual(
            config["oauth-model-alias"]["codex"],
            [
                {
                    "name": "gpt-5.6-sol",
                    "alias": "gpt-5.6-sol-fast",
                    "display-name": "GPT-5.6 Sol Fast",
                    "fork": True,
                    "force-mapping": True,
                }
            ],
        )
        self.assertEqual(
            config["payload"]["override"],
            [
                {
                    "models": [
                        {"name": "gpt-5.6-sol-fast", "protocol": "codex"}
                    ],
                    "params": {"service_tier": "priority"},
                }
            ],
        )

    def test_npx_wrapper_uses_argument_array_without_shell(self) -> None:
        wrapper = (PROJECT_ROOT / "bin" / "cli.js").read_text(encoding="utf-8")

        self.assertIn('const { spawn } = require("node:child_process");', wrapper)
        self.assertIn('"-File",', wrapper)
        self.assertIn("...parsed.powershellArguments", wrapper)
        self.assertIn("shell: false", wrapper)
        self.assertNotIn("exec(", wrapper)

    def test_release_workflow_builds_expected_assets(self) -> None:
        workflow = (
            PROJECT_ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")

        self.assertIn('tags:\n      - "v*"', workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn(".\\build.ps1 -SkipStartupRegistration", workflow)
        self.assertIn("CLIProxyAPI-Manager.exe.sha256", workflow)
        self.assertIn('"install.ps1"', workflow)
        self.assertIn("npm pack --json", workflow)

    def test_powershell_installer_parses(self) -> None:
        powershell = shutil.which("powershell.exe")
        if powershell is None:
            self.skipTest("powershell.exe is not available")

        command = (
            "$tokens = $null; $errors = $null; "
            "[System.Management.Automation.Language.Parser]::ParseFile("
            f"'{PROJECT_ROOT / 'install.ps1'}', [ref]$tokens, [ref]$errors) "
            "| Out-Null; "
            "if ($errors.Count -gt 0) { "
            "$errors | ForEach-Object { $_.Message }; exit 1 }"
        )
        result = subprocess.run(
            [powershell, "-NoProfile", "-Command", command],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_node_wrapper_syntax_and_help(self) -> None:
        node = shutil.which("node.exe") or shutil.which("node")
        if node is None:
            self.skipTest("Node.js is not available")

        syntax = subprocess.run(
            [node, "--check", str(PROJECT_ROOT / "bin" / "cli.js")],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(syntax.returncode, 0, syntax.stdout + syntax.stderr)

        help_result = subprocess.run(
            [node, str(PROJECT_ROOT / "bin" / "cli.js"), "--help"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(
            help_result.returncode,
            0,
            help_result.stdout + help_result.stderr,
        )
        self.assertIn("--skip-claude-code", help_result.stdout)
        self.assertIn("--skip-startup", help_result.stdout)


if __name__ == "__main__":
    unittest.main()
