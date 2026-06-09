# Runs game_watch.py from the repo root. Safe for Task Scheduler (minimal env).
param(
    [string]$Config = "config.yaml",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$LogDir = Join-Path $RepoRoot "logs"
$LogFile = Join-Path $LogDir "game-watch.log"

function Find-Uv {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $candidates = @(
        "$env:LOCALAPPDATA\Programs\uv\uv.exe",
        "$env:USERPROFILE\.local\bin\uv.exe",
        "$env:USERPROFILE\.cargo\bin\uv.exe"
    )
    foreach ($path in $candidates) {
        if (Test-Path $path) { return $path }
    }

    throw "uv not found on PATH. Install: https://docs.astral.sh/uv/"
}

function Write-Log([string]$Message) {
    $line = "{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $Message
    Add-Content -Path $LogFile -Value $line -Encoding utf8
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Set-Location $RepoRoot

$uv = Find-Uv
$args = @("run", "python", "game_watch.py", "--config", $Config)
if ($DryRun) { $args += "--dry-run" }

Write-Log "START (uv=$uv config=$Config dry_run=$($DryRun.IsPresent))"
try {
    & $uv @args 2>&1 | ForEach-Object {
        $text = $_.ToString()
        Write-Log $text
        if (-not $DryRun) { Write-Output $text }
    }
    if ($LASTEXITCODE -ne 0) {
        throw "game_watch.py exited with code $LASTEXITCODE"
    }
    Write-Log "OK"
    exit 0
}
catch {
    Write-Log "ERROR: $_"
    exit 1
}
