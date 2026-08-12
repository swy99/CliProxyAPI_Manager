[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "CLIProxyAPI"),
    [switch]$SkipClaudeCode,
    [switch]$SkipStartupRegistration,
    [switch]$NoLaunch
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$InstallerVersion = "1.2.1"
$MaximumAssetBytes = 256MB
$MaximumScriptBytes = 5MB
$BackendReleaseApi = "https://api.github.com/repos/router-for-me/CLIProxyAPI/releases/latest"
$ManagerReleaseApi = "https://api.github.com/repos/swy99/CliProxyAPI_Manager/releases/latest"
$ClaudeInstallerUrl = "https://claude.ai/install.ps1"
$ManagerAssetName = "CLIProxyAPI-Manager.exe"
$GitHubHeaders = @{
    Accept = "application/vnd.github+json"
    "X-GitHub-Api-Version" = "2022-11-28"
    "User-Agent" = "CLIProxyAPI-Manager-Installer/$InstallerVersion"
}

function Write-Step {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host "[CLIProxyAPI] $Message"
}

function Resolve-InstallDirectory {
    param([Parameter(Mandatory = $true)][string]$Path)

    $expanded = [Environment]::ExpandEnvironmentVariables($Path)
    if ([IO.Path]::IsPathRooted($expanded)) {
        return [IO.Path]::GetFullPath($expanded)
    }
    return [IO.Path]::GetFullPath((Join-Path (Get-Location).Path $expanded))
}

function Assert-HttpsUri {
    param([Parameter(Mandatory = $true)][string]$Uri)

    $parsed = [Uri]$Uri
    if ($parsed.Scheme -ne "https") {
        throw "HTTPS가 아닌 다운로드 주소를 거부했습니다: $Uri"
    }
}

function Invoke-GitHubReleaseRequest {
    param([Parameter(Mandatory = $true)][string]$Uri)

    Assert-HttpsUri $Uri
    return Invoke-RestMethod -Uri $Uri -Headers $GitHubHeaders -TimeoutSec 30
}

function Save-Download {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][long]$MaximumBytes,
        [hashtable]$Headers = @{}
    )

    Assert-HttpsUri $Uri
    $request = [Net.HttpWebRequest]::Create($Uri)
    $request.AllowAutoRedirect = $true
    $request.MaximumAutomaticRedirections = 5
    $request.Timeout = 120000
    $request.ReadWriteTimeout = 120000
    $request.UserAgent = "CLIProxyAPI-Manager-Installer/$InstallerVersion"
    foreach ($entry in $Headers.GetEnumerator()) {
        if ($entry.Key -eq "Accept") {
            $request.Accept = [string]$entry.Value
        }
        elseif ($entry.Key -eq "User-Agent") {
            $request.UserAgent = [string]$entry.Value
        }
        else {
            $request.Headers[[string]$entry.Key] = [string]$entry.Value
        }
    }

    $response = $null
    $input = $null
    $output = $null
    try {
        $response = $request.GetResponse()
        Assert-HttpsUri ([string]$response.ResponseUri.AbsoluteUri)
        if ($response.ContentLength -gt $MaximumBytes) {
            throw "다운로드 파일이 허용 크기를 초과했습니다: $($response.ContentLength) bytes"
        }

        $input = $response.GetResponseStream()
        $output = [IO.File]::Open(
            $Destination,
            [IO.FileMode]::Create,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None
        )
        $buffer = New-Object byte[] (1024 * 1024)
        [long]$total = 0
        while (($read = $input.Read($buffer, 0, $buffer.Length)) -gt 0) {
            $total += $read
            if ($total -gt $MaximumBytes) {
                throw "다운로드 파일이 허용 크기($MaximumBytes bytes)를 초과했습니다."
            }
            $output.Write($buffer, 0, $read)
        }

        return [pscustomobject]@{
            Size = $total
            ContentType = [string]$response.ContentType
        }
    }
    catch {
        if (Test-Path -LiteralPath $Destination) {
            Remove-Item -LiteralPath $Destination -Force -Confirm:$false
        }
        throw
    }
    finally {
        if ($null -ne $output) {
            $output.Dispose()
        }
        if ($null -ne $input) {
            $input.Dispose()
        }
        if ($null -ne $response) {
            $response.Dispose()
        }
    }
}

