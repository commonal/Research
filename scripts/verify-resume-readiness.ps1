[CmdletBinding()]
param(
    [switch]$SkipBackend,
    [switch]$SkipFrontend,
    [switch]$SkipBuild,
    [string]$PythonPath = ".venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

$requiredFiles = @(
    "README.md",
    "docs\INDEX.md",
    "docs\PROJECT_HANDOFF.md",
    "docs\resume-readiness.md",
    "docs\workbench-real-ui-acceptance-2026-09-11.md",
    "evals\workbench_golden_tasks.jsonl",
    "evals\baseline\README.md",
    "evals\baseline\records.template.jsonl",
    "evals\baseline\summarize.py",
    "scripts\run-baseline-eval.ps1"
)

foreach ($relativePath in $requiredFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $projectRoot $relativePath))) {
        throw "缺少收束基线文件：$relativePath"
    }
}

$tasks = @(Get-Content -LiteralPath (Join-Path $projectRoot "evals\workbench_golden_tasks.jsonl") | Where-Object { $_.Trim() } | ForEach-Object { $_ | ConvertFrom-Json })
if ($tasks.Count -lt 9) {
    throw "黄金任务集至少需要 9 个任务，当前只有 $($tasks.Count) 个"
}
$requiredKinds = @("selection", "research", "recovery", "quality")
foreach ($kind in $requiredKinds) {
    if (-not ($tasks.kind -contains $kind)) {
        throw "黄金任务集缺少类别：$kind"
    }
}

Write-Host "[1] 收束基线文件：通过（$($requiredFiles.Count) 个）"
Write-Host "[2] 黄金任务集：通过（$($tasks.Count) 个，覆盖 $($requiredKinds -join ', ')）"

if (-not $SkipBackend) {
    if (-not (Test-Path -LiteralPath $PythonPath)) {
        throw "未找到项目虚拟环境 Python：$PythonPath；如只想校验文档，请使用 -SkipBackend"
    }
    $testTempRoot = Join-Path $projectRoot ".resume-readiness-tmp"
    New-Item -ItemType Directory -Force -Path $testTempRoot | Out-Null
    $oldTemp = $env:TEMP
    $oldTmp = $env:TMP
    $env:TEMP = $testTempRoot
    $env:TMP = $testTempRoot
    try {
        & $PythonPath -m pytest tests/test_workbench_exploration_api.py tests/test_workbench_recovery_reconciliation.py tests/test_workbench_continuation.py tests/test_baseline_eval.py
        $backendExitCode = $LASTEXITCODE
    } finally {
        $env:TEMP = $oldTemp
        $env:TMP = $oldTmp
    }
    if ($backendExitCode -ne 0) {
        throw "后端工作台回归失败，退出码 $backendExitCode"
    }
    Write-Host "[3] 后端工作台回归：通过"
} else {
    Write-Host "[3] 后端工作台回归：跳过"
}

if (-not $SkipFrontend) {
    Push-Location (Join-Path $projectRoot "frontend")
    try {
        npm run test
        if ($LASTEXITCODE -ne 0) { throw "前端测试失败，退出码 $LASTEXITCODE" }
        if (-not $SkipBuild) {
            npm run build
            if ($LASTEXITCODE -ne 0) { throw "前端构建失败，退出码 $LASTEXITCODE" }
        }
    } finally {
        Pop-Location
    }
    Write-Host "前端验证：通过"
} else {
    Write-Host "前端验证：跳过"
}

Write-Host "收束基线检查完成。真实浏览器和外部服务验收仍需按 docs/resume-readiness.md 记录 receipt。"
