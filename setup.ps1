<#
.SYNOPSIS
    LoL Highlights 一鍵安裝（Windows 10/11 x64 + NVIDIA GPU）

.DESCRIPTION
    git clone 之後執行本腳本即可：
        .\setup.ps1

    腳本會：回答 4 個問題 -> 跳一次 UAC -> 自動安裝 Miniconda / lol-env / ffmpeg / MySQL /
    YOLO 模型 -> 產生 .env -> 建表 -> 跑環境健診。每一步都先檢查「已經做過了嗎」，失敗可直接重跑。

    非互動（CI / 全用預設值）：
        .\setup.ps1 -NonInteractive

.PARAMETER NonInteractive
    不問問題，全部用預設值或參數值。找不到 GPU 時自動略過 GPU 檢查。
.PARAMETER VideoDir / OutputDir
    影片輸入根 / 剪輯輸出根。預設 <專案>\videos、<專案>\output。
.PARAMETER MysqlDataDir
    MySQL 資料目錄（主程式固定裝在 C:\Program Files\MySQL\MySQL Server 8.0）。預設 <專案>\data\mysql。
.PARAMETER YoutubeApiKey
    YouTube Data API v3 key，可空。
.PARAMETER EnableLpl
    要看 LPL（Bilibili）。只影響印出的說明，不改設定。
.PARAMETER InstallMiniconda
    就算機器上已有 conda 也強制裝一份新的 Miniconda 到 %USERPROFILE%\miniconda3（CI 用）。
.PARAMETER SkipGpuCheck
    環境健診略過 CUDA / YOLO GPU 兩項（沒顯卡的機器用）。
#>
[CmdletBinding()]
param(
    [switch]$NonInteractive,
    [string]$VideoDir = "",
    [string]$OutputDir = "",
    [string]$MysqlDataDir = "",
    [string]$YoutubeApiKey = "",
    [switch]$EnableLpl,
    [switch]$InstallMiniconda,
    [switch]$SkipGpuCheck,
    [string]$ModelsReleaseTag = "models-v1",
    [switch]$Elevated   # 內部用：代表已經是提權後的第二次執行
)

$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

# ── 常數 ──────────────────────────────────────────────────────────────────────
$ProjectRoot     = $PSScriptRoot
$LogPath         = Join-Path $ProjectRoot "deployment\setup_log.txt"
$DownloadDir     = Join-Path $ProjectRoot "_tmp\setup_downloads"
$GithubRepo      = "RoyalMilkteaMaster/lol-highlights"

$MinicondaUrl    = "https://repo.anaconda.com/miniconda/Miniconda3-latest-Windows-x86_64.exe"
$FfmpegUrl       = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
$MysqlVersion    = "8.0.46"
$MysqlUrl        = "https://cdn.mysql.com/Downloads/MySQL-8.0/mysql-$MysqlVersion-winx64.zip"
$MysqlMd5        = "003f527d5df61b663ff191038cd676bd"
$MysqlBaseDir    = "C:\Program Files\MySQL\MySQL Server 8.0"
$MysqlService    = "MySQL80"
$MysqlDbName     = "lol_highlight"
$MysqlAppUser    = "lol_app"
$RootPasswordFile = Join-Path $ProjectRoot "deployment\mysql_root_password.txt"

$ModelsDir       = Join-Path $ProjectRoot "highlight\assets\yolo_models"
$ShaSumsFile     = Join-Path $ModelsDir "SHA256SUMS"
$FfmpegDir       = Join-Path $ProjectRoot "tools\ffmpeg"
$EnvFile         = Join-Path $ProjectRoot ".env"

# ── 輸出 / log ────────────────────────────────────────────────────────────────
function Write-Log([string]$Text, [string]$Color = "Gray") {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $Text
    Write-Host $line -ForegroundColor $Color
    Add-Content -Path $LogPath -Value $line -Encoding UTF8
}
function Step([string]$Text) { Write-Log "" ; Write-Log ("==== " + $Text + " ====") "Cyan" }
function Ok([string]$Text)   { Write-Log ("  [OK] " + $Text) "Green" }
function Skip([string]$Text) { Write-Log ("  [SKIP] " + $Text) "DarkGray" }
function Warn([string]$Text) { Write-Log ("  [WARN] " + $Text) "Yellow" }
function Fail([string]$Text) { Write-Log ("  [FAIL] " + $Text) "Red"; throw $Text }

function Test-IsAdmin {
    ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function New-RandomPassword([int]$Length = 24) {
    # 只用英數，避免 .env / SQL / 命令列的跳脫問題
    $chars = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    $bytes = New-Object byte[] $Length
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })
}