function Select-ReleaseAsset {
    param(
        [Parameter(Mandatory = $true)]$Release,
        [string]$ExactName,
        [string]$NameSuffix
    )

    $assets = @($Release.assets)
    if ($ExactName) {
        $matches = @($assets | Where-Object { [string]$_.name -ieq $ExactName })
    }
    else {
        $matches = @($assets | Where-Object {
            ([string]$_.name).EndsWith($NameSuffix, [StringComparison]::OrdinalIgnoreCase)
        })
    }

    if ($matches.Count -ne 1) {
        $description = if ($ExactName) { $ExactName } else { "*$NameSuffix" }
        throw "릴리스 $($Release.tag_name)에서 '$description' 자산을 하나만 찾을 수 없습니다."
    }
    return $matches[0]
}

function Get-ExpectedSha256 {
    param(
        [Parameter(Mandatory = $true)]$Release,
        [Parameter(Mandatory = $true)]$Asset,
        [Parameter(Mandatory = $true)][string]$TemporaryDirectory
    )

    $digestProperty = $Asset.PSObject.Properties["digest"]
    if ($null -ne $digestProperty) {
        $digest = [string]$digestProperty.Value
        if ($digest -match "^sha256:([a-fA-F0-9]{64})$") {
            return $Matches[1].ToLowerInvariant()
        }
    }

    $targetName = [string]$Asset.name
    $checksumAssets = @($Release.assets | Where-Object {
        $name = [string]$_.name
        $name -ieq "$targetName.sha256" -or
        $name -ieq "checksums.txt" -or
        $name -ieq "sha256sums.txt"
    })

    foreach ($checksumAsset in $checksumAssets) {
        $checksumPath = Join-Path $TemporaryDirectory ("checksum-" + [Guid]::NewGuid().ToString("N") + ".txt")
        Save-Download -Uri ([string]$checksumAsset.browser_download_url) `
            -Destination $checksumPath -MaximumBytes 1MB -Headers $GitHubHeaders | Out-Null
        $content = [IO.File]::ReadAllText($checksumPath)
        Remove-Item -LiteralPath $checksumPath -Force -Confirm:$false

        $checksumName = [string]$checksumAsset.name
        if ($checksumName -ieq "$targetName.sha256") {
            $match = [regex]::Match($content, "(?i)\b([a-f0-9]{64})\b")
            if ($match.Success) {
                return $match.Groups[1].Value.ToLowerInvariant()
            }
        }
        else {
            foreach ($line in ($content -split "`r?`n")) {
                if ($line.IndexOf($targetName, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
                    continue
                }
                $match = [regex]::Match($line, "(?i)\b([a-f0-9]{64})\b")
                if ($match.Success) {
                    return $match.Groups[1].Value.ToLowerInvariant()
                }
            }
        }
    }

    throw "릴리스 $($Release.tag_name)의 $targetName SHA-256 체크섬을 찾을 수 없습니다."
}

function Save-VerifiedReleaseAsset {
    param(
        [Parameter(Mandatory = $true)]$Release,
        [Parameter(Mandatory = $true)]$Asset,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$TemporaryDirectory
    )

    $assetSize = [long]$Asset.size
    if ($assetSize -gt $MaximumAssetBytes) {
        throw "$($Asset.name) 자산이 허용 크기(256MB)를 초과했습니다."
    }

    $expected = Get-ExpectedSha256 -Release $Release -Asset $Asset -TemporaryDirectory $TemporaryDirectory
    Save-Download -Uri ([string]$Asset.browser_download_url) -Destination $Destination `
        -MaximumBytes $MaximumAssetBytes -Headers $GitHubHeaders | Out-Null
    $actual = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $expected) {
        Remove-Item -LiteralPath $Destination -Force -Confirm:$false
        throw "SHA-256 검증 실패: 예상 $expected, 실제 $actual"
    }
    return $actual
}

