#Requires -RunAsAdministrator
# NATURE_STOCK 서비스 중지 및 제거 (관리자 PowerShell)
$ErrorActionPreference = "Stop"
$Name = "NATURE_STOCK"
$nssm = (Get-Command nssm -ErrorAction SilentlyContinue).Source
if (-not (Get-Service $Name -ErrorAction SilentlyContinue)) { Write-Host "$Name 서비스가 없습니다."; exit 0 }
Stop-Service $Name -Force -ErrorAction SilentlyContinue
if ($nssm) { & $nssm remove $Name confirm } else { sc.exe delete $Name }
Write-Host "$Name 서비스를 제거했습니다."
