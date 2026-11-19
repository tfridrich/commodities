# Sets up the daily oil & gas update on this PC. Started by setup.bat - no admin rights needed.
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$taskName = "Commodities - oil and gas update"

# 1. find Python (Anaconda first)
$candidates = @("$env:USERPROFILE\anaconda3\python.exe", "$env:LOCALAPPDATA\anaconda3\python.exe",
                "$env:ProgramData\anaconda3\python.exe", "$env:USERPROFILE\miniconda3\python.exe")
$py = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $py) { $c = Get-Command python.exe -ErrorAction SilentlyContinue; if ($c) { $py = $c.Source } }
if (-not $py) { Write-Host "Python was not found. Install Anaconda or Python and run setup again." -ForegroundColor Red; exit 1 }
$pyw = Join-Path (Split-Path $py) "pythonw.exe"
if (-not (Test-Path $pyw)) { $pyw = $py }
Write-Host "Using Python: $py"

# 2. token, EIA key and a test run
& $py "$here\update_from_pc.py" --setup
if ($LASTEXITCODE -ne 0) { Write-Host "`nSetup did not finish - see the message above." -ForegroundColor Red; exit 1 }

# 3. scheduled task: 07:15 and 19:15 every day, and 5 minutes after you log on.
#    "StartWhenAvailable" = if the PC was off or asleep at that time, run as soon as it is back.
$action = New-ScheduledTaskAction -Execute $pyw -Argument "`"$here\update_from_pc.py`"" -WorkingDirectory $here
$triggers = @((New-ScheduledTaskTrigger -Daily -At "07:15"), (New-ScheduledTaskTrigger -Daily -At "19:15"))
$logon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$logon.Delay = "PT5M"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
$desc = "Fetches oil & gas prices from Yahoo/EIA/ECB and uploads them to github.com/tfridrich/commodities. Log: $here\update_log.txt"
try {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger ($triggers + $logon) -Settings $settings -Description $desc -Force | Out-Null
} catch {
    # some company PCs only allow time triggers without admin rights
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers -Settings $settings -Description $desc -Force | Out-Null
    Write-Host "(The 'at log on' trigger was not allowed on this PC; the task runs at 07:15 and 19:15, or as soon as the PC is back on.)"
}
Write-Host "`nDone. The task '$taskName' runs at 07:15 and 19:15, after you log on, and catches up when the PC was off." -ForegroundColor Green
Write-Host "To run it now: double-click run_now.bat. The log is update_log.txt in this folder."
