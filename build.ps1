# 把 DGLAB-CS2 打包成 Windows 執行檔（圖形介面版）
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File build.ps1              # 單一 exe（預設）
#   powershell -ExecutionPolicy Bypass -File build.ps1 -OneDir      # 資料夾版（啟動較快）
#   powershell -ExecutionPolicy Bypass -File build.ps1 -Console     # 保留主控台視窗（除錯用）
#
# 產出：
#   dist\DGLAB-CS2.exe                  （單一檔案模式）
#   dist\DGLAB-CS2\DGLAB-CS2.exe        （資料夾模式）
#   dist\...\config.toml                ← 放在 exe 旁邊，可以自由修改
#   dist\...\gamestate_integration_dglab.cfg
#
# 註：設定檔只是「附在旁邊」方便手動安裝；
#     執行檔本身會依 config.toml 的內容產生 cfg，也可以用介面上的
#     「安裝 GSI 設定」按鈕自動放進遊戲目錄。

[CmdletBinding()]
param(
    [switch]$OneDir,
    [switch]$Console,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# pip 與 PyInstaller 都會用到暫存目錄。有些受限環境的系統 temp 不能寫，
# 這裡統一改到專案目錄底下，順手也讓建置產物不會散得到處都是。
$buildTemp = Join-Path $root ".buildtmp"
New-Item -ItemType Directory -Force -Path $buildTemp | Out-Null
$env:TEMP = $buildTemp
$env:TMP = $buildTemp

Write-Host "==> 安裝相依套件" -ForegroundColor Cyan
& $Python -m pip install -r requirements.txt
& $Python -m pip install --upgrade "pyinstaller>=6.0"

$common = @(
    "--noconfirm",
    "--clean",
    "--name", "DGLAB-CS2",
    # websockets 的後端與傳輸實作是動態匯入的，必須明確收集
    "--collect-submodules", "websockets",
    "--hidden-import", "websockets.asyncio.client",
    "--hidden-import", "websockets.client",
    "--hidden-import", "websockets.legacy.client",
    # qrcode 的 image factory 也是動態載入
    "--collect-all", "qrcode",
    # 這個專案用不到、但常常被連帶掃進來的東西
    "--exclude-module", "lxml",
    "--exclude-module", "qrcode.tests",
    "--exclude-module", "numpy",
    "--exclude-module", "pandas",
    "--exclude-module", "matplotlib",
    "--exclude-module", "scipy",
    "--exclude-module", "pytest"
)

if ($Console) {
    Write-Host "==> 保留主控台視窗（除錯用）" -ForegroundColor Cyan
    $common += "--console"
} else {
    Write-Host "==> 不開主控台視窗（記錄寫到 dglab-cs2.log）" -ForegroundColor Cyan
    $common += "--windowed"
}

if ($OneDir) {
    Write-Host "==> 打包成資料夾" -ForegroundColor Cyan
    & $Python -m PyInstaller @common --onedir main_gui.py
    $outDir = Join-Path $root "dist\DGLAB-CS2"
} else {
    Write-Host "==> 打包成單一 exe" -ForegroundColor Cyan
    & $Python -m PyInstaller @common --onefile main_gui.py
    $outDir = Join-Path $root "dist"
}

# 設定檔與 cfg 放在 exe 旁邊，方便手動修改／手動安裝
Copy-Item (Join-Path $root "config.toml") $outDir -Force
Copy-Item (Join-Path $root "gamestate_integration_dglab.cfg") $outDir -Force

Write-Host ""
Write-Host "完成！輸出在 $outDir" -ForegroundColor Green
Write-Host "接下來：" -ForegroundColor Green
Write-Host "  1. 執行 DGLAB-CS2.exe（會開啟圖形介面）"
Write-Host "  2. 按介面上的「安裝 GSI 設定」把 cfg 放進遊戲目錄"
Write-Host "  3. 重新啟動 CS2，然後按「開始」並用 DG-LAB App 掃 QR code"
Write-Host "  4. 介面上的「? 使用教學」有完整步驟說明"
