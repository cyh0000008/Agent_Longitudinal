$ErrorActionPreference = "Stop"

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8NoBom
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$Root = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $Root "logs"
$PidFile = Join-Path $LogDir "feishuagent.pid"
$LockFile = Join-Path $LogDir "feishuagent.lock"
$Python = "C:\Users\10309\miniconda3\python.exe"
$Main = Join-Path $Root "main.py"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python executable not found: $Python"
}

if (-not (Test-Path -LiteralPath $Main)) {
    throw "Bot entry file not found: $Main"
}

function Read-OptionalPid($path) {
    if (-not (Test-Path -LiteralPath $path)) {
        return $null
    }
    $text = (Get-Content -LiteralPath $path -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
    if ($text -match "^\d+$") {
        return [int]$text
    }
    return $null
}

function Test-PidAlive($pidValue) {
    if ($null -eq $pidValue) {
        return $false
    }
    return $null -ne (Get-Process -Id $pidValue -ErrorAction SilentlyContinue)
}

function Get-BotProcessRows {
    try {
        $items = Get-CimInstance Win32_Process -ErrorAction Stop |
            Where-Object {
                $_.Name -eq "python.exe" -and $_.CommandLine -like "*$Main*"
            } |
            Select-Object ProcessId, Name, CommandLine
        return @($items)
    } catch {
        return @()
    }
}

$existingPid = Read-OptionalPid $PidFile
$existingLockPid = Read-OptionalPid $LockFile
$runningRows = @(Get-BotProcessRows)
$runningPid = $null
if ($runningRows.Count -gt 0) {
    $runningPid = [int]($runningRows | Select-Object -First 1).ProcessId
}

if ((Test-PidAlive $existingPid) -or (Test-PidAlive $existingLockPid) -or $runningRows.Count -gt 0) {
    $displayPid = if ($runningPid) { $runningPid } elseif ($existingPid) { $existingPid } else { $existingLockPid }
    Write-Host ("Feishu bot is already running. PID: {0}" -f $displayPid) -ForegroundColor Green
    Write-Host "If you need a clean restart, close the existing bot window first or use scripts\restart.ps1." -ForegroundColor Yellow
    exit 0
}

if (Test-Path -LiteralPath $PidFile) {
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
}
if (Test-Path -LiteralPath $LockFile) {
    Remove-Item -LiteralPath $LockFile -Force -ErrorAction SilentlyContinue
}

Write-Host "Starting Feishu bot in foreground mode..." -ForegroundColor Cyan
Write-Host "Keep this window open. Closing it will stop the bot." -ForegroundColor Yellow
Write-Host ("Python: {0}" -f $Python)
Write-Host ("Main: {0}" -f $Main)
Write-Host ""

Set-Location $Root
& $Python -u $Main

$exitCode = $LASTEXITCODE
Write-Host ""
Write-Host ("Feishu bot exited. Exit code: {0}" -f $exitCode) -ForegroundColor Yellow
exit $exitCode
