param(
    [switch]$NoStart,
    [switch]$NoPull,
    [switch]$SkipDependencyInstall
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

if (-not $NoPull) {
    $Git = Get-Command git.exe -ErrorAction Stop
    & $Git.Source -C $RepoRoot rev-parse --is-inside-work-tree | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Not a Git worktree: $RepoRoot" }
    $Dirty = & $Git.Source -C $RepoRoot status --porcelain
    if ($Dirty) { throw "Git worktree is not clean. Commit or stash changes before updating." }
    & $Git.Source -C $RepoRoot rev-parse --abbrev-ref --symbolic-full-name "@{upstream}" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Current branch has no upstream. Configure one before updating." }
    & $Git.Source -C $RepoRoot pull --ff-only
    if ($LASTEXITCODE -ne 0) { throw "git pull --ff-only failed." }
}

$Installer = Join-Path $PSScriptRoot "install_blu_agent.ps1"
& $Installer -Start:(-not $NoStart) -SkipDependencyInstall:$SkipDependencyInstall
