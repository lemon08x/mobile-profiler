[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$MaaSource,

    [string]$BuildDirectory = "",

    [switch]$SkipDependencies,

    [switch]$SkipPatch
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ExpectedCommit = "2b44185c615d81bc39454933cd4649536c23f4f3"
$RepositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$PatchPath = Join-Path $RepositoryRoot "integrations\maa\patches\v6.14.2-viewport-transform.patch"
$MaaRoot = (Resolve-Path -LiteralPath $MaaSource).Path

if (-not (Test-Path -LiteralPath (Join-Path $MaaRoot ".git"))) {
    throw "MaaSource must be a Git working tree: $MaaRoot"
}

$ActualCommit = (& git -C $MaaRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $ActualCommit -ne $ExpectedCommit) {
    throw "Expected MAA v6.14.2 commit $ExpectedCommit, got $ActualCommit"
}

if (-not $BuildDirectory) {
    $BuildDirectory = Join-Path $MaaRoot "build-viewport"
}
else {
    $BuildDirectory = [System.IO.Path]::GetFullPath($BuildDirectory)
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [scriptblock]$Command,
        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

if (-not $SkipPatch) {
    & git -C $MaaRoot apply --reverse --check $PatchPath 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Viewport patch is already applied."
    }
    else {
        Invoke-Checked -Description "Viewport patch validation" -Command {
            & git -C $MaaRoot apply --check $PatchPath
        }
        Invoke-Checked -Description "Viewport patch application" -Command {
            & git -C $MaaRoot apply $PatchPath
        }
    }
}

$IterationTool = Join-Path $RepositoryRoot "tools\maa-iteration.py"
Invoke-Checked -Description "MAA integration metadata validation" -Command {
    & python $IterationTool validate
}
Invoke-Checked -Description "MAA viewport source invariant audit" -Command {
    & python $IterationTool source-audit --source-root $MaaRoot
}

$MaaUtilsCMake = Join-Path $MaaRoot "src\MaaUtils\MaaUtils.cmake"
if (-not (Test-Path -LiteralPath $MaaUtilsCMake)) {
    Invoke-Checked -Description "MaaUtils submodule initialization" -Command {
        & git -C $MaaRoot submodule update --init --depth 1 --recursive src/MaaUtils
    }
}

$MaaDepsCMake = Join-Path $MaaRoot "src\MaaUtils\MaaDeps\maadeps.cmake"
if (-not $SkipDependencies -and -not (Test-Path -LiteralPath $MaaDepsCMake)) {
    Invoke-Checked -Description "MaaDeps download" -Command {
        & python (Join-Path $MaaRoot "tools\maadeps-download.py") x64-windows
    }
}
if (-not (Test-Path -LiteralPath $MaaDepsCMake)) {
    throw "MaaDeps is missing. Run without -SkipDependencies or populate src\MaaUtils\MaaDeps."
}

$VsWhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path -LiteralPath $VsWhere)) {
    throw "Visual Studio Installer vswhere.exe was not found."
}
$VsRoot = (& $VsWhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath).Trim()
if (-not $VsRoot) {
    throw "Visual Studio C++ Build Tools were not found."
}

$DevShell = Join-Path $VsRoot "Common7\Tools\Launch-VsDevShell.ps1"
$CMake = Join-Path $VsRoot "Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
$CTest = Join-Path $VsRoot "Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\ctest.exe"
$Ninja = Join-Path $VsRoot "Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\ninja.exe"
foreach ($Tool in @($DevShell, $CMake, $CTest, $Ninja)) {
    if (-not (Test-Path -LiteralPath $Tool)) {
        throw "Required build tool was not found: $Tool"
    }
}

& $DevShell -Arch amd64 -HostArch amd64 -SkipAutomaticLocation
if ($LASTEXITCODE -ne 0) {
    throw "Visual Studio developer shell initialization failed."
}

$UnitBuild = "$BuildDirectory-unit-test"
Invoke-Checked -Description "Viewport unit-test configuration" -Command {
    & $CMake -S (Join-Path $MaaRoot "unit_test") -B $UnitBuild -G Ninja "-DCMAKE_MAKE_PROGRAM=$Ninja"
}
Invoke-Checked -Description "Viewport unit-test build" -Command {
    & $CMake --build $UnitBuild --target maa-viewport-transform-test --parallel
}
Invoke-Checked -Description "Viewport unit tests" -Command {
    & $CTest --test-dir $UnitBuild -R "viewport::" --output-on-failure
}

Invoke-Checked -Description "MaaCore configuration" -Command {
    & $CMake `
        -S $MaaRoot `
        -B $BuildDirectory `
        -G Ninja `
        "-DCMAKE_MAKE_PROGRAM=$Ninja" `
        -DCMAKE_BUILD_TYPE=Release `
        -DMAADEPS_TRIPLET=maa-x64-windows `
        -DBUILD_WPF_GUI=OFF `
        -DBUILD_DEBUG_DEMO=OFF `
        -DBUILD_RESOURCE_UPDATER=OFF `
        -DWITH_EMULATOR_EXTRAS=OFF `
        -DINSTALL_RESOURCE=OFF
}
Invoke-Checked -Description "MaaCore build" -Command {
    & $CMake --build $BuildDirectory --target MaaCore --parallel
}

$OutputDll = Join-Path $BuildDirectory "bin\MaaCore.dll"
if (-not (Test-Path -LiteralPath $OutputDll)) {
    throw "Build completed without MaaCore.dll: $OutputDll"
}
Write-Host "Patched MaaCore built successfully: $OutputDll"
