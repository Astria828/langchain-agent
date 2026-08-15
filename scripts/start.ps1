# 织境本地启动器：在当前 PowerShell 窗口中统一托管前端和后端进程。
[CmdletBinding()]
param(
    [switch]$NoBrowser
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$frontendRoot = Join-Path $projectRoot 'frontend'
$frontendPackage = Join-Path $frontendRoot 'package.json'
$frontendModules = Join-Path $frontendRoot 'node_modules'
$backendProcess = $null
$frontendProcess = $null

function Stop-ChildProcessTree {
    param(
        [System.Diagnostics.Process]$Process
    )

    if ($null -ne $Process -and -not $Process.HasExited) {
        $Process.Kill($true)
        $Process.WaitForExit(5000) | Out-Null
    }
}

function Wait-ApplicationReady {
    param(
        [System.Diagnostics.Process]$Backend,
        [System.Diagnostics.Process]$Frontend,
        [int]$TimeoutSeconds = 30
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $backendReady = $false
    $frontendReady = $false

    while ((Get-Date) -lt $deadline) {
        if ($Backend.HasExited) {
            throw "后端进程提前退出，退出码：$($Backend.ExitCode)"
        }
        if ($Frontend.HasExited) {
            throw "前端进程提前退出，退出码：$($Frontend.ExitCode)"
        }

        if (-not $backendReady) {
            try {
                $response = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/api/health' -TimeoutSec 2
                $backendReady = $response.StatusCode -eq 200
            }
            catch [System.Net.Http.HttpRequestException] {
                # 服务启动期间连接尚未建立，继续等待到统一超时。
            }
            catch [System.Threading.Tasks.TaskCanceledException] {
                # 服务进程已启动但尚未完成响应，继续等待到统一超时。
            }
        }

        if (-not $frontendReady) {
            try {
                $response = Invoke-WebRequest -Uri 'http://localhost:5173' -TimeoutSec 2
                $frontendReady = $response.StatusCode -eq 200
            }
            catch [System.Net.Http.HttpRequestException] {
                # Vite 尚未开始监听，继续等待到统一超时。
            }
            catch [System.Threading.Tasks.TaskCanceledException] {
                # Vite 已开始监听但尚未完成响应，继续等待到统一超时。
            }
        }

        if ($backendReady -and $frontendReady) {
            return
        }
        Start-Sleep -Milliseconds 300
    }

    throw "应用在 $TimeoutSeconds 秒内未完成启动，请查看上方日志。"
}

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw '未找到 .venv\Scripts\python.exe，请先按 README 完成后端安装。'
}
if (-not (Test-Path -LiteralPath $frontendPackage -PathType Leaf)) {
    throw '未找到 frontend\package.json，项目目录不完整。'
}
if (-not (Test-Path -LiteralPath $frontendModules -PathType Container)) {
    throw '未找到 frontend\node_modules，请先在 frontend 目录执行 npm ci。'
}

$npmPath = (Get-Command npm.cmd -ErrorAction Stop).Source

try {
    Write-Host '正在升级本地数据库...' -ForegroundColor Cyan
    & $pythonPath (Join-Path $projectRoot 'scripts\init_db.py')
    if ($LASTEXITCODE -ne 0) {
        throw "数据库升级失败，退出码：$LASTEXITCODE"
    }

    Write-Host '正在启动后端与前端...' -ForegroundColor Cyan
    $backendProcess = Start-Process `
        -FilePath $pythonPath `
        -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--app-dir', 'backend', '--host', '127.0.0.1', '--port', '8000') `
        -WorkingDirectory $projectRoot `
        -NoNewWindow `
        -PassThru

    $frontendProcess = Start-Process `
        -FilePath $npmPath `
        -ArgumentList @('run', 'dev') `
        -WorkingDirectory $frontendRoot `
        -NoNewWindow `
        -PassThru

    Wait-ApplicationReady -Backend $backendProcess -Frontend $frontendProcess
    Write-Host ''
    Write-Host '织境已启动：http://localhost:5173' -ForegroundColor Green
    Write-Host '保持此窗口运行；按 Ctrl+C 或关闭窗口即可停止前后端。' -ForegroundColor Yellow
    if (-not $NoBrowser) {
        Start-Process 'http://localhost:5173'
    }

    while (-not $backendProcess.HasExited -and -not $frontendProcess.HasExited) {
        Start-Sleep -Seconds 1
    }

    if ($backendProcess.HasExited) {
        throw "后端进程已退出，退出码：$($backendProcess.ExitCode)"
    }
    throw "前端进程已退出，退出码：$($frontendProcess.ExitCode)"
}
finally {
    Write-Host ''
    Write-Host '正在停止织境服务...' -ForegroundColor Cyan
    Stop-ChildProcessTree -Process $frontendProcess
    Stop-ChildProcessTree -Process $backendProcess
    Write-Host '前后端服务已停止。' -ForegroundColor Green
}