function Invoke-Download([string]$Url, [string]$OutFile) {
    if (-not (Test-Path $DownloadDir)) { New-Item -ItemType Directory -Force -Path $DownloadDir | Out-Null }
    Write-Log ("  downloading " + $Url)
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
        & $curl.Source -L --fail --retry 3 --retry-delay 3 -sS -o $OutFile $Url
        if ($LASTEXITCODE -ne 0) { throw "download failed (curl exit $LASTEXITCODE): $Url" }
    } else {
        $ProgressPreference = "SilentlyContinue"
        Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing
    }
    if (-not (Test-Path $OutFile)) { throw "download produced no file: $Url" }
}

function Expand-Zip([string]$Zip, [string]$Dest) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    if (Test-Path $Dest) { Remove-Item $Dest -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $Dest | Out-Null
    [System.IO.Compression.ZipFile]::ExtractToDirectory($Zip, $Dest)
}

function Get-EnvValue([string]$Key) {
    if (-not (Test-Path $EnvFile)) { return "" }
    $line = Get-Content $EnvFile -Encoding UTF8 | Where-Object { $_ -match ("^\s*" + [regex]::Escape($Key) + "\s*=") } | Select-Object -First 1
    if (-not $line) { return "" }
    return ($line -split "=", 2)[1].Trim().Trim('"')
}

function Set-EnvValue([string]$Key, [string]$Value) {
    # 有這個 key 就改值，沒有就補在檔尾；不動其他行
    $lines = @()
    if (Test-Path $EnvFile) { $lines = @(Get-Content $EnvFile -Encoding UTF8) }
    $pattern = "^\s*" + [regex]::Escape($Key) + "\s*="
    $found = $false
    $lines = $lines | ForEach-Object {
        if ($_ -match $pattern) { $found = $true; "$Key=$Value" } else { $_ }
    }
    if (-not $found) { $lines += "$Key=$Value" }
    [System.IO.File]::WriteAllLines($EnvFile, [string[]]$lines, (New-Object System.Text.UTF8Encoding($false)))
}

