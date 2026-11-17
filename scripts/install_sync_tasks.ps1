# Register the weekly research-data sync (opt-in, outside the 17:30 daily job).
#
# Why a separate task: the daily job owns the trading session and must finish
# before 18:00. The quarterly/forecast syncs rewrite whole `all` partitions and
# can take tens of minutes; Sunday 20:30 is deliberately away from the 17:30
# window, the 19:30 reconciliation and the 21:40 late pass.
#
# Idempotent: -Force re-registers without creating duplicates.
$ErrorActionPreference = 'Stop'

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$Job = Join-Path $Root 'scripts\sync_research_data.py'
$Log = Join-Path $Root 'data\research_sync.log'
$TaskName = 'quant-desk-research-sync'

if (-not (Test-Path $Python)) { throw "python not found: $Python" }
if (-not (Test-Path $Job)) { throw "job not found: $Job" }

$command = "& '$Python' '$Job' --batch 2000 *>> '$Log'"
$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -Command `"$command`"" `
    -WorkingDirectory $Root

$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At '20:30'
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -Hidden
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description 'quant-desk weekly research data sync (fina_indicator / forecast / express)' `
    -Force | Out-Null

Write-Output "registered: $TaskName (Sunday 20:30, --batch 2000)"
Get-ScheduledTask -TaskName $TaskName |
    Select-Object TaskName, State |
    Format-Table -AutoSize
Get-ScheduledTaskInfo -TaskName $TaskName |
    Select-Object LastRunTime, NextRunTime |
    Format-Table -AutoSize
