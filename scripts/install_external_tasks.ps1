# Register the real-account sync as two weekday tasks.
#   powershell -ExecutionPolicy Bypass -File scripts\install_external_tasks.ps1
#
# 09:26 preopen — is the auction line holding? Runs four minutes before the open,
# so the answer is still actionable. Nothing is imported here: the broker's files
# arrive later, and there is no point re-reading yesterday's.
#
# 17:35 close — import anything new from data\external\drop, then write the
# evening report and reconcile fills against the broker's position snapshot.
# 17:35 is five minutes after `quant-desk-daily` (17:30) so the two do not fight
# over the SQLite registry and the report quotes a settled session.
#
# Neither task places an order. Since the 2026-08 接入规范, a personal program
# that sends orders to a broker counter is 程序化交易, an institutional-only
# channel; reading a statement is bookkeeping. The line is crossed by the first
# POST /order, and there is none here — which is why this task is safe to run
# unattended and safe to leave logged in.

$ErrorActionPreference = 'Stop'
$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\', '/')
$Wrapper = Join-Path $Root 'scripts\external_job.ps1'

if (-not (Test-Path $Wrapper -PathType Leaf)) { throw "missing: $Wrapper" }

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

# Validate every destination before changing any task: an unrelated same-name
# task must produce a failure without leaving a half-updated installation.
Assert-ProjectTask -Name 'quant-desk-external-preopen' -ExpectedScript $Wrapper
Assert-ProjectTask -Name 'quant-desk-external-close' -ExpectedScript $Wrapper
Assert-ProjectTask -Name 'quant-desk-external-monitor' -ExpectedScript $Wrapper

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
    -LogonType Interactive -RunLevel Limited

function Register-ExtTask {
    param([string]$Name, [string]$At, [string]$Mode, [string]$Description)

    $action = New-ScheduledTaskAction `
        -Execute 'powershell.exe' `
        -Argument ("-NoProfile -NonInteractive -ExecutionPolicy Bypass " +
                   "-WindowStyle Hidden -File `"$Wrapper`" -Mode $Mode") `
        -WorkingDirectory $Root

    # Holidays need no special case: a report on a closed day finds no new fills
    # and prints the same prices, which costs nothing and is honest about it.
    $trigger = New-ScheduledTaskTrigger `
        -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $At

    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RestartCount 2 `
        -RestartInterval (New-TimeSpan -Minutes 5) `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 20) `
        -Hidden

    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal `
        -Description $Description -Force | Out-Null

    Write-Output "registered: $Name @ $At ($Mode)"
}

Register-ExtTask -Name 'quant-desk-external-preopen' -At '09:26' -Mode 'preopen' `
    -Description 'quant-desk 实盘只读同步：盘前竞价线检查（不下单）'
Register-ExtTask -Name 'quant-desk-external-close' -At '17:35' -Mode 'close' `
    -Description 'quant-desk 实盘只读同步：导入成交/持仓 + 收盘报告（不下单）'

# 盘中监控：每 5 分钟一次，09:30–15:00。任务计划没有"区间重复"的触发器，
# 只有 Repetition，所以先建一个 Weekly 触发器再挂上 Repetition。
$monitorName = 'quant-desk-external-monitor'
$monitorAt = '09:30'
$monitorAction = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument ("-NoProfile -NonInteractive -ExecutionPolicy Bypass " +
               "-WindowStyle Hidden -File `"$Wrapper`" -Mode monitor") `
    -WorkingDirectory $Root
$monitorTrigger = New-ScheduledTaskTrigger `
    -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $monitorAt
$monitorTrigger.Repetition = (New-ScheduledTaskTrigger -Once -At $monitorAt `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Hours 5 -Minutes 30)).Repetition
$monitorSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 4) `
    -Hidden
Register-ScheduledTask -TaskName $monitorName -Action $monitorAction `
    -Trigger $monitorTrigger -Settings $monitorSettings -Principal $principal `
    -Description 'quant-desk 实盘监控：盘中每 5 分钟检查止损/竞价/异动，触发才发邮件' `
    -Force | Out-Null
Write-Output "registered: $monitorName every 5 min 09:30-15:00"

Get-ScheduledTask -TaskName 'quant-desk-external-*' |
    Select-Object TaskName, State |
    Format-Table -AutoSize | Out-String | Write-Output

Get-ScheduledTask -TaskName 'quant-desk-external-*' | ForEach-Object {
    $info = Get-ScheduledTaskInfo -TaskName $_.TaskName
    Write-Output ("  {0}  next={1}" -f $_.TaskName, $info.NextRunTime)
}
