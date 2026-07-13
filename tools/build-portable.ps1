[CmdletBinding()]
param(
    [string]$OutputDirectory = "",
    [string]$PythonVersion = "",
    [string]$PythonEmbedZip = "",
    [string]$AdbPath = "",
    [switch]$SkipAdb
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $repoRoot "dist\mobile-power-profiler-portable"
}
$outputPath = [System.IO.Path]::GetFullPath($OutputDirectory)
$repoPath = [System.IO.Path]::GetFullPath($repoRoot)
if ($outputPath.TrimEnd('\') -eq $repoPath.TrimEnd('\')) {
    throw "OutputDirectory cannot be the repository root."
}

if (-not $PythonVersion) {
    $PythonVersion = (& python -c "import platform; print(platform.python_version())").Trim()
}
if ($PythonVersion -notmatch '^\d+\.\d+\.\d+$') {
    throw "PythonVersion must use major.minor.patch, for example 3.13.7."
}

$temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("mobile-power-profiler-portable-" + [Guid]::NewGuid().ToString("N"))
$stage = Join-Path $temporaryRoot "mobile-power-profiler-portable"
$runtime = Join-Path $stage "python-runtime"
$sitePackages = Join-Path $stage "site-packages"
New-Item -ItemType Directory -Force -Path $runtime, $sitePackages | Out-Null

try {
    if ($PythonEmbedZip) {
        $embedZip = (Resolve-Path -LiteralPath $PythonEmbedZip).Path
    }
    else {
        $architecture = if ([Environment]::Is64BitOperatingSystem) { "amd64" } else { "win32" }
        $embedZip = Join-Path $temporaryRoot "python-embed.zip"
        $url = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-$architecture.zip"
        Write-Host "Downloading Python embedded runtime $PythonVersion..."
        try {
            Invoke-WebRequest -Uri $url -OutFile $embedZip -UseBasicParsing
        }
        catch {
            throw "Unable to download $url. Download the official embeddable ZIP manually and rerun with -PythonEmbedZip. $($_.Exception.Message)"
        }
    }
    Expand-Archive -LiteralPath $embedZip -DestinationPath $runtime -Force

    Write-Host "Copying Mobile Power Profiler into the portable site-packages..."
    Copy-Item -LiteralPath (Join-Path $repoRoot "src\mobile_power_profiler") -Destination (Join-Path $sitePackages "mobile_power_profiler") -Recurse -Force

    $pth = Get-ChildItem -LiteralPath $runtime -Filter "python*._pth" | Select-Object -First 1
    if (-not $pth) {
        throw "The embedded Python archive does not contain python*._pth."
    }
    $pthLines = @(
        (Get-Content -LiteralPath $pth.FullName) |
            Where-Object { $_ -notmatch '^\s*#?\s*import site\s*$' -and $_ -notmatch '^\.\.\\site-packages\s*$' }
    )
    $pthLines += "..\site-packages"
    $pthLines += "import site"
    Set-Content -LiteralPath $pth.FullName -Value $pthLines -Encoding ascii

    foreach ($name in @("README.md", "pyproject.toml")) {
        Copy-Item -LiteralPath (Join-Path $repoRoot $name) -Destination $stage -Force
    }
    foreach ($name in @("docs", "examples")) {
        Copy-Item -LiteralPath (Join-Path $repoRoot $name) -Destination (Join-Path $stage $name) -Recurse -Force
    }

    if (-not $SkipAdb) {
        if (-not $AdbPath) {
            $adbCommand = Get-Command adb -ErrorAction SilentlyContinue
            if ($adbCommand) {
                $AdbPath = $adbCommand.Source
            }
        }
        if ($AdbPath) {
            $adbExecutable = (Resolve-Path -LiteralPath $AdbPath).Path
            $adbDirectory = Split-Path -Parent $adbExecutable
            $portableAdb = Join-Path $stage "platform-tools"
            New-Item -ItemType Directory -Force -Path $portableAdb | Out-Null
            foreach ($file in @("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll", "libwinpthread-1.dll", "NOTICE.txt", "source.properties")) {
                $source = Join-Path $adbDirectory $file
                if (Test-Path -LiteralPath $source) {
                    Copy-Item -LiteralPath $source -Destination $portableAdb -Force
                }
            }
            Write-Host "Bundled ADB from $adbDirectory"
        }
        else {
            Write-Warning "ADB was not found. The target computer must provide adb on PATH."
        }
    }

    $launcher = @'
@echo off
setlocal
set "ROOT=%~dp0"
if exist "%ROOT%platform-tools\adb.exe" set "PATH=%ROOT%platform-tools;%PATH%"
"%ROOT%python-runtime\python.exe" -m mobile_power_profiler %*
endlocal
'@
    Set-Content -LiteralPath (Join-Path $stage "profiler.cmd") -Value $launcher -Encoding ascii

    $uiLauncher = @'
@echo off
setlocal
set "ROOT=%~dp0"
cd /d "%ROOT%"
if exist "%ROOT%platform-tools\adb.exe" set "PATH=%ROOT%platform-tools;%PATH%"
if not exist "%ROOT%power-runs" mkdir "%ROOT%power-runs"
"%ROOT%python-runtime\python.exe" -m mobile_power_profiler ui --output-root "%ROOT%power-runs" %*
endlocal
'@
    Set-Content -LiteralPath (Join-Path $stage "start-ui.bat") -Value $uiLauncher -Encoding ascii

    $portableReadme = @'
Mobile Power Profiler Portable Bundle
======================================

1. Extract the complete directory. Do not copy start-ui.bat by itself.
2. Double-click start-ui.bat to launch the local dashboard.
3. Command-line entry: profiler.cmd --help
4. Captures are stored under the local power-runs directory by default.
5. When platform-tools is bundled, the UI can run adb connect IP:PORT.
6. The Tools & Delivery page can import BTR2 logs, rebuild/recover reports,
   create evidence ZIPs, and compare two completed runs.
7. Full Chinese guide: docs\usage-zh.md

This bundle uses an independent Embedded Python runtime. The target computer
does not need Python, a virtual environment, or pip. mobile-power-profiler is
already installed under the bundled site-packages directory.

Software rebuilding is intentionally disabled in a portable installation.
Make code changes in the complete source project, run its tests, and execute
build-portable.bat (or use the source UI Tools & Delivery page) to create a new
portable ZIP.
'@
    Set-Content -LiteralPath (Join-Path $stage "README-PORTABLE.txt") -Value $portableReadme -Encoding utf8
    New-Item -ItemType Directory -Force -Path (Join-Path $stage "power-runs") | Out-Null

    Write-Host "Validating portable runtime..."
    & (Join-Path $runtime "python.exe") -m mobile_power_profiler --help | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Portable runtime validation failed."
    }

    if (Test-Path -LiteralPath $outputPath) {
        Remove-Item -LiteralPath $outputPath -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $outputPath) | Out-Null
    Move-Item -LiteralPath $stage -Destination $outputPath

    $zipPath = "$outputPath.zip"
    if (Test-Path -LiteralPath $zipPath) {
        Remove-Item -LiteralPath $zipPath -Force
    }
    Compress-Archive -LiteralPath $outputPath -DestinationPath $zipPath -CompressionLevel Optimal
    Write-Host "Portable directory: $outputPath"
    Write-Host "Portable ZIP:       $zipPath"
}
finally {
    if (Test-Path -LiteralPath $temporaryRoot) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
}
