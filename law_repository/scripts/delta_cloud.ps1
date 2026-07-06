# 클라우드 3-스토어 델타 동기화 — Windows 작업 스케줄러용 래퍼.
# 배경: 법제처(law.go.kr)가 GitHub Actions(해외 IP)를 차단해 CI 동기화 불가(2026-07-06 실측).
#       한국 IP 인 이 PC 가 켜질 때 밀린 동기화를 따라잡는다 (delta 는 watermark 기반 멱등).
# 등록:  scripts\register_delta_task.ps1  참고 / 해제: Unregister-ScheduledTask lawrepo-delta-cloud
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent   # law_repository/
Set-Location $root

# .env.cloud 를 프로세스 환경변수로 로드 (환경변수가 .env 파일보다 우선이라 로컬 .env 를 건드리지 않음)
Get-Content ".env.cloud" | Where-Object { $_ -match "^\s*[^#].*=" } | ForEach-Object {
    $k, $v = $_ -split "=", 2
    [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), "Process")
}

$log = "data\delta_cloud.log"
"=== $(Get-Date -Format s) delta_cloud 시작 ===" | Add-Content $log
python -m src.sync.delta 2>&1 | Add-Content $log
"=== $(Get-Date -Format s) 종료 (exit $LASTEXITCODE) ===" | Add-Content $log
exit $LASTEXITCODE
