# Restart the quant API and prove that the replacement loaded the current code.
#   powershell -ExecutionPolicy Bypass -File scripts\restart_service.ps1
# Stopping the scheduled task alone can leave its uvicorn child listening. Stop
# only the verified project listener and let the already-running watchdog replace it.

param([int]$TimeoutSeconds = 90)

$ErrorActionPreference = 'Stop'
$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\', '/')
$TaskName = 'quant-desk-service'
$Supervisor = [IO.Path]::GetFullPath((Join-Path $Root 'scripts\supervise.py'))

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
$bound = @($task.Actions | Where-Object {
    ([string]$_.Arguments).IndexOf($Supervisor, [StringComparison]::OrdinalIgnoreCase) -ge 0
})
if (-not $bound.Count) {
    throw "refusing to restart ${TaskName}: its action is not bound to $Supervisor"
}

$listeners = @()
try {
    $listeners = @(Get-NetTCPConnection -LocalPort 8811 -State Listen -ErrorAction Stop)
} catch {
    $listeners = @()
}
if ($listeners.Count -gt 1) {
    throw 'refusing to restart: more than one process is listening on 8811'
}

$before = $null
if ($listeners.Count -eq 1) {
    $before = [int]$listeners[0].OwningProcess
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$before" -ErrorAction Stop
    if (-not $process) { throw "listener pid $before disappeared before it could be verified" }
    $commandLine = [string]$process.CommandLine
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($process.ParentProcessId)" -ErrorAction SilentlyContinue
    $parentCommandLine = [string]$parent.CommandLine
    $isUvicorn = $commandLine -match '(?i)(-m\s+uvicorn\b|uvicorn(\.exe)?\b)' -and
        $commandLine -match '(?i)service\.api\.app:app' -and
        $commandLine -match '(?i)--port\s+8811\b'
    $isProject = $commandLine.IndexOf($Root, [StringComparison]::OrdinalIgnoreCase) -ge 0 -or
        $parentCommandLine.IndexOf($Root, [StringComparison]::OrdinalIgnoreCase) -ge 0
    if (-not ($isUvicorn -and $isProject)) {
        throw "refusing to stop pid ${before}: listener is not verified as this project's uvicorn"
    }
    Stop-Process -Id $before -Force -ErrorAction Stop
}

if ($task.State -ne 'Running') {
    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
}

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$health = $null
$after = $null
do {
    Start-Sleep -Seconds 2
    try {
        $after = [int](Get-NetTCPConnection -LocalPort 8811 -State Listen -ErrorAction Stop |
            Select-Object -First 1).OwningProcess
    } catch {
        $after = $null
    }
    $newListener = $after -and ($null -eq $before -or $after -ne $before)
    if ($newListener) {
        try {
            $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8811/health' -TimeoutSec 5
        } catch {
            $health = $null
        }
    }
    $codeChanged = $null
    if ($health -and $health.build) { $codeChanged = $health.build.code_changed_since_start }
    $ready = $health -and $health.ok -and $newListener -and
        ($null -ne $codeChanged) -and (-not $codeChanged)
} until ($ready -or (Get-Date) -ge $deadline)

if (-not $ready) {
    $commit = if ($health -and $health.build) { $health.build.commit } else { $null }
    $started = if ($health -and $health.build) { $health.build.started_at } else { $null }
    throw "replacement service is not ready: before=$before after=$after commit=$commit started=$started code_changed=$codeChanged"
}

[pscustomobject]@{
    PreviousPid = $before
    CurrentPid = $after
    StartedAt = $health.build.started_at
    Commit = $health.build.commit
    CodeChangedSinceStart = $health.build.code_changed_since_start
    CodeFingerprint = $health.build.code_fingerprint
    Dirty = $health.build.dirty
}
