#Requires -RunAsAdministrator
<#
  NATURE_STOCK 윈도우 서비스 등록 (PC 부팅 시 app.py 자동 실행)

  사용법 (관리자 PowerShell):
      powershell -ExecutionPolicy Bypass -File .\service\install_service.ps1

  - 서비스 래퍼로 NSSM 을 사용한다 (파이썬 스크립트는 그 자체로 윈도우 서비스가 될 수 없음).
  - 기본은 LocalSystem 계정으로 실행 (비밀번호 불필요, 로그인하지 않아도 부팅 시 시작).
    Python 패키지가 사용자 영역에 있어 접근이 안 되는 환경이면 -AsCurrentUser 로 현재 계정(비밀번호 입력)으로 실행.
  - -Yes : NSSM 설치 확인 질문을 건너뜀
  - 부팅 후 자동(지연) 시작, 비정상 종료 시 30초 뒤 자동 재시작. 로그는 logs	rade.log.
#>
param([switch]$AsCurrentUser, [switch]$Yes)
$ErrorActionPreference = "Stop"
$Name = "NATURE_STOCK"
$Root = Split-Path -Parent $PSScriptRoot
$App = Join-Path $Root "app.py"
$LogDir = Join-Path $Root "logs"

if (-not (Test-Path $App)) { throw "app.py 를 찾을 수 없습니다: $App" }

# --- Python 경로 (WindowsApps 별칭이 아닌 실제 경로)
$Py = (& python -c "import sys; print(sys.executable)").Trim()
if (-not (Test-Path $Py)) { throw "python 실행 파일을 찾을 수 없습니다: $Py" }
Write-Host "Python : $Py"
Write-Host "앱     : $App"

# --- NSSM 확인 / 설치
$nssm = (Get-Command nssm -ErrorAction SilentlyContinue).Source
if (-not $nssm) {
    Write-Host "`nNSSM 이 설치되어 있지 않습니다."
    $ans = if ($Yes) { "Y" } else { Read-Host "winget 으로 NSSM(NSSM.NSSM) 을 설치할까요? (Y/N)" }
    if ($ans -notmatch '^[Yy]') { throw "NSSM 이 필요합니다. https://nssm.cc 에서 설치한 뒤 다시 실행하세요." }
    winget install --id NSSM.NSSM -e --accept-package-agreements --accept-source-agreements
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    $nssm = (Get-Command nssm -ErrorAction SilentlyContinue).Source
    if (-not $nssm) {
        $nssm = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages", "C:\Program Files\WinGet" -Recurse -Filter nssm.exe -ErrorAction SilentlyContinue |
                Where-Object { $_.FullName -match "win64" } | Select-Object -First 1 -ExpandProperty FullName
    }
    if (-not $nssm) { throw "NSSM 설치 후에도 nssm.exe 를 찾지 못했습니다. 새 PowerShell 창에서 다시 실행하세요." }
}
Write-Host "NSSM   : $nssm"

# --- 기존 서비스가 있으면 교체
if (Get-Service $Name -ErrorAction SilentlyContinue) {
    Write-Host "`n기존 $Name 서비스를 제거하고 다시 등록합니다."
    & $nssm stop $Name | Out-Null
    & $nssm remove $Name confirm | Out-Null
}

# --- 서비스 실행 계정
$plain = $null
if ($AsCurrentUser) {
    $user = "$env:USERDOMAIN\$env:USERNAME"
    $cred = Get-Credential -UserName $user -Message "서비스를 실행할 계정의 Windows 비밀번호를 입력하세요 ($Name)"
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($cred.Password))
}

New-Item -ItemType Directory -Force $LogDir | Out-Null

& $nssm install $Name $Py "`"$App`""
& $nssm set $Name AppDirectory $Root
& $nssm set $Name DisplayName "NATURE_STOCK (자동매매 + 대시보드)"
& $nssm set $Name Description "STOCK_INVEST app.py - 한국투자증권 자동매매 스케줄러와 웹 대시보드"
if ($AsCurrentUser) { & $nssm set $Name ObjectName $cred.UserName $plain } else { & $nssm set $Name ObjectName LocalSystem }
& $nssm set $Name Start SERVICE_DELAYED_AUTO_START
& $nssm set $Name AppEnvironmentExtra "PYTHONUNBUFFERED=1" "PYTHONIOENCODING=utf-8"
& $nssm set $Name AppExit Default Restart
& $nssm set $Name AppRestartDelay 30000
# 주의: AppStdout/AppStderr(출력 리디렉션)을 설정하면 서비스 중지 시 NSSM 이 "중지 중"에서 멈추는 문제가 있어 쓰지 않는다.
# 앱 로그는 app.py 가 logs	rade.log 에 직접 기록한다.
& $nssm set $Name AppStopMethodConsole 8000
$plain = $null

& $nssm start $Name
Start-Sleep 3
Get-Service $Name | Format-Table Name, Status, StartType -AutoSize
Write-Host "완료. 로그: $LogDir	rade.log / 대시보드: http://127.0.0.1:8765"
Write-Host "관리: Get-Service $Name | Restart-Service $Name | Stop-Service $Name"
