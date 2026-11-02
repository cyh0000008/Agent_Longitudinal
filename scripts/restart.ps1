$ErrorActionPreference = "Stop"

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8NoBom
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$Root = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$Python = "C:\Users\10309\miniconda3\python.exe"
$Main = Join-Path $Root "main.py"
$Stdout = Join-Path $LogDir "feishuagent.out.log"
$Stderr = Join-Path $LogDir "feishuagent.err.log"
$PidFile = Join-Path $LogDir "feishuagent.pid"
$LockFile = Join-Path $LogDir "feishuagent.lock"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python executable not found: $Python"
}

if (-not (Test-Path -LiteralPath $Main)) {
    throw "Bot entry file not found: $Main"
}

function Get-PythonProcessIds {
    @(Get-Process -Name python -ErrorAction SilentlyContinue | ForEach-Object { [int]$_.Id })
}

function Get-BotProcessIds {
    try {
        $items = Get-CimInstance Win32_Process -ErrorAction Stop |
            Where-Object {
                $_.Name -eq "python.exe" -and
                $_.CommandLine -like "*$Main*"
            } |
            Select-Object -ExpandProperty ProcessId
        return @($items | ForEach-Object { [int]$_ })
    } catch {
        return @()
    }
}

$existingIds = Get-PythonProcessIds
$botIds = Get-BotProcessIds
$stoppedIds = @()

if (Test-Path -LiteralPath $PidFile) {
    $oldPidText = (Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
    if ($oldPidText -match "^\d+$") {
        $oldPid = [int]$oldPidText
        $oldProcess = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
        if ($oldProcess) {
            Write-Host ("Stopping existing bot process from pid file: {0}" -f $oldPid) -ForegroundColor Yellow
            Stop-Process -Id $oldPid -Force
            $stoppedIds += $oldPid
            Start-Sleep -Seconds 2
        }
    }
}

foreach ($botId in $botIds) {
    if ($stoppedIds -contains $botId) {
        continue
    }
    $botProcess = Get-Process -Id $botId -ErrorAction SilentlyContinue
    if ($botProcess) {
        Write-Host ("Stopping extra bot process: {0}" -f $botId) -ForegroundColor Yellow
        Stop-Process -Id $botId -Force
        $stoppedIds += $botId
        Start-Sleep -Milliseconds 500
    }
}

if ($stoppedIds.Count -eq 0) {
    Write-Host "No tracked bot process found via pid file." -ForegroundColor DarkYellow
    Write-Host "If you started the bot manually before, please stop that old process once. After that, this script can manage restart by itself." -ForegroundColor DarkYellow
}

Write-Host "Starting Feishu bot..." -ForegroundColor Cyan
$cmdArgs = "/c start `"`" /D `"$Root`" `"$Python`" -u `"$Main`""
$hostProcess = Start-Process `
    -FilePath "cmd.exe" `
    -ArgumentList $cmdArgs `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -PassThru

$botIds = @()
for ($attempt = 1; $attempt -le 12; $attempt++) {
    Start-Sleep -Seconds 1
    $botIds = @(Get-BotProcessIds)
    if ($botIds.Count -gt 0) {
        break
    }
}

if ($botIds.Count -eq 0) {
    throw "Bot process failed to stay running. Try scripts\start_bot.bat to keep a visible foreground window and inspect the error."
}

$currentPid = [int]($botIds | Select-Object -First 1)
[int]$currentPid | Set-Content -LiteralPath $PidFile -Encoding ascii
[int]$currentPid | Set-Content -LiteralPath $LockFile -Encoding ascii

Write-Host "Feishu bot restarted successfully." -ForegroundColor Green
Write-Host ("Starter cmd PID: {0}" -f $hostProcess.Id)
Write-Host ("Python PID: {0}" -f $currentPid)
Write-Host ("Pid file: {0}" -f $PidFile)
Write-Host ("Lock file: {0}" -f $LockFile)
Write-Host ("Stdout log: {0}" -f $Stdout)
Write-Host ("Stderr log: {0}" -f $Stderr)
