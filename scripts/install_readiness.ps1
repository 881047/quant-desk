# Register the readiness check as a per-user scheduled task.
#   powershell -ExecutionPolicy Bypass -File scripts\install_readiness.ps1
#
# Two triggers on weekdays, both read-only:
#   09:00  before the open   -- was yesterday settled?
#   17:45  after the close   -- did today's pipeline deliver?
#
# `daily_job` writes ALERT.txt when it runs and fails. It cannot write anything
# on a day it never starts, which is the 2026-10-29 case: the bar never reached
# the store and a close report was written anyway. This task asks the question
# even when nobody is looking at the desk.

$ErrorActionPreference = 'Stop'
$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\', '/')
$TaskName = 'quant-desk-readiness'
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$Script = Join-Path $Root 'scripts\check_readiness.py'
$Log = Join-Path $Root 'data\readiness.log'

foreach ($path in @($Python, $Script)) {
    if (-not (Test-Path $path -PathType Leaf)) { throw "missing: $path" }
}

# Same guard as install_service.ps1: never replace a task of the same name that
# belongs to something else.
function Assert-ProjectTask {
    param([string]$Name, [string]$ExpectedScript)

    $existing = @(Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue)
    foreach ($task in $existing) {
        $actions = @($task.Actions)
        if (-not $actions.Count) { throw "refusing to replace ${Name}: task has no action" }
        foreach ($taskAction in $actions) {
            $working = [string]$taskAction.WorkingDirectory
            $arguments = [string]$taskAction.Arguments
            $isProject = $false
            if ($working) {
                $fullWorking = [IO.Path]::GetFullPath($working).TrimEnd('\', '/')
                $isProject = $fullWorking.Equals($Root, [StringComparison]::OrdinalIgnoreCase)
            }
            $hasScript = $arguments.IndexOf(
                [IO.Path]::GetFullPath($ExpectedScript),
                [StringComparison]::OrdinalIgnoreCase) -ge 0
            if (-not ($isProject -and $hasScript)) {
                throw "refusing to replace ${Name}: existing action is not bound to $ExpectedScript"
            }
        }
    }
}

Assert-ProjectTask -Name $TaskName -ExpectedScript $Script

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -Command `"& '$Python' '$Script' --quiet *>> '$Log'`"" `
    -WorkingDirectory $Root

$weekdays = @(
    [System.DayOfWeek]::Monday, [System.DayOfWeek]::Tuesday, [System.DayOfWeek]::Wednesday,
    [System.DayOfWeek]::Thursday, [System.DayOfWeek]::Friday
)
$triggers = @(
    (New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At '09:00'),
    (New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At '17:45')
)

# StartWhenAvailable, but no WakeToRun: a missed check is caught up at the next
# logon, while a machine that is off overnight stays off. ExecutionTimeLimit is
# bounded because this script reads and exits — anything longer is a hang.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -Hidden

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
    -Settings $settings -Principal $principal `
    -Description 'quant-desk data readiness check: 09:00 and 17:45 on weekdays' -Force | Out-Null

Write-Output "registered: $TaskName"
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Output "next run: $($info.NextRunTime)"
