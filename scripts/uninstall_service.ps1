# Remove the quant service scheduled task.
#   powershell -ExecutionPolicy Bypass -File scripts\uninstall_service.ps1
# It stops the watchdog (and its uvicorn child) and unregisters the task.

$ErrorActionPreference = 'Continue'
$TaskName = 'quant-desk-service'
$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\', '/')
$RootPrefix = $Root + [IO.Path]::DirectorySeparatorChar

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

$conn = Get-NetTCPConnection -LocalPort 8811 -State Listen -ErrorAction SilentlyContinue
foreach ($listener in @($conn)) {
    if (-not $listener) { continue }
    $processId = $listener.OwningProcess
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
    $commandLine = [string]$process.CommandLine
    $executablePath = [string]$process.ExecutablePath
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($process.ParentProcessId)" -ErrorAction SilentlyContinue
    $parentCommandLine = [string]$parent.CommandLine
    $inProject = $false
    if ($executablePath) {
        try {
            $fullPath = [IO.Path]::GetFullPath($executablePath)
            $inProject = $fullPath.StartsWith($RootPrefix, [StringComparison]::OrdinalIgnoreCase)
        } catch {
            $inProject = $false
        }
    }
    # The real listener lives in uv's interpreter cache, outside the project.
    # Its own command line carries the project log config, and its shim parent's
    # command line carries the project .venv path; either is stronger evidence
    # than requiring the final ExecutablePath itself to sit under $Root.
    $inProject = $inProject -or
        $commandLine.IndexOf($Root, [StringComparison]::OrdinalIgnoreCase) -ge 0 -or
        $parentCommandLine.IndexOf($Root, [StringComparison]::OrdinalIgnoreCase) -ge 0
    $isProjectUvicorn = $inProject -and
        $commandLine -match '(?i)(-m\s+uvicorn\b|uvicorn(\.exe)?\b)' -and
        $commandLine -match '(?i)service\.api\.app:app' -and
        $commandLine -match '(?i)--port\s+8811\b'
    if ($isProjectUvicorn) {
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
        Write-Output "stopped project uvicorn listener on 8811 (pid $processId)"
    } else {
        Write-Output "left listener on 8811 untouched (pid $processId): not verified as this project's uvicorn"
    }
}

Write-Output "unregistered: $TaskName"
