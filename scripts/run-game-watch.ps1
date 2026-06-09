# Runs game_watch.py from the repo root. Safe for Task Scheduler (minimal env).
param(
    [string]$Config = "config.yaml",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$LogDir = Join-Path $RepoRoot "logs"
$LogFile = Join-Path $LogDir "game-watch.log"
$Utf8NoBom = New-Object System.Text.UTF8Encoding $false

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
    [System.IO.File]::AppendAllText($LogFile, $line + [Environment]::NewLine, $Utf8NoBom)
}

function Invoke-GameWatch([string]$Uv, [string[]]$UvArgs) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Uv
    $psi.Arguments = (
        $UvArgs | ForEach-Object {
            if ($_ -match '[\s"]') { '"{0}"' -f ($_ -replace '"', '""') } else { $_ }
        }
    ) -join " "
    $psi.WorkingDirectory = $RepoRoot
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = $Utf8NoBom
    $psi.StandardErrorEncoding = $Utf8NoBom
    $psi.CreateNoWindow = $true
    $psi.EnvironmentVariables["PYTHONUTF8"] = "1"
    $psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"

    $process = [System.Diagnostics.Process]::Start($psi)
    $stdout = $process.StandardOutput.ReadToEnd()
    $stderr = $process.StandardError.ReadToEnd()
    $process.WaitForExit()

    return [PSCustomObject]@{
        ExitCode = $process.ExitCode
        Stdout   = $stdout
        Stderr   = $stderr
    }
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Set-Location $RepoRoot

$uv = Find-Uv
$args = @("run", "python", "game_watch.py", "--config", $Config)
if ($DryRun) { $args += "--dry-run" }

Write-Log "START (uv=$uv config=$Config dry_run=$($DryRun.IsPresent))"
try {
    $result = Invoke-GameWatch -Uv $uv -UvArgs $args
    foreach ($line in ($result.Stdout -split "`r?`n")) {
        if ($line.Length -eq 0) { continue }
        Write-Log $line
        if (-not $DryRun) { Write-Output $line }
    }
    foreach ($line in ($result.Stderr -split "`r?`n")) {
        if ($line.Length -eq 0) { continue }
        Write-Log "stderr: $line"
    }
    if ($result.ExitCode -ne 0) {
        throw "game_watch.py exited with code $($result.ExitCode)"
    }
    Write-Log "OK"
    exit 0
}
catch {
    Write-Log "ERROR: $_"
    exit 1
}
