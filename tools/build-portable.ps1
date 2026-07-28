[CmdletBinding()]
param(
    [string]$OutputDirectory = "",
    [string]$PythonVersion = "",
    [string]$PythonEmbedZip = "",
    [string]$AdbPath = "",
    [ValidateSet("Standard", "Full")]
    [string]$Edition = "Full",
    [switch]$SkipAdb
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$projectText = Get-Content -LiteralPath (Join-Path $repoRoot "pyproject.toml") -Raw
$versionMatch = [regex]::Match($projectText, '(?m)^version\s*=\s*"([^"]+)"\s*$')
if (-not $versionMatch.Success) {
    throw "Unable to read the Mobile Profiler version from pyproject.toml."
}
$ProjectVersion = $versionMatch.Groups[1].Value
if ($ProjectVersion -notmatch '^[0-9A-Za-z][0-9A-Za-z._-]*$') {
    throw "The Mobile Profiler version contains unsupported filename characters."
}
$editionSlug = $Edition.ToLowerInvariant()
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $repoRoot "dist\mobile-profiler-v$ProjectVersion-$editionSlug-portable"
}
$outputPath = [System.IO.Path]::GetFullPath($OutputDirectory)
$repoPath = [System.IO.Path]::GetFullPath($repoRoot)
if ($outputPath.TrimEnd('\') -eq $repoPath.TrimEnd('\')) {
    throw "OutputDirectory cannot be the repository root."
}

$builderPython = (Get-Command python -ErrorAction Stop).Source
$builderPythonVersion = (& $builderPython -c "import platform; print(platform.python_version())").Trim()
if (-not $PythonVersion) {
    $PythonVersion = $builderPythonVersion
}
if ($PythonVersion -notmatch '^\d+\.\d+\.\d+$') {
    throw "PythonVersion must use major.minor.patch, for example 3.13.7."
}
$targetPythonSeries = ([version]$PythonVersion).ToString(2)
$builderPythonSeries = ([version]$builderPythonVersion).ToString(2)
if ($targetPythonSeries -ne $builderPythonSeries) {
    throw "Portable dependency packaging requires builder Python $targetPythonSeries.x; current python is $builderPythonVersion."
}

$temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("mobile-profiler-portable-" + [Guid]::NewGuid().ToString("N"))
$stage = Join-Path $temporaryRoot "mobile-profiler-portable"
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

    Write-Host "Copying Mobile Profiler into the portable site-packages..."
    $portablePackage = Join-Path $sitePackages "mobile_profiler"
    Copy-Item -LiteralPath (Join-Path $repoRoot "src\mobile_profiler") -Destination $portablePackage -Recurse -Force

    $cacheDirectories = @(
        Get-ChildItem -LiteralPath $portablePackage -Directory -Filter "__pycache__" -Recurse -ErrorAction SilentlyContinue |
            Sort-Object -Property FullName -Descending
    )
    $portablePackagePrefix = [System.IO.Path]::GetFullPath($portablePackage).TrimEnd('\') + '\'
    foreach ($cacheDirectory in $cacheDirectories) {
        $cachePath = [System.IO.Path]::GetFullPath($cacheDirectory.FullName)
        if (-not $cachePath.StartsWith($portablePackagePrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove cache outside the staged package: $cachePath"
        }
        if (Test-Path -LiteralPath $cachePath) {
            Remove-Item -LiteralPath $cachePath -Recurse -Force
        }
    }
    Get-ChildItem -LiteralPath $portablePackage -File -Filter "*.pyc" -Recurse -ErrorAction SilentlyContinue |
        Remove-Item -Force

    $bundledExtras = @("uiautomator2")
    $portableRequirements = @("uiautomator2>=3.4,<4")
    $openSourceAutomation = $Edition -eq "Full"
    if ($openSourceAutomation) {
        $bundledExtras += "image"
        $portableRequirements += @("numpy>=1.26,<3", "opencv-python-headless>=4.10,<5")
    }
    $buildProfile = [ordered]@{
        schema_version = 1
        edition = $editionSlug
        portable = $true
        features = [ordered]@{
            open_source_automation = $openSourceAutomation
        }
        bundled_extras = $bundledExtras
        generated_at = [DateTime]::UtcNow.ToString("o")
    }
    $buildProfile | ConvertTo-Json -Depth 4 |
        Set-Content -LiteralPath (Join-Path $portablePackage "_build_profile.json") -Encoding utf8

    if (-not $openSourceAutomation) {
        Write-Host "Removing open-source automation modules from the Standard edition..."
        foreach ($pattern in @("open_source_automation.py", "maa_*.py", "maaend_*.py", "star_rail_*.py")) {
            Get-ChildItem -LiteralPath $portablePackage -File -Filter $pattern -ErrorAction SilentlyContinue |
                Remove-Item -Force
        }
        foreach ($name in @("maa_guard_policy.json", "maaend_guard_policy.json")) {
            $source = Join-Path $portablePackage $name
            if (Test-Path -LiteralPath $source) {
                Remove-Item -LiteralPath $source -Force
            }
        }
    }

    Write-Host "Installing portable Python dependencies for the $Edition edition..."
    $pipArguments = @(
        "-m", "pip", "install",
        "--disable-pip-version-check",
        "--no-compile",
        "--upgrade",
        "--target", $sitePackages
    ) + $portableRequirements
    & $builderPython @pipArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to install portable Python dependencies for the $Edition edition."
    }

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

    $adbBundled = $false
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
            $adbBundled = Test-Path -LiteralPath (Join-Path $portableAdb "adb.exe")
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
"%ROOT%python-runtime\python.exe" -m mobile_profiler %*
endlocal
'@
    Set-Content -LiteralPath (Join-Path $stage "profiler.cmd") -Value $launcher -Encoding ascii

    $uiLauncher = @"
@echo off
setlocal
set "ROOT=%~dp0"
cd /d "%ROOT%"
if exist "%ROOT%platform-tools\adb.exe" set "PATH=%ROOT%platform-tools;%PATH%"
if not exist "%ROOT%profiler-runs" mkdir "%ROOT%profiler-runs"
echo Mobile Profiler v$ProjectVersion
"%ROOT%python-runtime\python.exe" -m mobile_profiler ui --output-root "%ROOT%profiler-runs" %*
endlocal
"@
    Set-Content -LiteralPath (Join-Path $stage "start-ui.bat") -Value $uiLauncher -Encoding ascii

    $portableReadme = @"
Mobile Profiler Portable Bundle v$ProjectVersion
===============================================

Version: $ProjectVersion
Edition: $Edition
Open-source automation: $(if ($openSourceAutomation) { "included" } else { "not included" })
Bundled Python extras: $($bundledExtras -join ", ")

1. Extract the complete directory. Do not copy start-ui.bat by itself.
2. Double-click start-ui.bat to launch the local dashboard.
3. Command-line entry: profiler.cmd --help
4. Captures are stored under the local profiler-runs directory by default.
5. When platform-tools is bundled, the UI can run adb connect IP:PORT.
6. The Tools & Delivery page can import BTR2 logs, rebuild/recover reports,
   create evidence ZIPs, and compare two completed runs.
7. Full Chinese guide: docs\usage-zh.md

This bundle uses an independent Embedded Python runtime. The target computer
does not need Python, a virtual environment, or pip. Mobile Profiler is already
included under the bundled site-packages directory.

The Full edition includes OpenCV, NumPy, uiautomator2, and the Mobile Profiler
open-source automation adapters. Third-party game runtimes such as MAA,
StarRailCopilot, and MaaEnd are not redistributed; configure their directories
after launch. The Standard edition keeps profiling and AI automation but omits
the open-source automation page and its Python adapters.

Software rebuilding is intentionally disabled in a portable installation.
Make code changes in the complete source project, run its tests, and execute
build-portable.bat (or use the source UI Tools & Delivery page) to create a new
portable ZIP.
"@
    Set-Content -LiteralPath (Join-Path $stage "README-PORTABLE.txt") -Value $portableReadme -Encoding utf8
    Set-Content -LiteralPath (Join-Path $stage "VERSION.txt") -Value $ProjectVersion -Encoding ascii
    $buildManifest = [ordered]@{
        schema_version = 1
        project = "mobile-profiler"
        version = $ProjectVersion
        edition = $editionSlug
        python_version = $PythonVersion
        open_source_automation = $openSourceAutomation
        bundled_extras = $bundledExtras
        adb_bundled = $adbBundled
        generated_at = $buildProfile.generated_at
    }
    $buildManifest | ConvertTo-Json -Depth 4 |
        Set-Content -LiteralPath (Join-Path $stage "BUILD-MANIFEST.json") -Encoding utf8
    New-Item -ItemType Directory -Force -Path (Join-Path $stage "profiler-runs") | Out-Null

    Write-Host "Validating portable runtime..."
    & (Join-Path $runtime "python.exe") -B -m mobile_profiler --help | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Portable runtime validation failed."
    }
    $profileValidation = if ($openSourceAutomation) {
        "from mobile_profiler.build_profile import CURRENT_BUILD_PROFILE as p; assert p['edition'] == 'full'; assert p['features']['open_source_automation'] is True; import mobile_profiler.open_source_automation, cv2, numpy, uiautomator2"
    }
    else {
        "from importlib.util import find_spec; from mobile_profiler.build_profile import CURRENT_BUILD_PROFILE as p; assert p['edition'] == 'standard'; assert p['features']['open_source_automation'] is False; assert find_spec('mobile_profiler.open_source_automation') is None; import mobile_profiler.ui, uiautomator2"
    }
    & (Join-Path $runtime "python.exe") -B -c $profileValidation
    if ($LASTEXITCODE -ne 0) {
        throw "$Edition edition feature validation failed."
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
    Write-Host "Mobile Profiler:    v$ProjectVersion"
    Write-Host "Edition:            $Edition"
}
finally {
    if (Test-Path -LiteralPath $temporaryRoot) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
}
