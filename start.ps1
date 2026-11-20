param([int]$Port = 8765, [string]$BankDir = "")
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

if (-not $BankDir) {
    $resolved = & (Join-Path $PSScriptRoot 'scripts\bank_dir.ps1')
    if ($LASTEXITCODE -eq 0 -and $resolved) { $BankDir = $resolved.Trim() }
}
if ($BankDir) {
    $env:STUDY_BANK_DIR = (Resolve-Path -LiteralPath $BankDir).Path
    Write-Host "题库目录: $env:STUDY_BANK_DIR"
}

python -m uvicorn app.main:app --host 127.0.0.1 --port $Port
