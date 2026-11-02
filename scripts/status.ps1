$ErrorActionPreference = "Stop"

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8NoBom
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom

$Root = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $Root "logs"
$Main = Join-Path $Root "main.py"
$Launcher = Join-Path $Root "scripts\run_bot.py"
$PidFile = Join-Path $LogDir "feishuagent.pid"
$LockFile = Join-Path $LogDir "feishuagent.lock"

function Get-BotProcessRows {
    try {
        $items = Get-CimInstance Win32_Process -ErrorAction Stop |
            Where-Object {
                $_.Name -eq "python.exe" -and
                ($_.CommandLine -like "*$Main*" -or $_.CommandLine -like "*$Launcher*")
            } |
            Select-Object ProcessId, Name, CommandLine
        return @($items)
    } catch {
        return @()
    }
}

function Read-OptionalId($path) {
    if (-not (Test-Path -LiteralPath $path)) {
        return $null
    }
    $text = (Get-Content -LiteralPath $path -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
    if ($text -match "^\d+$") {
        return [int]$text
    }
    return $null
}

$botRows = @(Get-BotProcessRows)
$pidValue = Read-OptionalId $PidFile
$lockValue = Read-OptionalId $LockFile

$pidDisplay = if ($null -ne $pidValue) { $pidValue } else { "empty" }
$lockDisplay = if ($null -ne $lockValue) { $lockValue } else { "empty" }

Write-Host "Feishu bot status" -ForegroundColor Cyan
Write-Host ("Main: {0}" -f $Main)
Write-Host ("Pid file: {0}" -f $pidDisplay)
Write-Host ("Lock file: {0}" -f $lockDisplay)
Write-Host ("Running bot instances: {0}" -f $botRows.Count)

if ($botRows.Count -gt 0) {
    $botRows |
        Sort-Object ProcessId |
        Format-Table ProcessId, Name, CommandLine -AutoSize
}

if ($botRows.Count -eq 0) {
    Write-Host "No running bot process detected." -ForegroundColor Yellow
} elseif ($botRows.Count -eq 1) {
    Write-Host "Single bot instance is running." -ForegroundColor Green
} else {
    Write-Host "Warning: multiple bot instances are running." -ForegroundColor Red
}
