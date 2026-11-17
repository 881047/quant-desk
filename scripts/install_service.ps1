# Register the quant service as a per-user scheduled task.
#   powershell -ExecutionPolicy Bypass -File scripts\install_service.ps1
# No admin required; runs at logon, hidden, restarts on failure.

$ErrorActionPreference = 'Stop'
$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\', '/')
$TaskName = 'quant-desk-service'
$Pythonw = Join-Path $Root '.venv\Scripts\pythonw.exe'
$Supervisor = Join-Path $Root 'scripts\supervise.py'

foreach ($path in @($Pythonw, $Supervisor)) {
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

Assert-ProjectTask -Name $TaskName -ExpectedScript $Supervisor

$action = New-ScheduledTaskAction `
    -Execute $Pythonw `
    -Argument "`"$Supervisor`"" `
    -WorkingDirectory $Root

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -Hidden

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description 'quant-desk paper trading API on 127.0.0.1:8811' -Force | Out-Null

Write-Output "registered: $TaskName"
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