function Get-WindowsReleaseArchitecture {
    $architecture = $env:PROCESSOR_ARCHITEW6432
    if ([string]::IsNullOrWhiteSpace($architecture)) {
        $architecture = $env:PROCESSOR_ARCHITECTURE
    }

    switch ($architecture.ToUpperInvariant()) {
        "AMD64" { return "amd64" }
        "ARM64" { return "aarch64" }
        default { throw "지원하지 않는 Windows 아키텍처입니다: $architecture" }
    }
}

function Expand-BackendBinary {
    param(
        [Parameter(Mandatory = $true)][string]$ArchivePath,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($ArchivePath)
    try {
        $matches = @($archive.Entries | Where-Object {
            -not [string]::IsNullOrEmpty($_.Name) -and $_.Name -ieq "cli-proxy-api.exe"
        })
        if ($matches.Count -ne 1) {
            throw "CLIProxyAPI ZIP에서 cli-proxy-api.exe를 하나만 찾을 수 없습니다."
        }
        if ($matches[0].Length -gt $MaximumAssetBytes) {
            throw "압축된 cli-proxy-api.exe가 허용 크기(256MB)를 초과했습니다."
        }

        $input = $null
        $output = $null
        try {
            $input = $matches[0].Open()
            $output = [IO.File]::Open(
                $Destination,
                [IO.FileMode]::Create,
                [IO.FileAccess]::Write,
                [IO.FileShare]::None
            )
            $buffer = New-Object byte[] (1024 * 1024)
            [long]$total = 0
            while (($read = $input.Read($buffer, 0, $buffer.Length)) -gt 0) {
                $total += $read
                if ($total -gt $MaximumAssetBytes) {
                    throw "압축 해제 파일이 허용 크기(256MB)를 초과했습니다."
                }
                $output.Write($buffer, 0, $read)
            }
        }
        finally {
            if ($null -ne $output) {
                $output.Dispose()
            }
            if ($null -ne $input) {
                $input.Dispose()
            }
        }
    }
    finally {
        $archive.Dispose()
    }
}

function Get-BackendVersion {
    param([Parameter(Mandatory = $true)][string]$ExecutablePath)

    if (-not (Test-Path -LiteralPath $ExecutablePath -PathType Leaf)) {
        return $null
    }
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = (& $ExecutablePath -help 2>&1 | Out-String)
    }
    catch {
        return $null
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    $match = [regex]::Match($output, "(?im)CLIProxyAPI Version:\s*v?([^,\s]+)")
    if (-not $match.Success) {
        return $null
    }
    return $match.Groups[1].Value
}

function Compare-NumericVersion {
    param(
        [Parameter(Mandatory = $true)][string]$Left,
        [Parameter(Mandatory = $true)][string]$Right
    )

    $leftParts = @($Left.TrimStart("v", "V").Split(".") | ForEach-Object { [int]$_ })
    $rightParts = @($Right.TrimStart("v", "V").Split(".") | ForEach-Object { [int]$_ })
    $length = [Math]::Max($leftParts.Count, $rightParts.Count)
    for ($index = 0; $index -lt $length; $index++) {
        $leftValue = if ($index -lt $leftParts.Count) { $leftParts[$index] } else { 0 }
        $rightValue = if ($index -lt $rightParts.Count) { $rightParts[$index] } else { 0 }
        if ($leftValue -lt $rightValue) { return -1 }
        if ($leftValue -gt $rightValue) { return 1 }
    }
    return 0
}

function Test-PortableExecutable {
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = [IO.File]::OpenRead($Path)
    try {
        if ($stream.Length -lt 2) {
            return $false
        }
        return $stream.ReadByte() -eq 0x4D -and $stream.ReadByte() -eq 0x5A
    }
    finally {
        $stream.Dispose()
    }
}

function Get-FileSha256OrNull {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Stop-ProcessesByExecutablePath {
    param([Parameter(Mandatory = $true)][string]$ExecutablePath)

    if (-not (Test-Path -LiteralPath $ExecutablePath -PathType Leaf)) {
        return $false
    }
    $target = [IO.Path]::GetFullPath($ExecutablePath)
    $processes = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        -not [string]::IsNullOrWhiteSpace([string]$_.ExecutablePath) -and
        [string]::Equals(
            [IO.Path]::GetFullPath([string]$_.ExecutablePath),
            $target,
            [StringComparison]::OrdinalIgnoreCase
        )
    })

    foreach ($process in $processes) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
        Wait-Process -Id $process.ProcessId -Timeout 10 -ErrorAction SilentlyContinue
    }
    return $processes.Count -gt 0
}

