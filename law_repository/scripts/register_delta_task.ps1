# 작업 스케줄러 등록 (1회 실행): 매일 09:30 클라우드 델타 동기화.
# PC 가 꺼져 있었으면 다음 부팅 직후 실행(StartWhenAvailable) — 밀린 만큼 watermark 가 따라잡음.
# 해제: Unregister-ScheduledTask -TaskName lawrepo-delta-cloud -Confirm:$false
$script = Join-Path $PSScriptRoot "delta_cloud.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""
$trigger = New-ScheduledTaskTrigger -Daily -At 09:30
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName "lawrepo-delta-cloud" -Action $action `
    -Trigger $trigger -Settings $settings -Force
Write-Host "등록 완료 — 확인: Get-ScheduledTask lawrepo-delta-cloud"
