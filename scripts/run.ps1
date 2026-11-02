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

Start-Process `
    -FilePath $Python `
    -ArgumentList @("-u", $Main) `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $Stdout `
    -RedirectStandardError $Stderr `
    -PassThru
