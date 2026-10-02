[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Records,
    [string]$Tasks = ".\evals\workbench_golden_tasks.jsonl",
    [string]$OutputDir = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "未找到项目虚拟环境 Python：$pythonPath"
}
if (-not (Test-Path -LiteralPath $Records)) {
    throw "找不到评估记录：$Records"
}
if (-not $OutputDir) {
    $OutputDir = Join-Path $projectRoot ("evals\baseline\runs\" + (Get-Date -Format "yyyy-MM-dd-HHmmss"))
}
$resolvedOutputDir = if ([System.IO.Path]::IsPathRooted($OutputDir)) {
    $OutputDir
} else {
    Join-Path $projectRoot $OutputDir
}
$arguments = @(
    (Join-Path $projectRoot "evals\baseline\summarize.py"),
    "--records", (Resolve-Path -LiteralPath $Records),
    "--tasks", (Resolve-Path -LiteralPath $Tasks),
    "--output-dir", $resolvedOutputDir
)
& $pythonPath @arguments
if ($LASTEXITCODE -ne 0) {
    throw "基线评估未完成，退出码 $LASTEXITCODE"
}
Write-Host "基线评估已写入：$resolvedOutputDir"
