# 基础版本只改 VERSION。正式版同步源元数据；beta 版本只进入构建产物。
# 在 Windows 虚拟机里、仓库根目录执行：
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\package_win.ps1
#   测试版追加 -Channel beta -BetaNumber 1
#
# 只用同一个 x64 Python。虚拟机是 ARM 时，也必须是仿真运行的 AMD64 Python 和 x64 Node。
# 不上传、不跑 release.sh、不删除用户数据。

param(
    [ValidateSet("stable", "beta")][string]$Channel = "stable",
    [string]$BetaNumber = ""
)

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

function Invoke-Native([string]$Exe, [string[]]$Arguments, [string]$LogFile = "") {
    # 原生程序的 stderr 绝不能再进 PowerShell 错误流：PS 5.1 把重定向的 stderr 变成
    # NativeCommandError，配合 $ErrorActionPreference=Stop 会在打包中途终止
    # （PyInstaller / pip / pnpm 都把进度写到 stderr）。这里统一交给 cmd 合并重定向。
    $quoted = @($Arguments | ForEach-Object { '"' + ([string]$_).Replace('"', '\"') + '"' })
    $line = '"' + $Exe + '" ' + ($quoted -join ' ')
    if ($LogFile) {
        $line = $line + ' > "' + $LogFile + '" 2>&1'
    } else {
        $line = $line + ' 2>&1'
    }
    # Out-Host：子进程输出只显示，不进函数返回值，否则 $exit = Invoke-Native 会拿到数组。
    cmd /c $line | Out-Host
    return $LASTEXITCODE
}

