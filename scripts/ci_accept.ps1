# 知序整合工程 · 本机验收脚本（PowerShell 外壳）
# 用法：
#   cd E:\responsibility\study-platform
#   .\scripts\ci_accept.ps1                         # 全量静态检查 + 全量 pytest（题库跟随 releases/CURRENT，当前 r5）
#   .\scripts\ci_accept.ps1 -Ui -BaseUrl http://127.0.0.1:8840
#                                                   # 额外跑浏览器验收（含 ui-a11y 无障碍；需先手动起隔离实例；服务器不可达直接失败）
#   .\scripts\ci_accept.ps1 -EditorBuild            # 追加「编辑器重新构建 vs web/editor 入库产物」一致性检查
#   .\scripts\ci_accept.ps1 -SkipPytest             # 开发用：跳过全量 pytest（汇总显示 SKIP，退出 3 / ACCEPT INCOMPLETE）
#   .\scripts\ci_accept.ps1 -Stages bank,compileall # 开发用：只跑部分阶段（其余 SKIP，退出 3 / ACCEPT INCOMPLETE）
#   .\scripts\ci_accept.ps1 -BankDir <目录>          # 显式指定题库；默认跟随 releases/CURRENT，r3 需显式指定
#
# 说明：阶段编排/汇总/退出码在 scripts/accept_runner.py（可被 pytest 直接测试）。
#       退出码：0=全部选中阶段真正执行且全过；1=存在失败阶段（含 UI 服务器不可达）；
#       3=未完成（有阶段被跳过或没有阶段真正执行，打印 ACCEPT INCOMPLETE）；2=参数/环境错误。
#       每个阶段的结果与 SKIP 状态都进入最终汇总，并落盘 build_id / bank_version 到报告 JSON。
param(
    [switch]$Ui,
    [string]$BaseUrl = "http://127.0.0.1:8840",
    [string]$BankDir = "",
    [string]$DataDir = "",
    [switch]$SkipPytest,
    [string[]]$Stages = @(),
    [switch]$EditorBuild,
    [string]$ReportPath = ""
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath (Join-Path $PSScriptRoot "..")

# 子进程统一测试环境（accept_runner.py 也会兜底设置，这里保证外壳直接调用时一致）。
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
if (-not $env:STUDY_ALLOWED_HOSTS) { $env:STUDY_ALLOWED_HOSTS = "127.0.0.1,localhost,[::1],testserver" }
if (-not $env:STUDY_INDEX_PREWARM) { $env:STUDY_INDEX_PREWARM = "0" }
if (-not $env:PYTHONIOENCODING) { $env:PYTHONIOENCODING = "utf-8" }

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Host "[FAIL] 未找到 python，无法执行验收" -ForegroundColor Red
    exit 2
}

$arguments = @((Join-Path $PSScriptRoot "accept_runner.py"))
if ($Ui) { $arguments += "--ui" }
if ($BaseUrl) { $arguments += @("--base-url", $BaseUrl) }
if ($BankDir) { $arguments += @("--bank-dir", $BankDir) }
if ($DataDir) { $arguments += @("--data-dir", $DataDir) }
if ($SkipPytest) { $arguments += "--skip-pytest" }
if ($EditorBuild) { $arguments += "--editor-build" }
if ($Stages.Count -gt 0) { $arguments += @("--stages", ($Stages -join ",")) }
if ($ReportPath) { $arguments += @("--report-path", $ReportPath) }

$exitCode = 2
try {
    & $python.Source @arguments
    $exitCode = $LASTEXITCODE
} catch {
    Write-Host "[FAIL] 验收 runner 执行异常：$($_.Exception.Message)" -ForegroundColor Red
    $exitCode = 2
}
exit $exitCode
