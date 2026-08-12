#!/usr/bin/env node

"use strict";

const path = require("node:path");
const { spawn } = require("node:child_process");

const usage = `CLIProxyAPI Manager 통합 설치기 (Windows/Linux)

사용법:
  npx -y cliproxyapi-manager [옵션]

옵션:
  --install-dir <경로>   설치 경로 지정
  --skip-claude-code     Claude Code 설치 건너뛰기
  --skip-startup         자동 시작 등록 건너뛰기
  --no-launch            설치 후 Manager를 실행하지 않기
  -h, --help             도움말 표시
`;

function fail(message) {
  process.stderr.write(`${message}\n\n${usage}`);
  process.exit(2);
}

function parseArguments(args, platform = process.platform) {
  const powershellArguments = [];
  const bashArguments = [];

  for (let index = 0; index < args.length; index += 1) {
    const argument = args[index];
    switch (argument) {
      case "-h":
      case "--help":
        return { help: true, powershellArguments: [] };
      case "--install-dir": {
        const value = args[index + 1];
        if (!value || value.startsWith("--")) {
          fail("--install-dir 다음에 설치 경로가 필요합니다.");
        }
        powershellArguments.push("-InstallDir", value);
        bashArguments.push("--install-dir", value);
        index += 1;
        break;
      }
      case "--skip-claude-code":
        powershellArguments.push("-SkipClaudeCode");
        bashArguments.push("--skip-claude-code");
        break;
      case "--skip-startup":
        powershellArguments.push("-SkipStartupRegistration");
        bashArguments.push("--skip-startup");
        break;
      case "--no-launch":
        powershellArguments.push("-NoLaunch");
        bashArguments.push("--no-launch");
        break;
      default:
        fail(`알 수 없는 옵션입니다: ${argument}`);
    }
  }

  return { help: false, platform, powershellArguments, bashArguments };
}

if (!["win32", "linux"].includes(process.platform)) {
  fail(`지원하지 않는 운영체제입니다: ${process.platform}`);
}

const parsed = parseArguments(process.argv.slice(2));
if (parsed.help) {
  process.stdout.write(usage);
  process.exit(0);
}

const isWindows = process.platform === "win32";
const installerPath = path.resolve(
  __dirname,
  "..",
  isWindows ? "install.ps1" : "install.sh",
);
const executable = isWindows ? "powershell.exe" : "bash";
const installerArguments = isWindows
  ? [
      "-NoProfile",
      "-ExecutionPolicy",
      "Bypass",
      "-File",
      installerPath,
      ...parsed.powershellArguments,
    ]
  : [installerPath, ...parsed.bashArguments];
const child = spawn(
  executable,
  installerArguments,
  {
    stdio: "inherit",
    windowsHide: false,
    shell: false,
  },
);

child.on("error", (error) => {
  process.stderr.write(`설치기를 시작하지 못했습니다: ${error.message}\n`);
  process.exit(1);
});

child.on("exit", (code, signal) => {
  if (signal === "SIGINT") {
    process.exit(130);
  }
  if (signal === "SIGTERM") {
    process.exit(143);
  }
  process.exit(code === null ? 1 : code);
});
