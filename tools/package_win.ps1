# 版本只改仓库根目录的 VERSION。本脚本会把它写入 package.json 和 pyproject.toml。
# 在 Windows 虚拟机里、仓库根目录执行：
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\package_win.ps1
#
# 只用同一个 x64 Python。虚拟机是 ARM 时，也必须是仿真运行的 AMD64 Python 和 x64 Node。
# 不上传、不跑 release.sh、不删除用户数据。

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$env:PYTHONUTF8 = "1"
$env:CI = "true"
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"
$env:PLAYWRIGHT_DOWNLOAD_HOST = "https://npmmirror.com/mirrors/playwright"
$env:PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT = "600000"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

function Die([string]$Message) {
    Write-Error $Message
    exit 1
}

$VersionFile = Join-Path $Root "VERSION"
if (-not (Test-Path -LiteralPath $VersionFile)) { Die "找不到 VERSION" }
$Version = (Get-Content -LiteralPath $VersionFile -Raw).Trim()
if ($Version -notmatch '^\d+\.\d+\.\d+$') { Die "VERSION 必须是 x.y.z，当前是 $Version" }
Write-Output ("VERSION=" + $Version)

$Utf8 = New-Object System.Text.UTF8Encoding $false
$PackageJson = Join-Path $Root "frontend\package.json"
$PackageText = Get-Content -LiteralPath $PackageJson -Raw
$PackageText = [regex]::Replace($PackageText, '(?m)^  "version": ".*"', "  `"version`": `"$Version`"", 1)
[System.IO.File]::WriteAllText($PackageJson, $PackageText, $Utf8)

$ProjectFile = Join-Path $Root "pyproject.toml"
$ProjectText = Get-Content -LiteralPath $ProjectFile -Raw
$ProjectText = [regex]::Replace($ProjectText, '(?m)^version = ".*"', "version = `"$Version`"", 1)
[System.IO.File]::WriteAllText($ProjectFile, $ProjectText, $Utf8)

Write-Output ("WIN_ARCH=" + $env:PROCESSOR_ARCHITECTURE)

$Python = $env:SPIDER_GD_PYTHON
if (-not $Python) {
    $candidates = @(
        "C:\build\spider-gd-venv-x64\Scripts\python.exe",
        "C:\Users\jayden\AppData\Local\Programs\Python\Python311\python.exe",
        "py"
    )
    foreach ($candidate in $candidates) {
        if ($candidate -eq "py") {
            $Python = "py"
            break
        }
        if (Test-Path -LiteralPath $candidate) {
            $Python = $candidate
            break
        }
    }
}
if (-not $Python) { Die "找不到 Python。设置 SPIDER_GD_PYTHON 为 64 位 python.org 安装的 python.exe。" }

$Venv = "C:\build\spider-gd-venv-x64\Scripts\python.exe"
if ($Python -ne $Venv) {
    if (-not (Test-Path -LiteralPath $Venv)) {
        & $Python -m venv "C:\build\spider-gd-venv-x64"
        if ($LASTEXITCODE -ne 0) { Die "创建虚拟环境失败" }
    }
}
$Py = $Venv

$ArchLine = & $Py -c "import platform,sys; print(platform.machine()); print(sys.executable); print(sys.version)"
Write-Output ($ArchLine -join "`n")
if ($ArchLine[0] -ne "AMD64") {
    Die "Python 不是 AMD64。不要用 ARM64 Python，也不要加 --allow-arm64。"
}

$Node = Get-Command node -ErrorAction SilentlyContinue
if (-not $Node) { Die "找不到 node。需要 x64 Node。" }
$NodeArch = & node -p "process.arch + ' ' + process.version"
Write-Output ("NODE " + $NodeArch)
if ($NodeArch -notlike "x64 *") { Die "Node 不是 x64。" }

$BuildId = (& git -C $Root rev-parse --short=12 HEAD).Trim()
if (-not $BuildId -or $BuildId.Length -ne 12) { Die "git rev-parse 没有给出 12 位提交号。" }
Write-Output ("BUILD_ID=" + $BuildId)

$BackendOut = Join-Path $Root "frontend\resources\backend"
if (Test-Path -LiteralPath $BackendOut) {
    Remove-Item -LiteralPath $BackendOut -Recurse -Force
}

Write-Output "PIP_BEGIN"
& $Py -m pip install -e ".[online,win,ocr]"
if ($LASTEXITCODE -ne 0) { Die "安装 .[online,win,ocr] 失败" }
& $Py -m pip install pyinstaller
if ($LASTEXITCODE -ne 0) { Die "安装 pyinstaller 失败" }

Write-Output "PLAYWRIGHT_BEGIN"
& $Py -m playwright install chromium
if ($LASTEXITCODE -ne 0) { Die "playwright install chromium 失败" }

$Log = Join-Path $Root "build\package_win.log"
New-Item -ItemType Directory -Force -Path (Split-Path $Log) | Out-Null
$RuntimeDll = "C:\build\vc-x64\MSVCP140_1.dll"
if (-not (Test-Path -LiteralPath $RuntimeDll)) {
    Die "没有 C:\build\vc-x64\MSVCP140_1.dll。请先放入官方 VC++ 2015-2022 x64 的这份库，不要用 ARM 虚拟机 System32 里的文件。"
}
$RuntimeArch = & $Py -c "import build_backend,pathlib; print(hex(build_backend.pe_machine_of(pathlib.Path(r'C:\build\vc-x64\MSVCP140_1.dll').read_bytes())))"
if ($RuntimeArch.Trim() -ne "0x8664") { Die "MSVCP140_1.dll 不是 x64：$RuntimeArch" }
Write-Output "PYINSTALLER_BEGIN"
& $Py build_backend.py --with-browsers > $Log 2>&1
$BuildExit = $LASTEXITCODE
Get-Content -LiteralPath $Log
if ($BuildExit -ne 0) { Die "build_backend.py --with-browsers 退出码是 $BuildExit。日志：$Log" }

$Browsers = Join-Path $BackendOut "playwright_browsers"
$ChromiumDirs = @(Get-ChildItem -LiteralPath $Browsers -Directory -Filter "chromium-*" -ErrorAction SilentlyContinue)
Write-Output ("BROWSER_DIRS=" + (($ChromiumDirs | ForEach-Object { $_.Name }) -join ","))
if ($ChromiumDirs.Count -ne 1) { Die "Playwright Chromium 目录必须恰好一个。" }
$Chrome = @(Get-ChildItem -LiteralPath $Browsers -Recurse -Filter "chrome.exe")
if ($Chrome.Count -lt 1) { Die "没有把 chrome.exe 打进后端目录。" }

$StampPath = Join-Path $BackendOut "build_stamp.json"
$Stamp = Get-Content -LiteralPath $StampPath -Raw
Write-Output ("STAMP=" + $Stamp.Trim())
if ($Stamp -notmatch [regex]::Escape($BuildId)) { Die "build_stamp.json 的 build_id 不是 $BuildId" }

$Exe = Join-Path $BackendOut "spider-gd-service.exe"
$SelfTest = & $Exe --self-test-ocr
Write-Output $SelfTest
if ($LASTEXITCODE -ne 0 -or ($SelfTest -join "`n") -notmatch "(?m)^ok ") {
    Die "安装前的 --self-test-ocr 没有成功"
}

Set-Location (Join-Path $Root "frontend")
Write-Output "PNPM_INSTALL_BEGIN"
pnpm install --frozen-lockfile
if ($LASTEXITCODE -ne 0) { Die "pnpm install --frozen-lockfile 失败" }
Write-Output "PNPM_BUILD_BEGIN"
pnpm build:win
if ($LASTEXITCODE -ne 0) { Die "pnpm build:win 失败" }

$VersionInPackage = (& node -p "require('./package.json').version").Trim()
if ($VersionInPackage -ne $Version) { Die "package.json 版本是 $VersionInPackage，不是 $Version" }
$Setup = Get-ChildItem -LiteralPath "dist" -Filter "*-$Version-setup.exe" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $Setup) { Die "没有找到版本 $Version 的 setup.exe" }
$Hash = Get-FileHash -LiteralPath $Setup.FullName -Algorithm SHA256
Write-Output ("SETUP=" + $Setup.FullName)
Write-Output ("SETUP_BYTES=" + $Setup.Length)
Write-Output ("SETUP_SHA256=" + $Hash.Hash)
Write-Output "PACKAGE_WIN_DONE"
