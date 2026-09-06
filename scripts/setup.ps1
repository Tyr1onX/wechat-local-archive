$ErrorActionPreference = "Stop"

$Python = "py"
& $Python -3.12 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -e ".[voice]"

Write-Host ""
Write-Host "Ready. First run:" -ForegroundColor Green
Write-Host "  .\.venv\Scripts\wechat-archive.exe doctor"
Write-Host "  .\.venv\Scripts\wechat-archive.exe bootstrap"