function Install-StagedFile {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    $directory = Split-Path -Parent $Destination
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    $existing = Test-Path -LiteralPath $Destination -PathType Leaf
    $sourceHash = Get-FileSha256OrNull $Source
    if ($existing -and (Get-FileSha256OrNull $Destination) -eq $sourceHash) {
        return [pscustomobject]@{
            Changed = $false
            Destination = $Destination
            Backup = $null
            Existed = $true
        }
    }

    $backup = $null
    $staged = Join-Path $directory ("." + [IO.Path]::GetFileName($Destination) + ".install-" + [Guid]::NewGuid().ToString("N"))
    try {
        Copy-Item -LiteralPath $Source -Destination $staged
        if ($existing) {
            $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
            $backup = "$Destination.backup-$stamp-$([Guid]::NewGuid().ToString('N').Substring(0, 8))"
            Move-Item -LiteralPath $Destination -Destination $backup
        }
        Move-Item -LiteralPath $staged -Destination $Destination
    }
    catch {
        if (Test-Path -LiteralPath $staged) {
            Remove-Item -LiteralPath $staged -Force -Confirm:$false
        }
        if ($null -ne $backup -and (Test-Path -LiteralPath $backup) -and -not (Test-Path -LiteralPath $Destination)) {
            Move-Item -LiteralPath $backup -Destination $Destination
        }
        throw
    }

    return [pscustomobject]@{
        Changed = $true
        Destination = $Destination
        Backup = $backup
        Existed = $existing
    }
}

function Undo-InstalledFile {
    param([Parameter(Mandatory = $true)]$Change)

    if (-not $Change.Changed) {
        return
    }
    if (Test-Path -LiteralPath $Change.Destination) {
        Remove-Item -LiteralPath $Change.Destination -Force -Confirm:$false
    }
    if ($null -ne $Change.Backup -and (Test-Path -LiteralPath $Change.Backup)) {
        Move-Item -LiteralPath $Change.Backup -Destination $Change.Destination
    }
}

function New-DefaultConfiguration {
    param([Parameter(Mandatory = $true)][string]$Path)

    $bytes = New-Object byte[] 32
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $random.GetBytes($bytes)
    }
    finally {
        $random.Dispose()
    }
    $apiKey = ($bytes | ForEach-Object { $_.ToString("x2") }) -join ""
    $configuration = @"
host: "127.0.0.1"
port: 8317

tls:
  enable: false

remote-management:
  allow-remote: false
  secret-key: ""

auth-dir: "~/.cli-proxy-api"

api-keys:
  - "$apiKey"

debug: false
pprof:
  enable: false
ws-auth: true
"@
    $utf8 = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($Path, $configuration, $utf8)
}

function Get-ClaudeCodeVersion {
    $command = Get-Command claude -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        return $null
    }
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = (& $command.Source --version 2>&1 | Out-String).Trim()
        $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0 -or [string]::IsNullOrWhiteSpace($output)) {
            return $null
        }
        return $output
    }
    catch {
        return $null
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
}

function Refresh-ProcessPath {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @($machinePath, $userPath, $env:Path) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    $env:Path = $parts -join ";"
}

function Install-ClaudeCode {
    param([Parameter(Mandatory = $true)][string]$InstallerPath)

    Write-Step "Claude Code 공식 설치기를 실행합니다."
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $InstallerPath
    if ($LASTEXITCODE -ne 0) {
        throw "Claude Code 공식 설치기가 종료 코드 ${LASTEXITCODE}로 실패했습니다."
    }
    Refresh-ProcessPath
    $version = Get-ClaudeCodeVersion
    if ($null -eq $version) {
        throw "Claude Code 설치 후 'claude --version' 검증에 실패했습니다. 새 터미널에서 다시 확인하세요."
    }
    Write-Step "Claude Code 설치 확인: $version"
}

