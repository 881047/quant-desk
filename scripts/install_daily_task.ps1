# Register the data refresh as weekday scheduled tasks.
#   powershell -ExecutionPolicy Bypass -File scripts\install_daily_task.ps1
# No admin required; runs hidden, restarts on failure.
#
# Three passes, because Tushare publishes in three waves:
#
#   17:30  quant-desk-daily          close   — bars, factors, limits, pools, all
#                                              downstream steps
#   21:40  quant-desk-daily-late     late    — moneyflow (19:00),
#                                              moneyflow_hsgt (18:00-20:00)
#   08:50  quant-desk-daily-morning  morning — margin_detail (T+1 08:30) plus a
#                                              retry of the late pair
#
# Why 17:30, not 16:10: Tushare publishes `daily` / `daily_basic` between 15:00
# and **17:00** (its own data page says 15–17 点, the interface page says 15–16).
# 16:10 sits inside that window, so on a slow publication day the run fetches a
# partial session — `daily_job` then records `core_nav` as skipped and the book
# trades on yesterday's close. 17:30 is 30 minutes past the documented ceiling
# and 40 minutes before the 18:10 agent wake-up, so the wake-up reads a complete
# session instead of racing the backfill.
#
# The two extra passes are not padding. Before 2026-11-01 the close pass asked
# for moneyflow / moneyflow_hsgt / margin_detail at 17:30, which cannot succeed:
# the run reported "done", the block stayed one session behind, and nothing
# failed. `/data/status` showed the lag and no one owned it.
#
# Why a task at all: on 2026-10-26 the whole desk sat in `wait_data` because
# nothing scheduled `daily_job.py` — the 16:03 backfill that did run only covered
# `fund_daily`, so every downstream check saw "no bar for today" and refused to
# trade. The guard was right; the pipeline was simply never wired up.

$ErrorActionPreference = 'Stop'
$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\', '/')
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$Job = Join-Path $Root 'scripts\daily_job.py'
$Log = Join-Path $Root 'data\daily_job.log'

foreach ($path in @($Python, $Job)) {
    if (-not (Test-Path $path -PathType Leaf)) { throw "missing: $path" }
}

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

$Passes = @(
    @{ Name = 'quant-desk-daily';         At = '17:30'; Args = '';                What = 'close' },
    @{ Name = 'quant-desk-daily-late';    At = '21:40'; Args = '--pass late';     What = 'late' },
    @{ Name = 'quant-desk-daily-morning'; At = '08:50'; Args = '--pass morning';  What = 'morning' }
)

foreach ($pass in $Passes) {
    Assert-ProjectTask -Name $pass.Name -ExpectedScript $Job

    # pythonw would hide the window but leaves sys.stdout as None, and daily_job
    # prints. A hidden PowerShell wrapper keeps the console off screen *and*
    # keeps the output: one log file, appended, one line per run.
    $suffix = if ($pass.Args) { " $($pass.Args)" } else { '' }
    $command = "& '$Python' '$Job' --days 3$suffix *>> '$Log'"
    $action = New-ScheduledTaskAction `
        -Execute 'powershell.exe' `
        -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -Command `"$command`"" `
        -WorkingDirectory $Root

    # Monday-Friday. Holidays need no special case: daily_job reads trade_cal and
    # skips the session work when the day is closed. The morning pass still runs
    # on a holiday and finds nothing to fetch, which costs one API call.
    $trigger = New-ScheduledTaskTrigger `
        -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $pass.At

    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 10) `
        -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
        -Hidden

    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

    Register-ScheduledTask -TaskName $pass.Name -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal `
        -Description "quant-desk data refresh ($($pass.What) pass) weekdays $($pass.At)" -Force | Out-Null

    Write-Output "registered: $($pass.Name) at $($pass.At)"
}

Get-ScheduledTask -TaskName 'quant-desk-daily*' | Select-Object TaskName, State

# 盘中分帧采集。11-02 那天只有 10:30 抓到过一次，13:18 还在用那份数算封单/成交比，
# 中间 2.8 小时。`/market/pools` 现在会自报 `stale` 与 `age_seconds`，但**报得准不如
# 本来就不旧**。
#
# 一个进程覆盖全天，密度分段（窗口写在 snapshot_intraday.py 的 DEFAULT_WINDOWS 里）：
#   09:25-10:00 / 14:30-15:00  每 30 秒    首封与尾盘，封单变化最快
#   10:00-11:30 / 13:00-14:30  每 3 分钟  封单金额变化慢，但"封这件事"在发生
#   11:30-13:00 不抓           午休的池子就是 11:30 那一刻
#
# 盘中那两段不是凑数：22 个交易日 544 笔炸板样本里，首次封板时间落在 10:00-14:30
# 的占 50.0%。只抓首尾两段，回封模型的训练集少一半。
#
# 只装**一个**写入者。原来那五个 `quant-desk-pools-*` 单帧任务（10:00/11:00/13:30/
# 14:30/14:55）全部落在 -loop 的窗口内，两个进程同一刻写同一个 pool_tape 分区会撞
# 清单指针——11-02 那次三个池分区读 500 就是这么来的。这里顺手把它们停用。
#
# 不加 `--close-snapshot`：那是收盘后按行情自带时间戳打标签用的，盘中用会把快照标错。
#
# 这个循环顺手把 paper 账本也看了：每落一帧就把持仓过一遍 tripwire，写
# reports/paper/。所以**不要**再装第二个"盯模拟盘"的任务——那会让同一时刻有两份
# 现价，而读者分不清那条破线是按哪一份算的。`--no-watch` 可以关掉它。
# ExecutionTimeLimit 必须是 6 小时：调度器默认 PT8M，会在第 8 分钟把循环杀掉，
# 表现只是"数据少了一半"，不会报错。
$Snapshot = Join-Path $Root 'scripts\snapshot_intraday.py'
if (Test-Path $Snapshot) {
    $loopName = 'quant-desk-snapshot-loop'
    Assert-ProjectTask -Name $loopName -ExpectedScript $Snapshot
    $loopAction = New-ScheduledTaskAction `
        -Execute 'powershell.exe' `
        -Argument ("-NoProfile -NonInteractive -WindowStyle Hidden -Command `"& '$Python' " +
                   "'$Snapshot' --loop 30 *>> '$Log'`"") `
        -WorkingDirectory $Root
    $loopTrigger = New-ScheduledTaskTrigger `
        -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At '09:25'
    $loopSettings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours 6) -Hidden
    Register-ScheduledTask -TaskName $loopName -Action $loopAction -Trigger $loopTrigger `
        -Settings $loopSettings -Principal $principal `
        -Description 'quant-desk intraday frame capture 09:25-15:00 (weekdays)' -Force | Out-Null
    Write-Output "registered: $loopName at 09:25"

    foreach ($at in @('10:00', '11:00', '13:30', '14:30', '14:55')) {
        $stale = "quant-desk-pools-$($at.Replace(':', ''))"
        $found = Get-ScheduledTask -TaskName $stale -ErrorAction SilentlyContinue
        if ($found -and $found.State -ne 'Disabled') {
            Disable-ScheduledTask -TaskName $stale | Out-Null
            Write-Output "disabled: $stale (inside the frame-capture window)"
        }
    }
} else {
    Write-Output "skip: $Snapshot not found"
}
Get-ScheduledTask -TaskName 'quant-desk-daily*' | ForEach-Object {
    Get-ScheduledTaskInfo -TaskName $_.TaskName |
        Select-Object TaskName, LastRunTime, NextRunTime
}
