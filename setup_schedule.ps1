# Registers the twice-daily NewsMap update (08:00 and 20:00) in Windows Task Scheduler.
# Re-run after changing times. Remove with:  Unregister-ScheduledTask -TaskName 'NewsMap update' -Confirm:$false
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$pyw = Join-Path $root '.venv\Scripts\pythonw.exe'
$action = New-ScheduledTaskAction -Execute $pyw -Argument "`"$root\pipeline.py`" --mode auto --scheduled" -WorkingDirectory $root
$triggers = @((New-ScheduledTaskTrigger -Daily -At '08:00'), (New-ScheduledTaskTrigger -Daily -At '20:00'))
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit (New-TimeSpan -Hours 5) -Priority 7 -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'NewsMap update' -Action $action -Trigger $triggers -Settings $settings `
  -Description 'Collects and summarizes AI and security news for NewsMap (low priority, waits if the GPU is busy).' -Force | Out-Null
Get-ScheduledTask -TaskName 'NewsMap update' | Get-ScheduledTaskInfo | Select-Object TaskName, NextRunTime