function Add-ShellShortcutsToProfile {
    param([Parameter(Mandatory = $true)][string]$ProfilePath)

    $begin = "# >>> cliproxyapi-manager shortcuts >>>"
    $end = "# <<< cliproxyapi-manager shortcuts <<<"
    $block = @"
$begin
# Claude Code 실행 단축키 (cliproxyapi-manager 설치기가 추가).
# cs 계열: 권한 프롬프트 건너뜀. csg 계열: GPT/Codex 백엔드(약 258K)용 --autocompact 230k.
function cs   { & claude --dangerously-skip-permissions @args }
function csr  { & claude --dangerously-skip-permissions --resume @args }
function csw  { & claude --dangerously-skip-permissions -w @args }
function csg  { & claude --dangerously-skip-permissions --autocompact 230k @args }
function csgr { & claude --dangerously-skip-permissions --autocompact 230k --resume @args }
function csgw { & claude --dangerously-skip-permissions --autocompact 230k -w @args }
$end
"@

    $directory = Split-Path -Parent $ProfilePath
    if (-not [string]::IsNullOrEmpty($directory)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }

    $existing = ""
    if (Test-Path -LiteralPath $ProfilePath -PathType Leaf) {
        $existing = [IO.File]::ReadAllText($ProfilePath)
    }

    $pattern = [regex]::Escape($begin) + "(?s).*?" + [regex]::Escape($end)
    if ([regex]::IsMatch($existing, $pattern)) {
        $evaluator = [System.Text.RegularExpressions.MatchEvaluator] { param($match) $block }
        $updated = [regex]::Replace($existing, $pattern, $evaluator)
    }
    else {
        if ($existing.Length -gt 0 -and -not $existing.EndsWith("`n")) {
            $existing += "`r`n"
        }
        $updated = $existing + $block + "`r`n"
    }

    $utf8 = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($ProfilePath, $updated, $utf8)
}

function Add-CmdShortcuts {
    param([Parameter(Mandatory = $true)][string]$AliasFilePath)

    $content = @"
@echo off
:: cliproxyapi-manager Claude Code shortcuts (doskey). Managed by installer.
doskey cs=claude --dangerously-skip-permissions `$*
doskey csr=claude --dangerously-skip-permissions --resume `$*
doskey csw=claude --dangerously-skip-permissions -w `$*
doskey csg=claude --dangerously-skip-permissions --autocompact 230k `$*
doskey csgr=claude --dangerously-skip-permissions --autocompact 230k --resume `$*
doskey csgw=claude --dangerously-skip-permissions --autocompact 230k -w `$*
"@

    $directory = Split-Path -Parent $AliasFilePath
    if (-not [string]::IsNullOrEmpty($directory)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }
    $utf8 = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($AliasFilePath, $content, $utf8)
}

