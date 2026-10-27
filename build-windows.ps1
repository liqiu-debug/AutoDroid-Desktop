# ============================================================
# AutoDroid Desktop — Windows 一键构建脚本
#
# 前置条件（一次性安装）：
#   1. Windows 10/11 x64
#   2. Python 3.11+（安装时勾选 "Add python.exe to PATH"）
#   3. Node.js 20+（含 npm）
#   4. 本仓库（AutoDroid-Desktop）与 AutoDroid 平台仓库并排放置：
#      <任意目录>\AutoDroid-Desktop
#      <任意目录>\AutoDroid          （含 frontend/）
#
# 用法（PowerShell，在 AutoDroid-Desktop 目录下）：
#   powershell -ExecutionPolicy Bypass -File .\build-windows.ps1
#
# 产物：release\AutoDroid Setup <版本>.exe（NSIS 安装包，内置引擎+前端+资源）
# 说明：引擎必须在 Windows 上构建（PyInstaller 不支持跨平台编译），
#       本脚本自动完成 引擎 → 前端 → 壳 → 安装包 全链路。
# ============================================================
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Fail($msg) { Write-Host "[X] $msg" -ForegroundColor Red; exit 1 }
function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Ok($msg) { Write-Host "    OK: $msg" -ForegroundColor Green }

Set-Location -Path $PSScriptRoot

# ---------- 0. 环境检查 ----------
Step '检查构建环境'
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { Fail '未找到 python，请安装 Python 3.11+ 并勾选 Add python.exe to PATH' }
$node = Get-Command node -ErrorAction SilentlyContinue
if (-not $node) { Fail '未找到 node，请安装 Node.js 20+' }
$pyVer = python --version 2>&1
Ok "python: $pyVer"
Ok "node: $(node --version) / npm: $(npm --version)"

# ---------- 1. 引擎（PyInstaller onedir → release-engine\autodroid-engine\autodroid-engine.exe） ----------
Step '构建本地引擎（首次约 2-5 分钟）'
if (-not (Test-Path '.venv\Scripts\python.exe')) {
    python -m venv .venv
}
.\.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
.\.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt pyinstaller
if (Test-Path 'release-engine') { Remove-Item -Recurse -Force 'release-engine' }
if (Test-Path 'build-engine') { Remove-Item -Recurse -Force 'build-engine' }
.\.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm --distpath release-engine --workpath build-engine engine.spec
$engineExe = 'release-engine\autodroid-engine\autodroid-engine.exe'
if (-not (Test-Path $engineExe)) { Fail "引擎产物缺失: $engineExe" }
Ok "引擎就绪: $engineExe"

# ---------- 2. 前端 dist（AutoDroid/frontend，缺省由脚本自动构建） ----------
Step '准备前端产物 frontend/dist'
$frontendDist = '..\AutoDroid\frontend\dist\index.html'
if (-not (Test-Path $frontendDist)) {
    Write-Host '    dist 不存在，自动构建前端（首次需 npm install）...'
    Push-Location '..\AutoDroid\frontend'
    npm install
    npm run build
    Pop-Location
}
if (-not (Test-Path $frontendDist)) { Fail "前端构建失败：$frontendDist 仍不存在" }
Ok "前端就绪: $(Resolve-Path '..\AutoDroid\frontend\dist')"

# ---------- 3. 壳依赖 ----------
Step '安装桌面壳依赖（首次）'
if (-not (Test-Path 'node_modules')) { npm install }
Ok '依赖就绪'

# ---------- 4. 出 Windows 安装包 ----------
Step '构建 NSIS 安装包'
npx electron-builder --win nsis --x64
if ($LASTEXITCODE -ne 0) { Fail "electron-builder 失败（exit=$LASTEXITCODE）" }

$setup = Get-ChildItem 'release' -Filter '*.exe' | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $setup) { Fail 'release 目录未找到 .exe 安装包' }
Ok ("安装包: " + $setup.FullName + ' (' + [math]::Round($setup.Length / 1MB, 1) + ' MB)')

Write-Host "`n[DONE] 构建完成。安装后首次启动出现加载页 → 主界面即正常。" -ForegroundColor Green
