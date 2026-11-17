# One run of the real-account sync. Called by the three scheduled tasks, and safe
# to run by hand:
#
#   powershell -ExecutionPolicy Bypass -File scripts\external_job.ps1 -Mode close
#   powershell -ExecutionPolicy Bypass -File scripts\external_job.ps1 -Mode preopen
#   powershell -ExecutionPolicy Bypass -File scripts\external_job.ps1 -Mode monitor
#
#   close    import new exports, write the evening report, mail the digest
#   preopen  write the pre-open line, then alert if the auction line broke
#   monitor  alerting only — this is the one that runs every five minutes
#
# The wrapper exists rather than a bare `python ... *>> log` for three reasons:
# python's Chinese output needs PYTHONIOENCODING or the log is mojibake; a run
# needs a timestamped header to be readable weeks later; and a breached level
# has to land somewhere a human will look, which is not a 400-line log.

param([ValidateSet('preopen', 'close', 'monitor')][string]$Mode = 'close')

$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$External = Join-Path $Root 'scripts\external.py'
$Monitor = Join-Path $Root 'scripts\monitor.py'
$Log = Join-Path $Root 'data\external_job.log'
$AlertDir = Join-Path $Root 'data\external'

$env:PYTHONIOENCODING = 'utf-8'
# The Windows shell decodes a child process's output with the console code page,
# so UTF-8 bytes from python arrive as GBK mojibake and land in the log that way.
# Setting the console encoding is what makes the two agree.
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
New-Item -ItemType Directory -Force -Path $AlertDir | Out-Null

$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
"[$stamp] external_job mode=$Mode" | Out-File -Append -Encoding utf8 $Log

function Invoke-LoggedStep {
    param([string]$Name, [scriptblock]$Command)

    (& $Command 2>&1) | Out-File -Append -Encoding utf8 $Log
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        $failedStamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
        "[$failedStamp] $Name failed exit=$code" | Out-File -Append -Encoding utf8 $Log
        exit $code
    }
}

switch ($Mode) {
    'close' {
        Invoke-LoggedStep -Name 'import' -Command { & $Python $External import }
        Invoke-LoggedStep -Name 'report' -Command { & $Python $External report --reconcile }
        Invoke-LoggedStep -Name 'digest' -Command { & $Python $Monitor --digest }
    }
    'preopen' {
        Invoke-LoggedStep -Name 'report-preopen' -Command { & $Python $External report --preopen }
        Invoke-LoggedStep -Name 'monitor' -Command { & $Python $Monitor --once }
    }
    'monitor' {
        # Every five minutes: quiet unless something fired, and deduplicated so
        # one breach is one mail.
        Invoke-LoggedStep -Name 'monitor' -Command { & $Python $Monitor --once }
    }
}
