# Registers a Windows scheduled task that runs game_watch every N minutes.
# Run once from an elevated or normal PowerShell prompt:
#   .\scripts\register-scheduled-task.ps1
# Run only while signed in (no stored password):
#   .\scripts\register-scheduled-task.ps1 -InteractiveOnly
# Scheduled dry-run (no email):
#   .\scripts\register-scheduled-task.ps1 -DryRun
# Remove with:
#   Unregister-ScheduledTask -TaskName BGPC-GameWatch -Confirm:$false

param(
    [int]$IntervalMinutes = 10,
    [string]$TaskName,
    [switch]$InteractiveOnly,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
if (-not $TaskName) {
    $TaskName = if ($DryRun) { "BGPC-GameWatch-DryRun" } else { "BGPC-GameWatch" }
}
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$Runner = Join-Path $PSScriptRoot "run-game-watch.ps1"

if (-not (Test-Path (Join-Path $RepoRoot "config.yaml"))) {
    throw "config.yaml not found in $RepoRoot - copy config.yaml.example and configure it first."
}
if (-not (Test-Path (Join-Path $RepoRoot ".env"))) {
    throw ".env not found in $RepoRoot - copy .env.example and set SMTP_PASSWORD / SMTP_TO."
}

$runnerArgs = if ($DryRun) { "-DryRun" } else { "" }
$pwsh = Get-Command pwsh -ErrorAction SilentlyContinue
if ($pwsh) {
    $shell = $pwsh.Source
    $shellArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$Runner`" $runnerArgs"
} else {
    $shell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    $shellArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$Runner`" $runnerArgs"
}

$action = New-ScheduledTaskAction -Execute $shell -Argument $shellArgs -WorkingDirectory $RepoRoot
# Task Scheduler rejects [TimeSpan]::MaxValue (P99999999DT23H59M59S); 9999 days is the practical max.
$repetitionDuration = New-TimeSpan -Days 9999
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration $repetitionDuration
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$taskParams = @{
    TaskName    = $TaskName
    Action      = $action
    Trigger     = $trigger
    Settings    = $settings
    Description = if ($DryRun) {
        "BGPC price watcher dry-run (every $IntervalMinutes min, no email)"
    } else {
        "BGPC board-game price watcher (every $IntervalMinutes min)"
    }
}

if ($InteractiveOnly) {
    Register-ScheduledTask @taskParams -ErrorAction Stop | Out-Null
    $runMode = "only when you are signed in"
} else {
    $account = if ($env:USERDOMAIN -and $env:USERDOMAIN -ne $env:COMPUTERNAME) {
        "$env:USERDOMAIN\$env:USERNAME"
    } else {
        $env:USERNAME
    }
    $cred = Get-Credential -UserName $account -Message @"
Enter your Windows password so the task can run when you are signed out.
Stored locally by Task Scheduler (not in this repo).
"@
    if (-not $cred) {
        throw "Registration cancelled - password required for signed-out runs."
    }

    $taskParams.User = $cred.UserName
    $taskParams.Password = $cred.GetNetworkCredential().Password
    Register-ScheduledTask @taskParams -ErrorAction Stop | Out-Null
    $runMode = "whether you are signed in or not"
}

$emailMode = if ($DryRun) { "dry-run (no email)" } else { "sends email" }
Write-Host "Registered '$TaskName' - every $IntervalMinutes minutes, $runMode, $emailMode."
Write-Host "Repo:    $RepoRoot"
Write-Host "Runner:  $Runner"
Write-Host "Logs:    $(Join-Path $RepoRoot 'logs\game-watch.log')"
Write-Host ""
Write-Host "Test now:  powershell -ExecutionPolicy Bypass -File `"$Runner`" -DryRun"
Write-Host "Remove:    Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"
