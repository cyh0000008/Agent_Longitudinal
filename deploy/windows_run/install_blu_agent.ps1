param(
    [switch]$Start,
    [switch]$SkipDependencyInstall
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Venv = Join-Path $RepoRoot ".venv"
$Python = Join-Path $Venv "Scripts\python.exe"
$Requirements = Join-Path $RepoRoot "requirements.txt"
$Main = Join-Path $RepoRoot "main.py"
$LogDir = Join-Path $RepoRoot "logs"
$EnvFile = Join-Path $RepoRoot ".env"
$EnvExample = Join-Path $RepoRoot "deploy\windows_run\env.example"

if ($env:OS -ne "Windows_NT") { throw "This installer must run on Windows." }
if (-not (Test-Path -LiteralPath $Requirements)) { throw "Missing requirements.txt: $Requirements" }
if (-not (Test-Path -LiteralPath $Main)) { throw "Missing main.py: $Main" }

function Add-MissingKpiDefaults {
    if (-not (Test-Path -LiteralPath $EnvFile) -or -not (Test-Path -LiteralPath $EnvExample)) { return }
    $existingKeys = @{}
    Get-Content -LiteralPath $EnvFile | ForEach-Object {
        if ($_ -match '^([^#=]+)=') { $existingKeys[$matches[1].Trim()] = $true }
    }
    $wanted = @(
        'KPI_REPORT_BASE_TOKEN', 'KPI_REPORT_TABLE_ID', 'KPI_REPORT_FIELD_REPORT',
        'KPI_REPORT_FIELD_TRIGGER', 'KPI_REPORT_FIELD_RESULT',
        'KPI_REPORT_TRIGGER_VALUE', 'KPI_REPORT_RESET_VALUE'
    )
    $additions = @()
    Get-Content -LiteralPath $EnvExample | ForEach-Object {
        if ($_ -match '^([^#=]+)=(.*)$') {
            $key = $matches[1].Trim()
            if ($wanted -contains $key -and -not $existingKeys.ContainsKey($key)) {
                $additions += $_
            }
        }
    }
    if ($additions.Count -gt 0) {
        Add-Content -LiteralPath $EnvFile -Value "`r`n# KPI report analysis defaults"
        Add-Content -LiteralPath $EnvFile -Value $additions
        Write-Host "Added missing KPI Base defaults to .env" -ForegroundColor Cyan
    }
}

if (-not (Test-Path -LiteralPath $Python)) {
    $Launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($Launcher) {
        & $Launcher.Source -3.11 -m venv $Venv
        if ($LASTEXITCODE -ne 0) { & $Launcher.Source -3.10 -m venv $Venv }
    } else {
        $SystemPython = Get-Command python.exe -ErrorAction Stop
        & $SystemPython.Source -m venv $Venv
    }
}
if (-not (Test-Path -LiteralPath $Python)) { throw "Failed to create Python virtual environment: $Venv" }

Add-MissingKpiDefaults

if (-not $SkipDependencyInstall) {
    & $Python -m pip install --upgrade pip
    & $Python -m pip install -r $Requirements
}

& $Python -c "import agent_skills, kpi_report_analysis, main; print('Blu Agent Python health check passed')"
if ($LASTEXITCODE -ne 0) { throw "Python health check failed." }

if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot ".env"))) {
    Write-Warning "No .env found. Create it locally before starting; never commit secrets."
}

if ($Start) {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    $stdout = Join-Path $LogDir "feishuagent.out.log"
    $stderr = Join-Path $LogDir "feishuagent.err.log"
    $escapedMain = [Regex]::Escape($Main)
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -eq "python.exe" -and $_.CommandLine -match $escapedMain } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Process -FilePath $Python -ArgumentList @("-u", $Main) -WorkingDirectory $RepoRoot -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr | Out-Null
    Write-Host "Blu Agent started. Logs: $LogDir" -ForegroundColor Green
} else {
    Write-Host "Blu Agent installed from current Git checkout: $RepoRoot" -ForegroundColor Green
}
