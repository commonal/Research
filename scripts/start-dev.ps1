[CmdletBinding()]
param(
    [int]$BackendPort = 8001,
    [int]$FrontendPort = 5173,
    [switch]$StartDatabase,
    [switch]$Reload,
    [switch]$Stop,
    [int]$StartupTimeoutSeconds = 45
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$stateRoot = Join-Path $projectRoot ".dev"
$logRoot = Join-Path $stateRoot "logs"
$statePath = Join-Path $stateRoot "research-pulse-dev.json"

function Stop-ProcessTree {
    param([int]$RootProcessId)

    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$RootProcessId" -ErrorAction SilentlyContinue
    foreach ($child in $children) {
        Stop-ProcessTree -RootProcessId ([int]$child.ProcessId)
    }
    Stop-Process -Id $RootProcessId -Force -ErrorAction SilentlyContinue
}

function Stop-ManagedServers {
    if (-not (Test-Path -LiteralPath $statePath)) {
        Write-Host "No services started by this script were found."
        return
    }

    $state = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
    foreach ($entry in @($state.processes)) {
        $process = Get-Process -Id ([int]$entry.pid) -ErrorAction SilentlyContinue
        if ($null -eq $process) {
            continue
        }

        $recordedStart = [datetime]::Parse($entry.started_at).ToUniversalTime()
        $actualStart = $process.StartTime.ToUniversalTime()
        if ([math]::Abs(($actualStart - $recordedStart).TotalSeconds) -gt 2) {
            Write-Warning "PID $($entry.pid) was reused by another process and was not stopped."
            continue
        }
        Stop-ProcessTree -RootProcessId ([int]$entry.pid)
        Write-Host "Stopped $($entry.name) (PID $($entry.pid))."
    }
    Remove-Item -LiteralPath $statePath -Force
}

function Assert-PortAvailable {
    param([int]$Port, [string]$Name)

    $listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    if ($listener) {
        throw "$Name port $Port is already in use. Stop the existing service or choose another port."
    }
}

function Wait-HttpReady {
    param([string]$Name, [string]$Url, [System.Diagnostics.Process]$Process)

    $deadline = [datetime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
    while ([datetime]::UtcNow -lt $deadline) {
        if ($Process.HasExited) {
            throw "$Name process exited during startup. Check logs under $logRoot."
        }
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) {
                return
            }
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    }
    throw "$Name was not ready within $StartupTimeoutSeconds seconds. Check logs under $logRoot."
}

if ($Stop) {
    Stop-ManagedServers
    exit 0
}

if (Test-Path -LiteralPath $statePath) {
    throw "An existing launch record was found at $statePath. Run .\scripts\start-dev.ps1 -Stop first."
}

$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$frontendRoot = Join-Path $projectRoot "frontend"
$npm = (Get-Command npm.cmd -ErrorAction SilentlyContinue).Source

if (-not (Test-Path -LiteralPath $python)) {
    throw "Project virtual environment was not found: $python"
}
if (-not $npm) {
    throw "npm.cmd was not found. Install Node.js first."
}
if (-not (Test-Path -LiteralPath (Join-Path $frontendRoot "node_modules"))) {
    throw "frontend/node_modules was not found. Run npm install in the frontend directory first."
}

Assert-PortAvailable -Port $BackendPort -Name "Backend"
Assert-PortAvailable -Port $FrontendPort -Name "Frontend"

if ($StartDatabase) {
    $docker = (Get-Command docker.exe -ErrorAction SilentlyContinue).Source
    if (-not $docker) {
        throw "Docker CLI was not found; PostgreSQL cannot be started."
    }
    & $docker compose -f (Join-Path $projectRoot "compose.researchrag.yaml") up -d postgres
    if ($LASTEXITCODE -ne 0) {
        throw "PostgreSQL failed to start."
    }
}

New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backendOut = Join-Path $logRoot "backend-$stamp.out.log"
$backendErr = Join-Path $logRoot "backend-$stamp.err.log"
$frontendOut = Join-Path $logRoot "frontend-$stamp.out.log"
$frontendErr = Join-Path $logRoot "frontend-$stamp.err.log"

$backendArgs = @(
    "-m", "uvicorn",
    "research_pulse.api.runtime:create_runtime_app",
    "--factory", "--host", "127.0.0.1", "--port", "$BackendPort"
)
if ($Reload) {
    $backendArgs += "--reload"
}

$backend = Start-Process -FilePath $python -ArgumentList $backendArgs `
    -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $backendOut -RedirectStandardError $backendErr

$oldProxy = $env:VITE_API_PROXY
$env:VITE_API_PROXY = "http://127.0.0.1:$BackendPort"
try {
    $frontend = Start-Process -FilePath $npm `
        -ArgumentList @("run", "dev", "--", "--host", "127.0.0.1", "--port", "$FrontendPort") `
        -WorkingDirectory $frontendRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $frontendOut -RedirectStandardError $frontendErr
}
catch {
    Stop-ProcessTree -RootProcessId $backend.Id
    throw
}
finally {
    $env:VITE_API_PROXY = $oldProxy
}

$state = @{
    created_at = [datetime]::UtcNow.ToString("o")
    backend_url = "http://127.0.0.1:$BackendPort"
    frontend_url = "http://127.0.0.1:$FrontendPort"
    processes = @(
        @{ name = "backend"; pid = $backend.Id; started_at = $backend.StartTime.ToUniversalTime().ToString("o") },
        @{ name = "frontend"; pid = $frontend.Id; started_at = $frontend.StartTime.ToUniversalTime().ToString("o") }
    )
}
$state | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $statePath -Encoding UTF8

try {
    Wait-HttpReady -Name "Backend" -Url "http://127.0.0.1:$BackendPort/api/health" -Process $backend
    Wait-HttpReady -Name "Frontend" -Url "http://127.0.0.1:$FrontendPort/" -Process $frontend
}
catch {
    Stop-ManagedServers
    throw
}

Write-Host "Research Pulse is ready:"
Write-Host "  Frontend: http://127.0.0.1:$FrontendPort/"
Write-Host "  Backend:  http://127.0.0.1:$BackendPort/"
Write-Host "  Logs:     $logRoot"
Write-Host "Stop services: .\scripts\start-dev.ps1 -Stop"
