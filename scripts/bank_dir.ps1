param([string]$Version = "")
# 解析 study-platform 应使用的题库目录：
#   1) -Version 指定 -> 高考题库_按学科\releases\<Version>
#   2) releases\CURRENT 指针指向的版本
#   3) 回落到基线 高考题库_按学科
$repo = Split-Path -Parent $PSScriptRoot
$bank = Join-Path (Split-Path -Parent $repo) '高考题库_按学科'
$releases = Join-Path $bank 'releases'

if (-not $Version) {
    $pointer = Join-Path $releases 'CURRENT'
    if (Test-Path -LiteralPath $pointer) { $Version = (Get-Content -LiteralPath $pointer -Raw).Trim() }
}
$target = $bank
if ($Version) {
    $candidate = Join-Path $releases $Version
    if (Test-Path -LiteralPath (Join-Path $candidate '题库.db')) { $target = $candidate }
}
Write-Output $target
