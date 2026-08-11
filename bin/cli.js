#!/usr/bin/env node

"use strict";

const path = require("node:path");
const { spawn } = require("node:child_process");

const usage = `CLIProxyAPI Manager 통합 설치기

사용법:
  npx -y cliproxyapi-manager [옵션]

옵션:
  --install-dir <경로>   설치 경로 지정
  --skip-claude-code     Claude Code 설치 건너뛰기
  --skip-startup         Windows 시작프로그램 등록 건너뛰기
  --no-launch            설치 후 Manager를 실행하지 않기
  -h, --help             도움말 표시
`;

function fail(message) {
  process.stderr.write(`${message}\n\n${usage}`);
  process.exit(2);
}

function parseArguments(args) {
  const powershellArguments = [];

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
        index += 1;
        break;
      }
      case "--skip-claude-code":
        powershellArguments.push("-SkipClaudeCode");
        break;
      case "--skip-startup":
        powershellArguments.push("-SkipStartupRegistration");
        break;
      case "--no-launch":
        powershellArguments.push("-NoLaunch");
        break;
      default:
        fail(`알 수 없는 옵션입니다: ${argument}`);
    }
  }

  return { help: false, powershellArguments };
}

if (process.platform !== "win32") {
  fail("이 설치기는 Windows 10/11에서만 지원됩니다.");
}

const parsed = parseArguments(process.argv.slice(2));
if (parsed.help) {
  process.stdout.write(usage);
  process.exit(0);
}

const installerPath = path.resolve(__dirname, "..", "install.ps1");
const child = spawn(
  "powershell.exe",
  [
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    installerPath,
    ...parsed.powershellArguments,
  ],
  {
    stdio: "inherit",
    windowsHide: false,
    shell: false,
  },
);

child.on("error", (error) => {
  process.stderr.write(`PowerShell 설치기를 시작하지 못했습니다: ${error.message}\n`);
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
