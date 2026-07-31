[CmdletBinding()]
param(
    [switch]$SkipStartupRegistration
)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
$VirtualEnvironment = Join-Path $ProjectRoot ".manager-venv"
$Python = Join-Path $VirtualEnvironment "Scripts\python.exe"
$Output = Join-Path $ProjectRoot "CLIProxyAPI-Manager.exe"

# Keep packaging dependencies separate from the user's Python environment.
if (-not (Test-Path -LiteralPath $Python)) {
    python -m venv $VirtualEnvironment
}

& $Python -m pip install --disable-pip-version-check --upgrade pip
& $Python -m pip install --disable-pip-version-check -r (Join-Path $ProjectRoot "requirements.txt")

if (Test-Path -LiteralPath $Output) {
    $RunningManager = Get-CimInstance Win32_Process -Filter "Name = 'CLIProxyAPI-Manager.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -eq $Output }
    if ($RunningManager) {
        throw "CLIProxyAPI-Manager.exe가 실행 중입니다. 트레이 메뉴에서 관리자를 종료한 뒤 다시 빌드하세요."
    }
}

# A windowed one-file build starts without a console and is easy to place next
# to an existing CLIProxyAPI installation.
& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name "CLIProxyAPI-Manager" `
    --distpath $ProjectRoot `
    --workpath (Join-Path $ProjectRoot "build\manager") `
    --specpath (Join-Path $ProjectRoot "build") `
    (Join-Path $ProjectRoot "cliproxy_manager.py")

if (-not $SkipStartupRegistration) {
    # Register through the packaged program so source and release builds use
    # the same registry command format.
    $Registration = Start-Process `
        -FilePath $Output `
        -ArgumentList "--install-startup" `
        -WindowStyle Hidden `
        -Wait `
        -PassThru
    if ($Registration.ExitCode -ne 0) {
        throw "시작프로그램 등록에 실패했습니다 (종료 코드: $($Registration.ExitCode))."
    }
}

Write-Host ""
Write-Host "빌드 완료: $Output"
if (-not $SkipStartupRegistration) {
    Write-Host "Windows 시작프로그램 등록 완료"
}