function Register-CmdAutoRun {
    param([Parameter(Mandatory = $true)][string]$AliasFilePath)

    $key = "HKCU:\Software\Microsoft\Command Processor"
    if (-not (Test-Path -LiteralPath $key)) {
        New-Item -Path $key -Force | Out-Null
    }

    $entry = 'if exist "' + $AliasFilePath + '" call "' + $AliasFilePath + '"'
    $current = $null
    try {
        $current = [string](Get-ItemProperty -Path $key -Name "AutoRun" -ErrorAction Stop).AutoRun
    }
    catch {
        $current = $null
    }

    if ([string]::IsNullOrWhiteSpace($current)) {
        Set-ItemProperty -Path $key -Name "AutoRun" -Value $entry
    }
    elseif ($current.IndexOf($AliasFilePath, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
        return
    }
    else {
        Set-ItemProperty -Path $key -Name "AutoRun" -Value ($current + " & " + $entry)
    }
}

function Enable-ClaudeAutoCompact {
    param([Parameter(Mandatory = $true)][string]$SettingsPath)

    $directory = Split-Path -Parent $SettingsPath
    if (-not [string]::IsNullOrEmpty($directory)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }

    $data = $null
    if (Test-Path -LiteralPath $SettingsPath -PathType Leaf) {
        $raw = [IO.File]::ReadAllText($SettingsPath)
        if (-not [string]::IsNullOrWhiteSpace($raw)) {
            $data = $raw | ConvertFrom-Json
        }
    }
    if ($null -eq $data) {
        $data = [pscustomobject]@{}
    }

    $hasProperty = ($data.PSObject.Properties.Name -contains "autoCompactEnabled")
    if ($hasProperty -and $data.autoCompactEnabled -eq $true) {
        return $false
    }

    if ($hasProperty) {
        $data.autoCompactEnabled = $true
    }
    else {
        $data | Add-Member -NotePropertyName "autoCompactEnabled" -NotePropertyValue $true
    }

    $json = $data | ConvertTo-Json -Depth 25
    $utf8 = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($SettingsPath, $json, $utf8)
    return $true
}

function Invoke-ShellShortcutSetup {
    param([Parameter(Mandatory = $true)][string]$InstallDir)

    if (-not [Environment]::UserInteractive) {
        Write-Step "비대화형 실행이라 셸 단축키 설정을 건너뜁니다."
        return "skipped"
    }

    $answer = ""
    try {
        $answer = Read-Host "[CLIProxyAPI] 셸 단축키(cs/csg 등)와 autoCompact 설정을 추가할까요? (y/N)"
    }
    catch {
        return "skipped"
    }
    if ($answer -notmatch '^\s*(y|yes)\s*$') {
        Write-Step "셸 단축키 설정을 건너뜁니다."
        return "skipped"
    }

    $applied = @()

    try {
        $profilePath = $PROFILE.CurrentUserAllHosts
        Add-ShellShortcutsToProfile -ProfilePath $profilePath
        $applied += "PowerShell"
    }
    catch {
        Write-Warning "PowerShell 프로필 단축키 설정 실패: $($_.Exception.Message)"
    }

    try {
        $aliasFile = Join-Path $InstallDir "cmd-aliases.cmd"
        Add-CmdShortcuts -AliasFilePath $aliasFile
        Register-CmdAutoRun -AliasFilePath $aliasFile
        $applied += "cmd"
    }
    catch {
        Write-Warning "cmd 단축키 설정 실패: $($_.Exception.Message)"
    }

    try {
        $settingsPath = Join-Path $env:USERPROFILE ".claude\settings.json"
        Enable-ClaudeAutoCompact -SettingsPath $settingsPath | Out-Null
        $applied += "settings.json"
    }
    catch {
        Write-Warning "settings.json autoCompact 설정 실패: $($_.Exception.Message)"
    }

    if ($applied.Count -gt 0) {
        Write-Step ("셸 단축키/설정 적용: " + ($applied -join ", "))
        return "added"
    }
    return "failed"
}

if ($env:OS -ne "Windows_NT") {
    throw "이 설치기는 Windows 10/11에서만 지원됩니다."
}
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$InstallDir = Resolve-InstallDirectory $InstallDir
$ManagerDir = Join-Path $InstallDir "manager"
$BackendPath = Join-Path $InstallDir "cli-proxy-api.exe"
$ConfigPath = Join-Path $InstallDir "config.yaml"
$ManagerPath = Join-Path $ManagerDir $ManagerAssetName
$TemporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) ("cliproxyapi-install-" + [Guid]::NewGuid().ToString("N"))

New-Item -ItemType Directory -Path $TemporaryDirectory -Force | Out-Null
$backendChange = $null
$managerChange = $null
$configCreated = $false
$managerWasRunning = $false
$backendWasRunning = $false
$stackCommitted = $false