function Invoke-Capture([string]$Exe, [string[]]$Arguments) {
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("package-win-" + [guid]::NewGuid().ToString("N") + ".log")
    $code = Invoke-Native $Exe $Arguments $tmp
    $text = ""
    if (Test-Path -LiteralPath $tmp) {
        $text = Get-Content -LiteralPath $tmp -Raw -Encoding UTF8
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
    return [pscustomobject]@{ Code = $code; Text = $text }
}

$VersionFile = Join-Path $Root "VERSION"
if (-not (Test-Path -LiteralPath $VersionFile)) { Die "找不到 VERSION" }
$Version = (Get-Content -LiteralPath $VersionFile -Raw).Trim()
if ($Version -notmatch '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$') { Die "VERSION 必须是 x.y.z，当前是 $Version" }
if ($Channel -eq "beta" -and $BetaNumber -notmatch '^[1-9][0-9]*$') { Die "beta 的 BetaNumber 必须是正整数" }
if ($Channel -eq "stable" -and $BetaNumber) { Die "stable 不能指定 BetaNumber" }
$Contract = Get-Content -LiteralPath (Join-Path $Root "src\spider_gd\release-channels.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$Identity = $Contract.$Channel
$ReleaseVersion = $Version
if ($Channel -eq "beta") { $ReleaseVersion = "$Version-beta.$BetaNumber" }
Write-Output ("CHANNEL=" + $Channel)
Write-Output ("VERSION=" + $ReleaseVersion)

$Utf8 = New-Object System.Text.UTF8Encoding $false
$PackageJson = Join-Path $Root "frontend\package.json"
# 正式版继续同步源版本；测试版版本只进入构建暂存/extraMetadata，不能污染 PEP 440 元数据。
if ($Channel -eq "stable") {
    $PackageText = Get-Content -LiteralPath $PackageJson -Raw -Encoding UTF8
    $PackageText = [regex]::Replace($PackageText, '(?m)^  "version": ".*"', "  `"version`": `"$Version`"", 1)
    [System.IO.File]::WriteAllText($PackageJson, $PackageText, $Utf8)

    $ProjectFile = Join-Path $Root "pyproject.toml"
    $ProjectText = Get-Content -LiteralPath $ProjectFile -Raw -Encoding UTF8
    $ProjectText = [regex]::Replace($ProjectText, '(?m)^version = ".*"', "version = `"$Version`"", 1)
    [System.IO.File]::WriteAllText($ProjectFile, $ProjectText, $Utf8)
}

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
        $venvExit = Invoke-Native $Python @("-m", "venv", "C:\build\spider-gd-venv-x64")
        if ($venvExit -ne 0) { Die "创建虚拟环境失败" }
    }
}
$Py = $Venv

$ArchProbe = Invoke-Capture $Py @("-c", "import platform,sys; print(platform.machine()); print(sys.executable); print(sys.version)")
Write-Output ($ArchProbe.Text.Trim())
$ArchLine = @($ArchProbe.Text -split "`r?`n" | Where-Object { $_.Trim() -ne "" })
if ($ArchProbe.Code -ne 0 -or $ArchLine.Count -lt 1 -or $ArchLine[0].Trim() -ne "AMD64") {
    Die "Python 不是 AMD64 或无法执行。不要用 ARM64 Python，也不要加 --allow-arm64。"
}

$Node = Get-Command node -ErrorAction SilentlyContinue
if (-not $Node) { Die "找不到 node。需要 x64 Node。" }
$NodeProbe = Invoke-Capture "node" @("-p", "process.arch + ' ' + process.version")
$NodeArch = $NodeProbe.Text.Trim()
Write-Output ("NODE " + $NodeArch)
if ($NodeProbe.Code -ne 0 -or $NodeArch -notlike "x64 *") { Die "Node 不是 x64。" }

$GitProbe = Invoke-Capture "git" @("-C", $Root, "rev-parse", "--short=12", "HEAD")
$BuildId = $GitProbe.Text.Trim()
if ($GitProbe.Code -ne 0 -or -not $BuildId -or $BuildId.Length -ne 12) { Die "git rev-parse 没有给出 12 位提交号。" }
Write-Output ("BUILD_ID=" + $BuildId)

$BackendOut = Join-Path $Root "frontend\resources\backend"
if (Test-Path -LiteralPath $BackendOut) {
    Remove-Item -LiteralPath $BackendOut -Recurse -Force
}

Write-Output "PIP_BEGIN"
$pipExit = Invoke-Native $Py @("-m", "pip", "install", "-e", ".[online,win,ocr]")
if ($pipExit -ne 0) { Die "安装 .[online,win,ocr] 失败" }
$pipExit = Invoke-Native $Py @("-m", "pip", "install", "pyinstaller")
if ($pipExit -ne 0) { Die "安装 pyinstaller 失败" }

Write-Output "PLAYWRIGHT_BEGIN"
$pwExit = Invoke-Native $Py @("-m", "playwright", "install", "chromium")
if ($pwExit -ne 0) { Die "playwright install chromium 失败" }

$Log = Join-Path $Root "build\package_win.log"
New-Item -ItemType Directory -Force -Path (Split-Path $Log) | Out-Null
$RuntimeDll = "C:\build\vc-x64\MSVCP140_1.dll"
if (-not (Test-Path -LiteralPath $RuntimeDll)) {
    Die "没有 C:\build\vc-x64\MSVCP140_1.dll。请先放入官方 VC++ 2015-2022 x64 的这份库，不要用 ARM 虚拟机 System32 里的文件。"
}
$RuntimeProbe = Invoke-Capture $Py @("-c", "import build_backend,pathlib; print(hex(build_backend.pe_machine_of(pathlib.Path(r'C:\build\vc-x64\MSVCP140_1.dll').read_bytes())))")
$RuntimeArch = $RuntimeProbe.Text.Trim()
if ($RuntimeProbe.Code -ne 0 -or $RuntimeArch -ne "0x8664") { Die "MSVCP140_1.dll 不是 x64：$RuntimeArch" }
Write-Output "PYINSTALLER_BEGIN"
$BackendArgs = @("--with-browsers", "--channel", $Channel)
if ($Channel -eq "beta") { $BackendArgs += @("--beta-number", $BetaNumber) }
$BuildExit = Invoke-Native $Py (@("build_backend.py") + $BackendArgs) $Log
Get-Content -LiteralPath $Log
if ($BuildExit -ne 0) { Die "build_backend.py --with-browsers 退出码是 $BuildExit。日志：$Log" }

$Browsers = Join-Path $BackendOut "playwright_browsers"
$ChromiumDirs = @(Get-ChildItem -LiteralPath $Browsers -Directory -Filter "chromium-*" -ErrorAction SilentlyContinue)
Write-Output ("BROWSER_DIRS=" + (($ChromiumDirs | ForEach-Object { $_.Name }) -join ","))
if ($ChromiumDirs.Count -ne 1) { Die "Playwright Chromium 目录必须恰好一个。" }
$Chrome = @(Get-ChildItem -LiteralPath $Browsers -Recurse -Filter "chrome.exe")
if ($Chrome.Count -lt 1) { Die "没有把 chrome.exe 打进后端目录。" }

$StampPath = Join-Path $BackendOut "build_stamp.json"
$Stamp = Get-Content -LiteralPath $StampPath -Raw -Encoding UTF8
Write-Output ("STAMP=" + $Stamp.Trim())
$StampInfo = $Stamp | ConvertFrom-Json
if ($StampInfo.build_id -ne $BuildId) { Die "build_stamp.json 的 build_id 不是 $BuildId" }
if ($StampInfo.channel -ne $Channel -or $StampInfo.version -ne $ReleaseVersion) { Die "后端渠道或版本不是本次选择" }

$Exe = Join-Path $BackendOut ($Identity.serviceName + ".exe")
$SelfProbe = Invoke-Capture $Exe @("--self-test-ocr")
Write-Output $SelfProbe.Text
if ($SelfProbe.Code -ne 0 -or $SelfProbe.Text -notmatch "(?m)^ok ") {
    Die "安装前的 --self-test-ocr 没有成功"
}

Set-Location (Join-Path $Root "frontend")
Write-Output "PNPM_INSTALL_BEGIN"
$pnpmExit = Invoke-Native "pnpm" @("install", "--frozen-lockfile")
if ($pnpmExit -ne 0) { Die "pnpm install --frozen-lockfile 失败" }
Write-Output "PNPM_BUILD_BEGIN"
$FrontendArgs = @("--channel", $Channel)
if ($Channel -eq "beta") { $FrontendArgs += @("--beta-number", $BetaNumber) }
$pnpmExit = Invoke-Native "pnpm" (@("build:win") + $FrontendArgs)
if ($pnpmExit -ne 0) {
    Write-Output "[warn] pnpm build:win 失败（exit=$pnpmExit），15 秒后重试一次（Windows 偶发文件占用）"
    Start-Sleep -Seconds 15
    $pnpmExit = Invoke-Native "pnpm" (@("build:win") + $FrontendArgs)
}
if ($pnpmExit -ne 0) { Die "pnpm build:win 失败" }

# beta 的版本号只进构建产物（extraMetadata），源 package.json 保持基线版本；
# 正式版才要求源元数据与 VERSION 同步。
if ($Channel -eq "stable") {
    $VersionProbe = Invoke-Capture "node" @("-p", "require('./package.json').version")
    $VersionInPackage = $VersionProbe.Text.Trim()
    if ($VersionProbe.Code -ne 0 -or $VersionInPackage -ne $Version) { Die "package.json 版本是 $VersionInPackage，不是 $Version" }
}
$SetupName = $Identity.productName + "-$ReleaseVersion-setup.exe"
$Setup = Get-Item -LiteralPath (Join-Path "dist" $SetupName) -ErrorAction SilentlyContinue
if (-not $Setup) { Die "没有找到本次渠道版本安装包 $SetupName" }
$Hash = Get-FileHash -LiteralPath $Setup.FullName -Algorithm SHA256
Write-Output ("SETUP=" + $Setup.FullName)
Write-Output ("SETUP_BYTES=" + $Setup.Length)
Write-Output ("SETUP_SHA256=" + $Hash.Hash)
Write-Output "PACKAGE_WIN_DONE"
