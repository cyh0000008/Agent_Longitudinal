@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "ROOT=%%~fI"
set "PYTHON=C:\Users\10309\miniconda3\python.exe"
set "MAIN=%ROOT%\main.py"
set "LAUNCHER=%ROOT%\scripts\run_bot.py"
set "LOG_DIR=%ROOT%\logs"
set "PID_FILE=%LOG_DIR%\feishuagent.pid"
set "LOCK_FILE=%LOG_DIR%\feishuagent.lock"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

echo [FeishuAgent] stopping old bot process if present...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "$pidFile='%PID_FILE%'; $main='%MAIN%';" ^
  "if (Test-Path -LiteralPath $pidFile) { $pidText=(Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim(); if ($pidText -match '^\d+$') { Stop-Process -Id ([int]$pidText) -Force -ErrorAction SilentlyContinue } };" ^
  "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like ('*' + $main + '*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

if exist "%PID_FILE%" del /f /q "%PID_FILE%"
if exist "%LOCK_FILE%" del /f /q "%LOCK_FILE%"

echo [FeishuAgent] starting bot in a new console...
start "" /D "%ROOT%" "%PYTHON%" "%LAUNCHER%"

timeout /t 3 /nobreak >nul

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "$launcher='%LAUNCHER%'; $pidFile='%PID_FILE%'; $lockFile='%LOCK_FILE%';" ^
  "$bot=Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like ('*' + $launcher + '*') } | Select-Object -First 1;" ^
  "if ($bot) { [string]$bot.ProcessId | Set-Content -LiteralPath $pidFile -Encoding ascii; [string]$bot.ProcessId | Set-Content -LiteralPath $lockFile -Encoding ascii; Write-Host ('[FeishuAgent] started. Python PID: ' + $bot.ProcessId) } else { Write-Host '[FeishuAgent] failed to detect running python process.'; exit 1 }"

pause