try {
    $architecture = Get-WindowsReleaseArchitecture
    Write-Step "CLIProxyAPI 최신 릴리스를 확인합니다 ($architecture)."
    $backendRelease = Invoke-GitHubReleaseRequest $BackendReleaseApi
    $backendAsset = Select-ReleaseAsset -Release $backendRelease -NameSuffix "_windows_$architecture.zip"
    $backendArchive = Join-Path $TemporaryDirectory ([string]$backendAsset.name)
    Save-VerifiedReleaseAsset -Release $backendRelease -Asset $backendAsset `
        -Destination $backendArchive -TemporaryDirectory $TemporaryDirectory | Out-Null
    $backendCandidate = Join-Path $TemporaryDirectory "cli-proxy-api.exe"
    Expand-BackendBinary -ArchivePath $backendArchive -Destination $backendCandidate
    $candidateVersion = Get-BackendVersion $backendCandidate
    $latestVersion = ([string]$backendRelease.tag_name).TrimStart("v", "V")
    if ($null -eq $candidateVersion -or (Compare-NumericVersion $candidateVersion $latestVersion) -ne 0) {
        throw "다운로드한 CLIProxyAPI 버전이 릴리스 태그와 일치하지 않습니다 ($candidateVersion != $latestVersion)."
    }

    Write-Step "CLIProxyAPI Manager 최신 릴리스를 확인합니다."
    $managerRelease = Invoke-GitHubReleaseRequest $ManagerReleaseApi
    $managerAsset = Select-ReleaseAsset -Release $managerRelease -ExactName $ManagerAssetName
    $managerCandidate = Join-Path $TemporaryDirectory $ManagerAssetName
    Save-VerifiedReleaseAsset -Release $managerRelease -Asset $managerAsset `
        -Destination $managerCandidate -TemporaryDirectory $TemporaryDirectory | Out-Null
    if (-not (Test-PortableExecutable $managerCandidate)) {
        throw "다운로드한 CLIProxyAPI Manager가 올바른 Windows 실행 파일이 아닙니다."
    }

    $claudeVersion = $null
    $claudeInstallerPath = $null
    if (-not $SkipClaudeCode) {
        $claudeVersion = Get-ClaudeCodeVersion
        if ($null -eq $claudeVersion) {
            Write-Step "Claude Code 공식 설치기를 준비합니다."
            $claudeInstallerPath = Join-Path $TemporaryDirectory "claude-install.ps1"
            $download = Save-Download -Uri $ClaudeInstallerUrl -Destination $claudeInstallerPath `
                -MaximumBytes $MaximumScriptBytes
            $scriptText = [IO.File]::ReadAllText($claudeInstallerPath)
            if ($download.Size -lt 100 -or $scriptText -match "(?i)<\s*html" -or $scriptText -notmatch "(?i)claude") {
                throw "Claude Code 설치기 응답이 올바른 PowerShell 스크립트가 아닙니다."
            }
        }
        else {
            Write-Step "기존 Claude Code 설치를 사용합니다: $claudeVersion"
        }
    }

    $existingBackendVersion = Get-BackendVersion $BackendPath
    $replaceBackend = $true
    if ($null -ne $existingBackendVersion -and (Compare-NumericVersion $existingBackendVersion $latestVersion) -ge 0) {
        $replaceBackend = $false
        Write-Step "기존 CLIProxyAPI v${existingBackendVersion}을 보존합니다."
    }
    $replaceManager = (Get-FileSha256OrNull $ManagerPath) -ne (Get-FileSha256OrNull $managerCandidate)

    if ($replaceManager -or $replaceBackend) {
        $managerWasRunning = Stop-ProcessesByExecutablePath $ManagerPath
    }
    if ($replaceBackend) {
        $backendWasRunning = Stop-ProcessesByExecutablePath $BackendPath
    }

    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    New-Item -ItemType Directory -Path $ManagerDir -Force | Out-Null
    try {
        if ($replaceBackend) {
            Write-Step "CLIProxyAPI v${latestVersion}을 설치합니다."
            $backendChange = Install-StagedFile -Source $backendCandidate -Destination $BackendPath
        }
        if ($replaceManager) {
            Write-Step "CLIProxyAPI Manager $($managerRelease.tag_name)을 설치합니다."
            $managerChange = Install-StagedFile -Source $managerCandidate -Destination $ManagerPath
        }
        if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
            Write-Step "localhost 전용 기본 config.yaml을 생성합니다."
            New-DefaultConfiguration $ConfigPath
            $configCreated = $true
        }
        else {
            Write-Step "기존 config.yaml을 변경하지 않고 보존합니다."
        }

        $installedVersion = Get-BackendVersion $BackendPath
        if ($null -eq $installedVersion) {
            throw "설치된 cli-proxy-api.exe 버전을 확인할 수 없습니다."
        }
        if (-not (Test-PortableExecutable $ManagerPath)) {
            throw "설치된 CLIProxyAPI Manager 실행 파일 검증에 실패했습니다."
        }
        $stackCommitted = $true
    }
    catch {
        if ($configCreated -and (Test-Path -LiteralPath $ConfigPath)) {
            Remove-Item -LiteralPath $ConfigPath -Force -Confirm:$false
            $configCreated = $false
        }
        if ($null -ne $managerChange) {
            Undo-InstalledFile $managerChange
        }
        if ($null -ne $backendChange) {
            Undo-InstalledFile $backendChange
        }
        throw
    }

    if (-not $SkipClaudeCode -and $null -ne $claudeInstallerPath) {
        Install-ClaudeCode $claudeInstallerPath
    }

    if (-not $SkipStartupRegistration) {
        Write-Step "Manager를 Windows 시작프로그램에 등록합니다."
        $registration = Start-Process -FilePath $ManagerPath -ArgumentList "--install-startup" `
            -WindowStyle Hidden -Wait -PassThru
        if ($registration.ExitCode -ne 0) {
            throw "시작프로그램 등록에 실패했습니다 (종료 코드: $($registration.ExitCode))."
        }
    }

    if (-not $NoLaunch) {
        Write-Step "CLIProxyAPI Manager를 실행합니다."
        Start-Process -FilePath $ManagerPath -WorkingDirectory $ManagerDir | Out-Null
    }
    elseif ($managerWasRunning) {
        Start-Process -FilePath $ManagerPath -ArgumentList "--minimized" -WorkingDirectory $ManagerDir | Out-Null
    }
    elseif ($backendWasRunning) {
        Start-Process -FilePath $BackendPath -ArgumentList @("-config", $ConfigPath) -WorkingDirectory $InstallDir | Out-Null
    }

    $shellSetupResult = "skipped"
    try {
        $shellSetupResult = Invoke-ShellShortcutSetup -InstallDir $InstallDir
    }
    catch {
        Write-Warning "셸 단축키 설정 중 오류가 발생해 건너뜁니다: $($_.Exception.Message)"
    }

    Write-Host ""
    Write-Host "설치 완료"
    Write-Host "  설치 경로: $InstallDir"
    Write-Host "  CLIProxyAPI: v$(Get-BackendVersion $BackendPath)"
    if (-not $SkipClaudeCode) {
        Write-Host "  Claude Code: $(Get-ClaudeCodeVersion)"
    }
    Write-Host "  Manager: $ManagerPath"
    if ($shellSetupResult -eq "added") {
        Write-Host "  셸 단축키: cs/csr/csw/csg/csgr/csgw (새 터미널에서 적용)"
    }
    Write-Host ""
    Write-Host "Manager에서 사용할 공급자의 로그인/OAuth를 진행하세요."
    Write-Host "Claude Code 진단이 필요하면 새 터미널에서 'claude doctor'를 실행하세요."
}
catch {
    if (-not $stackCommitted) {
        if ($managerWasRunning -and (Test-Path -LiteralPath $ManagerPath)) {
            Start-Process -FilePath $ManagerPath -ArgumentList "--minimized" -WorkingDirectory $ManagerDir | Out-Null
        }
        elseif ($backendWasRunning -and (Test-Path -LiteralPath $BackendPath)) {
            Start-Process -FilePath $BackendPath -ArgumentList @("-config", $ConfigPath) -WorkingDirectory $InstallDir | Out-Null
        }
    }
    throw
}
finally {
    if (Test-Path -LiteralPath $TemporaryDirectory) {
        Remove-Item -LiteralPath $TemporaryDirectory -Recurse -Force -Confirm:$false
    }
}