function ToSlash([string]$P) { return $P.Replace("\", "/") }

# ═════════════════════════════════════════════════════════════════════════════
#  0. 問問題（在使用者自己的視窗問完，再提權一次）
# ═════════════════════════════════════════════════════════════════════════════
if (-not (Test-Path (Split-Path $LogPath))) { New-Item -ItemType Directory -Force -Path (Split-Path $LogPath) | Out-Null }
if (-not $Elevated) { "=== setup.ps1 started $(Get-Date) ===" | Out-File $LogPath -Encoding UTF8 }

if (-not $VideoDir)     { $VideoDir     = Join-Path $ProjectRoot "videos" }
if (-not $OutputDir)    { $OutputDir    = Join-Path $ProjectRoot "output" }
if (-not $MysqlDataDir) { $MysqlDataDir = Join-Path $ProjectRoot "data\mysql" }

if (-not $NonInteractive -and -not $Elevated) {
    Write-Host ""
    Write-Host "  LoL Highlights 一鍵安裝" -ForegroundColor Cyan
    Write-Host "  ------------------------------------------------------------"
    Write-Host "  接下來會問 4 個問題，直接按 Enter 就是用括號內的預設值。"
    Write-Host ""

    $a = Read-Host "  1) 影片輸入資料夾 [$VideoDir]"
    if ($a) { $VideoDir = $a }
    $a = Read-Host "  2) 剪輯輸出資料夾 [$OutputDir]"
    if ($a) { $OutputDir = $a }
    $a = Read-Host "  3) MySQL 資料目錄（主程式固定在 C:\Program Files\MySQL）[$MysqlDataDir]"
    if ($a) { $MysqlDataDir = $a }

    Write-Host ""
    Write-Host "  ------------------------------------------------------------" -ForegroundColor Yellow
    Write-Host "  接下來兩個問題跟「能不能自動接直播」有關，請看清楚：" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  沒有 YouTube API key 和 Bilibili 登入，系統【無法即時接直播自動錄影剪輯】。" -ForegroundColor Yellow
    Write-Host "  你只能自己下載好比賽影片、放進 videos\manual_inbox，再由系統剪精華。" -ForegroundColor Yellow
    Write-Host "  兩者都可以之後再補，不影響現在安裝。" -ForegroundColor Yellow
    Write-Host "  ------------------------------------------------------------" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  4a) YouTube Data API v3 key（LCK / LCP 直播用）"
    Write-Host "      取得：https://console.cloud.google.com/apis/credentials -> 建立憑證 -> API 金鑰"
    Write-Host "      沒有就直接 Enter，系統會改用 yt-dlp（較慢、較不穩）。"
    $a = Read-Host "      key"
    if ($a) { $YoutubeApiKey = $a.Trim() }

    Write-Host ""
    Write-Host "  4b) 要看 LPL（Bilibili）嗎？需要用 Firefox 登入 bilibili.com，系統會自動讀取登入狀態。"
    $a = Read-Host "      要 (y) / 不要 (Enter)"
    if ($a -match "^[Yy]") { $EnableLpl = $true }
    Write-Host ""
}

# ── 提權（只跳一次 UAC）───────────────────────────────────────────────────────
if (-not (Test-IsAdmin)) {
    if ($Elevated) { Fail "提權後仍不是管理員，請用「以系統管理員身分執行」開 PowerShell 再跑一次。" }
    Write-Host "  需要管理員權限（安裝 MySQL 服務、寫入 Program Files），請在 UAC 視窗按「是」。" -ForegroundColor Cyan
    $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"",
                 "-Elevated", "-NonInteractive",
                 "-VideoDir", "`"$VideoDir`"", "-OutputDir", "`"$OutputDir`"", "-MysqlDataDir", "`"$MysqlDataDir`"",
                 "-ModelsReleaseTag", $ModelsReleaseTag)
    if ($YoutubeApiKey)   { $argList += @("-YoutubeApiKey", "`"$YoutubeApiKey`"") }
    if ($EnableLpl)       { $argList += "-EnableLpl" }
    if ($InstallMiniconda){ $argList += "-InstallMiniconda" }
    if ($SkipGpuCheck)    { $argList += "-SkipGpuCheck" }
    try {
        $p = Start-Process -FilePath "powershell.exe" -ArgumentList $argList -Verb RunAs -Wait -PassThru
    } catch {
        Write-Host ""
        Write-Host "  已取消 UAC，安裝中止（什麼都還沒改）。重跑 .\setup.ps1 並在 UAC 視窗按「是」。" -ForegroundColor Yellow
        exit 2
    }
    Write-Host ""
    if ($p.ExitCode -eq 0) { Write-Host "  安裝完成。完整紀錄：deployment\setup_log.txt" -ForegroundColor Green }
    else { Write-Host "  安裝失敗（exit $($p.ExitCode)）。看 deployment\setup_log.txt 最後幾行，修正後重跑 .\setup.ps1 即可接續。" -ForegroundColor Red }
    exit $p.ExitCode
}

# ═════════════════════════════════════════════════════════════════════════════
#  以下全部在管理員權限下執行
# ═════════════════════════════════════════════════════════════════════════════
$exitCode = 0
try {
    Set-Location $ProjectRoot
    Write-Log ("project root : " + $ProjectRoot)
    Write-Log ("video dir    : " + $VideoDir)
    Write-Log ("output dir   : " + $OutputDir)
    Write-Log ("mysql datadir: " + $MysqlDataDir)

    # ── 1. 前置檢查 ───────────────────────────────────────────────────────────
    Step "1/9 前置檢查"
    if (-not [Environment]::Is64BitOperatingSystem) { Fail "需要 64 位元 Windows" }
    $osVer = [Environment]::OSVersion.Version
    if ($osVer.Major -lt 10) { Fail "需要 Windows 10 以上（目前 $osVer）" }
    Ok "Windows $osVer x64"

    $hasGpu = $false
    if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
        $gpuName = (& nvidia-smi --query-gpu=name --format=csv,noheader 2>$null | Select-Object -First 1)
        if ($gpuName) { $hasGpu = $true; Ok ("NVIDIA GPU: " + $gpuName) }
    }
    if (-not $hasGpu) {
        if ($NonInteractive -or $SkipGpuCheck) { $SkipGpuCheck = $true; Warn "找不到 nvidia-smi，GPU 檢查將略過（剪輯需要 NVIDIA 顯卡 + driver）" }
        else { Fail "找不到 nvidia-smi。請先裝 NVIDIA driver（https://www.nvidia.com/Download/index.aspx）再重跑。沒顯卡要硬跑可加 -SkipGpuCheck。" }
    }

    $drive = (Get-Item $ProjectRoot).PSDrive
    $freeGb = [math]::Round($drive.Free / 1GB, 1)
    if ($freeGb -lt 20) { Fail "磁碟 $($drive.Name): 只剩 $freeGb GB，至少要 20 GB（conda 環境約 8 GB + MySQL + ffmpeg + 模型）" }
    Ok "磁碟 $($drive.Name): 剩 $freeGb GB"

    try { Invoke-WebRequest -Uri "https://github.com" -Method Head -UseBasicParsing -TimeoutSec 15 | Out-Null; Ok "網路 OK" }
    catch { Fail "連不到 github.com，安裝需要網路" }

    # ── 2. Miniconda ──────────────────────────────────────────────────────────
    Step "2/9 conda"
    $condaRoot = $null
    if (-not $InstallMiniconda) {
        $candidates = @()
        if ($env:CONDA) { $candidates += $env:CONDA }
        $candidates += @("$env:USERPROFILE\anaconda3", "$env:USERPROFILE\miniconda3",
                         "$env:ProgramData\anaconda3", "$env:ProgramData\miniconda3",
                         "$env:LOCALAPPDATA\anaconda3", "C:\Miniconda", "C:\Miniconda3")
        $cmd = Get-Command conda -ErrorAction SilentlyContinue
        if ($cmd) { $candidates += (Split-Path (Split-Path $cmd.Source)) }
        foreach ($c in $candidates) {
            if ($c -and (Test-Path (Join-Path $c "Scripts\conda.exe"))) { $condaRoot = $c; break }
        }
    }
    if ($condaRoot) {
        Ok ("既有 conda: " + $condaRoot)
    } else {
        $condaRoot = Join-Path $env:USERPROFILE "miniconda3"
        if (Test-Path (Join-Path $condaRoot "Scripts\conda.exe")) {
            Ok ("既有 Miniconda: " + $condaRoot)
        } else {
            $inst = Join-Path $DownloadDir "Miniconda3-latest-Windows-x86_64.exe"
            if (-not (Test-Path $inst)) { Invoke-Download $MinicondaUrl $inst }
            Write-Log "  installing Miniconda (silent) -> $condaRoot"
            $p = Start-Process -FilePath $inst -ArgumentList @("/InstallationType=JustMe", "/AddToPath=0", "/RegisterPython=0", "/S", "/D=$condaRoot") -Wait -PassThru
            if ($p.ExitCode -ne 0 -or -not (Test-Path (Join-Path $condaRoot "Scripts\conda.exe"))) { Fail "Miniconda 安裝失敗 (exit $($p.ExitCode))" }
            Ok ("Miniconda 已裝到 " + $condaRoot)
        }
    }
    $conda = Join-Path $condaRoot "Scripts\conda.exe"
    # Anaconda 的 defaults channel 需要接受 ToS 才能非互動安裝（conda >= 25.x）
    $env:CONDA_PLUGINS_AUTO_ACCEPT_TOS = "yes"
    try {
        & $conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main --channel https://repo.anaconda.com/pkgs/r --channel https://repo.anaconda.com/pkgs/msys2 2>$null | Out-Null
        Write-Log "  (已接受 Anaconda defaults channel 的 Terms of Service: https://legal.anaconda.com/policies/en/)"
    } catch { }

    # ── 3. lol-env ────────────────────────────────────────────────────────────
    Step "3/9 lol-env（Python 3.10 + torch cu128 + 全部套件，約 3-5 GB，最慢的一步）"
    $envPython = Join-Path $condaRoot "envs\lol-env\python.exe"
    if (Test-Path $envPython) {
        Skip "lol-env 已存在（$envPython）。要更新請手動：conda env update -n lol-env -f deployment\environment.yml"
    } else {
        $yml = Join-Path $ProjectRoot "deployment\environment.yml"
        & $conda env create -f $yml
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $envPython)) { Fail "conda env create 失敗 (exit $LASTEXITCODE)。重跑 setup.ps1 會接續；若一直失敗請先 conda env remove -n lol-env" }
        Ok "lol-env 建好"
    }
    $pyVer = & $envPython -c "import sys; print(sys.version.split()[0])"
    Ok ("lol-env python " + $pyVer + " @ " + $envPython)

    # ── 4. ffmpeg ─────────────────────────────────────────────────────────────
    Step "4/9 ffmpeg / ffprobe"
    if ((Test-Path (Join-Path $FfmpegDir "ffmpeg.exe")) -and (Test-Path (Join-Path $FfmpegDir "ffprobe.exe"))) {
        Skip "tools\ffmpeg 已有 ffmpeg.exe + ffprobe.exe"
    } else {
        $zip = Join-Path $DownloadDir "ffmpeg-release-essentials.zip"
        if (-not (Test-Path $zip)) { Invoke-Download $FfmpegUrl $zip }
        $ex = Join-Path $DownloadDir "ffmpeg_extract"
        Expand-Zip $zip $ex
        $ff = Get-ChildItem $ex -Recurse -Filter "ffmpeg.exe" | Select-Object -First 1
        if (-not $ff) { Fail "解壓後找不到 ffmpeg.exe" }
        New-Item -ItemType Directory -Force -Path $FfmpegDir | Out-Null
        Copy-Item $ff.FullName (Join-Path $FfmpegDir "ffmpeg.exe") -Force
        Copy-Item (Join-Path $ff.DirectoryName "ffprobe.exe") (Join-Path $FfmpegDir "ffprobe.exe") -Force
        Ok "ffmpeg + ffprobe -> tools\ffmpeg"
    }
    $ffVer = (& (Join-Path $FfmpegDir "ffmpeg.exe") -version 2>$null | Select-Object -First 1)
    Ok $ffVer

    # ── 5. MySQL ──────────────────────────────────────────────────────────────
    Step "5/9 MySQL $MysqlVersion（程式 -> C:\Program Files，資料 -> $MysqlDataDir）"
    $mysqlBin = Join-Path $MysqlBaseDir "bin"
    $mysqlExe = Join-Path $mysqlBin "mysql.exe"
    $iniPath  = Join-Path $MysqlBaseDir "my.ini"

    $svc = Get-Service -Name $MysqlService -ErrorAction SilentlyContinue
    $freshInstall = $false
    if ($svc) {
        Skip "服務 $MysqlService 已存在（$($svc.Status)），不重裝"
        if ($svc.Status -ne "Running") { Start-Service $MysqlService; Ok "已啟動 $MysqlService" }
        if (-not (Test-Path $mysqlExe)) {
            # 服務存在但不在預設 basedir：從服務路徑反推
            $pathName = (Get-CimInstance Win32_Service -Filter "Name='$MysqlService'").PathName
            if ($pathName -match '^"?([^"]+\\bin)\\mysqld\.exe') { $mysqlBin = $Matches[1]; $mysqlExe = Join-Path $mysqlBin "mysql.exe" }
        }
    } else {
        if (Test-Path $MysqlBaseDir) { Fail "$MysqlBaseDir 已存在但沒有 $MysqlService 服務。請先移除該資料夾（或手動註冊服務）再重跑。" }
        if ((Test-Path $MysqlDataDir) -and ((Get-ChildItem $MysqlDataDir -Force | Measure-Object).Count -gt 0)) {
            Fail "$MysqlDataDir 已存在且不是空的。換一個資料目錄，或清空它再重跑。"
        }
        $freshInstall = $true
        $zip = Join-Path $DownloadDir "mysql-$MysqlVersion-winx64.zip"
        if (Test-Path $zip) {
            $md5 = (Get-FileHash $zip -Algorithm MD5).Hash.ToLower()
            if ($md5 -ne $MysqlMd5) { Remove-Item $zip -Force }
        }
        if (-not (Test-Path $zip)) { Invoke-Download $MysqlUrl $zip }
        $md5 = (Get-FileHash $zip -Algorithm MD5).Hash.ToLower()
        if ($md5 -ne $MysqlMd5) { Fail "MySQL zip MD5 不符（$md5），下載可能損毀，刪掉 _tmp\setup_downloads 後重跑" }
        Ok "MySQL zip MD5 驗證通過"

        $ex = Join-Path $DownloadDir "mysql_extract"
        Expand-Zip $zip $ex
        $src = Get-ChildItem $ex -Directory | Select-Object -First 1
        New-Item -ItemType Directory -Force -Path $MysqlBaseDir | Out-Null
        $rc = Start-Process -FilePath "robocopy.exe" -ArgumentList @("`"$($src.FullName)`"", "`"$MysqlBaseDir`"", "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/R:2", "/W:2") -Wait -PassThru -NoNewWindow
        if ($rc.ExitCode -ge 8 -or -not (Test-Path (Join-Path $mysqlBin "mysqld.exe"))) { Fail "複製 MySQL 程式檔失敗 (robocopy $($rc.ExitCode))" }
        Ok "程式檔 -> $MysqlBaseDir"

        $ini = @"
# MySQL $MysqlVersion - lol-highlights（由 setup.ps1 產生）
# 程式在 C:，資料在下面 datadir
[mysqld]
basedir=$(ToSlash $MysqlBaseDir)
datadir=$(ToSlash $MysqlDataDir)
port=3306
bind-address=127.0.0.1
character-set-server=utf8mb4
collation-server=utf8mb4_0900_ai_ci
default-storage-engine=INNODB
log-error=$(ToSlash $MysqlDataDir)/mysql-error.log
max_connections=151
default_authentication_plugin=caching_sha2_password

[client]
port=3306
default-character-set=utf8mb4
"@
        [System.IO.File]::WriteAllText($iniPath, $ini, (New-Object System.Text.UTF8Encoding($false)))
        New-Item -ItemType Directory -Force -Path $MysqlDataDir | Out-Null
        Ok "my.ini 寫好（datadir=$MysqlDataDir）"

        $p = Start-Process -FilePath (Join-Path $mysqlBin "mysqld.exe") -ArgumentList @("--defaults-file=`"$iniPath`"", "--initialize-insecure", "--console") -Wait -PassThru -NoNewWindow -RedirectStandardError (Join-Path $DownloadDir "mysql_init_stderr.txt") -RedirectStandardOutput (Join-Path $DownloadDir "mysql_init_stdout.txt")
        if ($p.ExitCode -ne 0 -or -not (Test-Path (Join-Path $MysqlDataDir "ibdata1"))) {
            Get-Content (Join-Path $DownloadDir "mysql_init_stderr.txt") -ErrorAction SilentlyContinue | ForEach-Object { Write-Log ("    " + $_) }
            Fail "mysqld --initialize 失敗 (exit $($p.ExitCode))"
        }
        Ok "資料目錄初始化完成"

        $p = Start-Process -FilePath (Join-Path $mysqlBin "mysqld.exe") -ArgumentList @("--install", $MysqlService, "--defaults-file=`"$iniPath`"") -Wait -PassThru -NoNewWindow
        if (-not (Get-Service -Name $MysqlService -ErrorAction SilentlyContinue)) { Fail "註冊服務 $MysqlService 失敗" }
        Set-Service -Name $MysqlService -StartupType Automatic
        Start-Service -Name $MysqlService
        $deadline = (Get-Date).AddSeconds(60)
        do { Start-Sleep -Seconds 2; $ok = (Test-NetConnection -ComputerName 127.0.0.1 -Port 3306 -WarningAction SilentlyContinue -InformationLevel Quiet) } while (-not $ok -and (Get-Date) -lt $deadline)
        if (-not $ok) { Fail "MySQL 服務啟動後 60 秒內 3306 沒開，看 $MysqlDataDir\mysql-error.log" }
        Ok "服務 $MysqlService 已啟動（開機自動）"

        $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
        if ($machinePath -notlike "*$mysqlBin*") {
            [Environment]::SetEnvironmentVariable("Path", ($machinePath.TrimEnd(';') + ";" + $mysqlBin), "Machine")
            Ok "已把 $mysqlBin 加進系統 PATH（新開的終端機才生效）"
        }
    }

    # ── 5b. 帳號 / 資料庫 ─────────────────────────────────────────────────────
    $dbUser = Get-EnvValue "MYSQL_USER"; if (-not $dbUser) { $dbUser = $MysqlAppUser }
    $dbPass = Get-EnvValue "MYSQL_PASSWORD"
    $dbOk = $false
    if ($dbPass) {
        & $mysqlExe -u $dbUser "-p$dbPass" -h 127.0.0.1 -e "SELECT 1" 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { $dbOk = $true; Skip ".env 內的 $dbUser 帳密可以連線，不重建帳號" }
    }
    if (-not $dbOk) {
        $rootArgs = @("-u", "root", "--skip-password")
        if (-not $freshInstall) {
            # 既有 MySQL：先試 root 無密碼，再試 mysql_root_password.txt
            & $mysqlExe @rootArgs -e "SELECT 1" 2>$null | Out-Null
            if ($LASTEXITCODE -ne 0 -and (Test-Path $RootPasswordFile)) {
                $rp = (Get-Content $RootPasswordFile -Raw).Trim()
                $rootArgs = @("-u", "root", "-p$rp")
                & $mysqlExe @rootArgs -e "SELECT 1" 2>$null | Out-Null
            }
            if ($LASTEXITCODE -ne 0) {
                if ($NonInteractive) { Fail "既有 MySQL 的 root 密碼未知，無法建立應用程式帳號。請手動建立帳號後把 MYSQL_USER / MYSQL_PASSWORD 寫進 .env 再重跑。" }
                $rp = Read-Host "  既有 MySQL 的 root 密碼（用來建立應用程式帳號）"
                $rootArgs = @("-u", "root", "-p$rp")
            }
        }
        $dbPass = New-RandomPassword
        $sql = @"
CREATE DATABASE IF NOT EXISTS ``$MysqlDbName`` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '$dbUser'@'localhost' IDENTIFIED BY '$dbPass';
CREATE USER IF NOT EXISTS '$dbUser'@'127.0.0.1' IDENTIFIED BY '$dbPass';
ALTER USER '$dbUser'@'localhost' IDENTIFIED BY '$dbPass';
ALTER USER '$dbUser'@'127.0.0.1' IDENTIFIED BY '$dbPass';
GRANT ALL PRIVILEGES ON ``$MysqlDbName``.* TO '$dbUser'@'localhost';
GRANT ALL PRIVILEGES ON ``$MysqlDbName``.* TO '$dbUser'@'127.0.0.1';
FLUSH PRIVILEGES;
"@
        $sql | & $mysqlExe @rootArgs --default-character-set=utf8mb4
        if ($LASTEXITCODE -ne 0) { Fail "建立資料庫 / 帳號失敗" }
        Ok "資料庫 $MysqlDbName + 帳號 $dbUser（隨機密碼，已寫入 .env）"

        if ($freshInstall) {
            $rootPass = New-RandomPassword
            "ALTER USER 'root'@'localhost' IDENTIFIED BY '$rootPass';" | & $mysqlExe -u root --skip-password
            if ($LASTEXITCODE -ne 0) { Warn "設定 root 密碼失敗，root 目前無密碼（只有本機能連）" }
            else {
                [System.IO.File]::WriteAllText($RootPasswordFile, $rootPass, (New-Object System.Text.UTF8Encoding($false)))
                Ok "root 密碼已隨機產生，存在 deployment\mysql_root_password.txt（已 gitignore）"
            }
        }
    }

    # ── 6. YOLO 模型 ──────────────────────────────────────────────────────────
    Step "6/9 YOLO 模型（GitHub Release: $ModelsReleaseTag）"
    if (-not (Test-Path $ShaSumsFile)) { Fail "找不到 $ShaSumsFile" }
    $sums = Get-Content $ShaSumsFile | Where-Object { $_ -match '^\s*([0-9a-fA-F]{64})\s+\*?(\S+)' } | ForEach-Object { @{ Hash = $Matches[1].ToLower(); File = $Matches[2] } }
    foreach ($m in $sums) {
        $dst = Join-Path $ModelsDir $m.File
        if ((Test-Path $dst) -and ((Get-FileHash $dst -Algorithm SHA256).Hash.ToLower() -eq $m.Hash)) { Skip ("已有 " + $m.File); continue }
        $url = "https://github.com/$GithubRepo/releases/download/$ModelsReleaseTag/$($m.File)"
        $downloaded = $false
        try { Invoke-Download $url $dst; $downloaded = $true } catch { Write-Log ("  direct download failed: " + $_.Exception.Message) }
        if (-not $downloaded -and (Get-Command gh -ErrorAction SilentlyContinue)) {
            # private repo：用 gh（需要 GH_TOKEN 或 gh auth login）
            & gh release download $ModelsReleaseTag -R $GithubRepo -p $m.File -D $ModelsDir --clobber
            if ($LASTEXITCODE -eq 0 -and (Test-Path $dst)) { $downloaded = $true }
        }
        if (-not $downloaded) { Fail ("無法下載模型 " + $m.File + "。若 repo 是 private，請設定 GH_TOKEN 或先 gh auth login。") }
        $actual = (Get-FileHash $dst -Algorithm SHA256).Hash.ToLower()
        if ($actual -ne $m.Hash) { Remove-Item $dst -Force; Fail ("模型 " + $m.File + " SHA-256 不符，已刪除，請重跑") }
        Ok ("下載並驗證 " + $m.File)
    }

    # ── 7. .env / config.yaml / 資料夾 ────────────────────────────────────────
    Step "7/9 設定檔與資料夾"
    if (-not (Test-Path $EnvFile)) { Copy-Item (Join-Path $ProjectRoot ".env.example") $EnvFile; Ok ".env 從 .env.example 建立" }
    else { Skip ".env 已存在，只補缺的值" }
    Set-EnvValue "MYSQL_HOST" "127.0.0.1"
    Set-EnvValue "MYSQL_PORT" "3306"
    Set-EnvValue "MYSQL_USER" $dbUser
    Set-EnvValue "MYSQL_PASSWORD" $dbPass
    Set-EnvValue "MYSQL_DB" $MysqlDbName
    if (-not (Get-EnvValue "VIDEO_DIR"))  { Set-EnvValue "VIDEO_DIR"  (ToSlash $VideoDir) }
    if (-not (Get-EnvValue "OUTPUT_DIR")) { Set-EnvValue "OUTPUT_DIR" (ToSlash $OutputDir) }
    if (-not (Get-EnvValue "FFMPEG_BIN")) { Set-EnvValue "FFMPEG_BIN" (ToSlash $FfmpegDir) }
    if ($YoutubeApiKey)                   { Set-EnvValue "YOUTUBE_API_KEY" $YoutubeApiKey }
    Set-EnvValue "LOL_ENV_PYTHON" $envPython
    Ok ".env 更新完成"

    foreach ($pair in @(@("automation\config.example.yaml", "automation\config.yaml"), @("highlight\config.example.yaml", "highlight\config.yaml"))) {
        $dst = Join-Path $ProjectRoot $pair[1]
        if (-not (Test-Path $dst)) { Copy-Item (Join-Path $ProjectRoot $pair[0]) $dst; Ok ($pair[1] + " 從範例建立") } else { Skip ($pair[1] + " 已存在") }
    }

    $vd = Get-EnvValue "VIDEO_DIR"; $od = Get-EnvValue "OUTPUT_DIR"
    foreach ($d in @("split", "scan", "live_recordings", "lol_games_vods", "lol_vods", "finals", "manual_inbox")) { New-Item -ItemType Directory -Force -Path (Join-Path $vd $d) | Out-Null }
    foreach ($d in @("raw", "final")) { New-Item -ItemType Directory -Force -Path (Join-Path $od $d) | Out-Null }
    New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot "highlight\assets\music") | Out-Null
    Ok "資料夾就緒（$vd, $od）"

    # ── 8. 建表 + migration ───────────────────────────────────────────────────
    Step "8/9 資料庫建表 + migration"
    $env:KMP_DUPLICATE_LIB_OK = "TRUE"; $env:PYTHONIOENCODING = "utf-8"
    & $envPython -m automation.db.init_db
    if ($LASTEXITCODE -ne 0) { Fail "init_db 失敗" }
    & $envPython -m automation.db.init_db --migrate
    if ($LASTEXITCODE -ne 0) { Fail "migrate 失敗" }
    Ok "schema + migrations 完成"

    # ── 9. 環境健診 ───────────────────────────────────────────────────────────
    Step "9/9 環境健診（check_env）"
    $checkArgs = @((Join-Path $ProjectRoot "deployment\_check_env_runner.py"))
    if ($SkipGpuCheck) { $checkArgs += "--skip-gpu" }
    & $envPython @checkArgs
    if ($LASTEXITCODE -ne 0) { Fail "環境健診有 FAIL，看上面" }

    # ── 完成 ──────────────────────────────────────────────────────────────────
    Write-Log ""
    Write-Log "============================================================" "Green"
    Write-Log "  安裝完成" "Green"
    Write-Log "============================================================" "Green"
    Write-Log ""
    Write-Log "  接下來："
    Write-Log "   - 手動剪一場：把比賽影片放進 $vd\manual_inbox，或直接跑"
    Write-Log "       `"$envPython`" -m highlight.main `"影片路徑.mp4`" --skip-split"
    Write-Log "   - 啟動自動化（排程 + 錄影 + 剪輯 + 儀表板）：.\start.bat"
    Write-Log "   - 儀表板：http://127.0.0.1:8765"
    Write-Log "   - 環境健診：deployment\check_env.bat"
    if (-not $YoutubeApiKey) { Write-Log "   - 之後要接 YouTube 直播：把 key 填進 .env 的 YOUTUBE_API_KEY" "Yellow" }
    if ($EnableLpl) { Write-Log "   - LPL：請安裝 Firefox 並用它登入 bilibili.com，系統會自動讀取登入 cookie（每週檢查一次是否過期）" "Yellow" }
    else { Write-Log "   - 之後要看 LPL：裝 Firefox、登入 bilibili.com 即可，不用改設定" }
    Write-Log "   - BGM：highlight\assets\music 只附一首參考曲，完整曲庫可用 music_urls.txt 自行下載（見 README）"
    Write-Log ""
}
catch {
    $exitCode = 1
    Write-Log ("!!! " + $_.Exception.Message) "Red"
    if ($_.InvocationInfo) { Write-Log ("!!! at line " + $_.InvocationInfo.ScriptLineNumber) "Red" }
    Write-Log "修正後重跑 .\setup.ps1，已完成的步驟會自動跳過。" "Yellow"
}

if ($Elevated -and -not $NonInteractive) { Read-Host "按 Enter 關閉" | Out-Null }
if ($Elevated) {
    # 提權視窗是獨立 console，停一下讓人看得到結果
    if ($exitCode -ne 0) { Start-Sleep -Seconds 3 }
}
exit $exitCode
