[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$FrameworkSource,

    [Parameter(Mandatory = $true)]
    [string]$MaaEndSource,

    [Parameter(Mandatory = $true)]
    [string]$GoBindingSource,

    [string]$FrameworkBuildDirectory = "",

    [string]$FrameworkInstallDirectory = "",

    [string]$AgentOutput = "",

    [string]$CppBuildDirectory = "",

    [string]$CppAgentOutput = "",

    [string]$GoExecutable = "",

    [switch]$SkipPatch
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ExpectedFrameworkCommit = "e6aa89259ff6907197becebdeb8efc0074f13dfc"
$ExpectedMaaEndCommit = "023e995a37898750d20052f40262c3f632348a76"
$ExpectedGoBindingCommit = "2b674ef2aeac62051c201945319802f14d5e5b3e"
$RepositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$FrameworkPatch = Join-Path $RepositoryRoot `
    "integrations\maaend\patches\maaframework-v5.12.1-viewport.patch"
$AgentPatch = Join-Path $RepositoryRoot `
    "integrations\maaend\patches\maaend-v2.20.0-viewport-gate.patch"
$GoBindingPatch = Join-Path $RepositoryRoot `
    "integrations\maaend\patches\maa-framework-go-v4.0.0-beta.17-viewport.patch"
$Framework = (Resolve-Path -LiteralPath $FrameworkSource).Path
$MaaEnd = (Resolve-Path -LiteralPath $MaaEndSource).Path
$GoBinding = (Resolve-Path -LiteralPath $GoBindingSource).Path
if ($GoExecutable) {
    # Launch-VsDevShell may change the current directory. Resolve caller-provided
    # relative paths before entering the developer shell so the Go tool remains
    # stable for the later test and build stages.
    $GoExecutable = (Resolve-Path -LiteralPath $GoExecutable).Path
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Command,
        [Parameter(Mandatory = $true)][string]$Description
    )
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

function Assert-GitBaseline {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Expected
    )
    if (-not (Test-Path -LiteralPath (Join-Path $Root ".git"))) {
        throw "Source must be a Git working tree: $Root"
    }
    $Actual = (& git -C $Root rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $Actual -ne $Expected) {
        throw "Expected source commit $Expected, got $Actual"
    }
}

function Apply-VerifiedPatch {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Patch
    )
    & git -C $Root apply --reverse --check $Patch 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Patch is already applied: $Patch"
        return
    }
    Invoke-Checked -Description "Patch validation" -Command {
        & git -C $Root apply --check $Patch
    }
    Invoke-Checked -Description "Patch application" -Command {
        & git -C $Root apply $Patch
    }
}

Assert-GitBaseline $Framework $ExpectedFrameworkCommit
Assert-GitBaseline $MaaEnd $ExpectedMaaEndCommit
Assert-GitBaseline $GoBinding $ExpectedGoBindingCommit
if (-not $SkipPatch) {
    Apply-VerifiedPatch $Framework $FrameworkPatch
    Apply-VerifiedPatch $MaaEnd $AgentPatch
    Apply-VerifiedPatch $GoBinding $GoBindingPatch
}

$IterationTool = Join-Path $RepositoryRoot "tools\maaend-iteration.py"
Invoke-Checked -Description "MaaEnd integration metadata validation" -Command {
    & python $IterationTool validate
}
Invoke-Checked -Description "MaaEnd source invariant audit" -Command {
    & python $IterationTool source-audit `
        --framework-source $Framework `
        --maaend-source $MaaEnd `
        --go-binding-source $GoBinding
}
Invoke-Checked -Description "MaaEnd patch drift check" -Command {
    & python $IterationTool patch-check `
        --framework-source $Framework `
        --maaend-source $MaaEnd `
        --go-binding-source $GoBinding
}

if (-not $FrameworkBuildDirectory) {
    $FrameworkBuildDirectory = Join-Path $Framework "build-adb-viewport"
}
else {
    $FrameworkBuildDirectory = [System.IO.Path]::GetFullPath(
        $FrameworkBuildDirectory
    )
}
if (-not $AgentOutput) {
    $AgentOutput = Join-Path $MaaEnd "build-mobile-profiler\go-service.exe"
}
else {
    $AgentOutput = [System.IO.Path]::GetFullPath($AgentOutput)
}
if (-not $FrameworkInstallDirectory) {
    $FrameworkInstallDirectory = Join-Path $MaaEnd "build-mobile-profiler\framework-install"
}
else {
    $FrameworkInstallDirectory = [System.IO.Path]::GetFullPath(
        $FrameworkInstallDirectory
    )
}
if (-not $CppBuildDirectory) {
    $CppBuildDirectory = Join-Path $MaaEnd "agent\cpp-algo\build-mobile-profiler"
}
else {
    $CppBuildDirectory = [System.IO.Path]::GetFullPath($CppBuildDirectory)
}
if (-not $CppAgentOutput) {
    $CppAgentOutput = Join-Path $CppBuildDirectory "bin\cpp-algo.exe"
}
else {
    $CppAgentOutput = [System.IO.Path]::GetFullPath($CppAgentOutput)
}

$VsWhere = Join-Path ${env:ProgramFiles(x86)} `
    "Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path -LiteralPath $VsWhere)) {
    throw "Visual Studio Installer vswhere.exe was not found"
}
$VsRoot = (& $VsWhere -latest -products * `
    -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
    -property installationPath).Trim()
if (-not $VsRoot) {
    throw "Visual Studio C++ Build Tools were not found"
}
$DevShell = Join-Path $VsRoot "Common7\Tools\Launch-VsDevShell.ps1"
$CMake = Join-Path $VsRoot `
    "Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
$CTest = Join-Path $VsRoot `
    "Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\ctest.exe"
$Ninja = Join-Path $VsRoot `
    "Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\ninja.exe"
foreach ($Tool in @($DevShell, $CMake, $CTest, $Ninja)) {
    if (-not (Test-Path -LiteralPath $Tool)) {
        throw "Required build tool was not found: $Tool"
    }
}

$MaaDeps = Join-Path $Framework "source\MaaUtils\MaaDeps\maadeps.cmake"
if (-not (Test-Path -LiteralPath $MaaDeps)) {
    throw "MaaFramework MaaDeps is missing; initialize MaaUtils and download maa-x64-windows dependencies"
}

& $DevShell -Arch amd64 -HostArch amd64 -SkipAutomaticLocation
if ($LASTEXITCODE -ne 0) {
    throw "Visual Studio developer shell initialization failed"
}
# Ninja recognizes and suppresses MSVC /showIncludes output only for the
# English prefix. Pinning VSLANG keeps localized hosts from emitting millions
# of dependency lines while preserving the same compiler and artifacts.
$env:VSLANG = "1033"
Invoke-Checked -Description "MaaFramework configuration" -Command {
    & $CMake `
        -S $Framework `
        -B $FrameworkBuildDirectory `
        -G Ninja `
        "-DCMAKE_MAKE_PROGRAM=$Ninja" `
        -DCMAKE_BUILD_TYPE=Release `
        -DMAADEPS_TRIPLET=maa-x64-windows `
        -DBUILD_PICLI=OFF `
        -DWITH_ADB_CONTROLLER=ON `
        -DWITH_CUSTOM_CONTROLLER=ON `
        -DWITH_MAA_AGENT=ON `
        -DBUILD_VIEWPORT_TESTING=ON `
        -DBUILD_SCREENCAP_FAILOVER_TESTING=ON
}
Invoke-Checked -Description "MaaFramework build" -Command {
    & $CMake --build $FrameworkBuildDirectory `
        --target MaaFramework MaaAdbControlUnit MaaUtils MaaToolkit `
            MaaAgentClient MaaAgentServer MaaCustomControlUnit `
            ViewportTransformTesting ViewportControllerTesting `
            ScreencapFailoverTesting `
        --parallel
}
Invoke-Checked -Description "MaaFramework isolated installation" -Command {
    & $CMake --install $FrameworkBuildDirectory `
        --prefix $FrameworkInstallDirectory
}
Invoke-Checked -Description "MaaFramework viewport and failover tests" -Command {
    & $CTest --test-dir $FrameworkBuildDirectory `
        -R "viewport::|screencap::" `
        --output-on-failure
}

if (-not $GoExecutable) {
    $GoCommand = Get-Command go -ErrorAction SilentlyContinue
    if ($GoCommand) {
        $GoExecutable = $GoCommand.Source
    }
}
if (-not $GoExecutable -or -not (Test-Path -LiteralPath $GoExecutable)) {
    throw "Go was not found; pass -GoExecutable with a Go 1.25-compatible executable"
}
$Go = (Resolve-Path -LiteralPath $GoExecutable).Path
$GoService = Join-Path $MaaEnd "agent\go-service"
$GoModDirectory = Join-Path $MaaEnd "build-mobile-profiler\go-mod"
New-Item -ItemType Directory -Path $GoModDirectory -Force | Out-Null
$GoModFile = Join-Path $GoModDirectory "maaend-viewport.mod"
$GoSumFile = Join-Path $GoModDirectory "maaend-viewport.sum"
$GoBindingModPath = $GoBinding.Replace("\", "/")
$GoModText = [System.IO.File]::ReadAllText((Join-Path $GoService "go.mod"))
$GoModText += "`nreplace github.com/MaaXYZ/maa-framework-go/v4 => $GoBindingModPath`n"
[System.IO.File]::WriteAllText(
    $GoModFile,
    $GoModText,
    [System.Text.UTF8Encoding]::new($false)
)
Copy-Item -LiteralPath (Join-Path $GoService "go.sum") `
    -Destination $GoSumFile -Force

$PreviousGoWork = $env:GOWORK
$env:GOWORK = "off"
try {
    Invoke-Checked -Description "MaaEnd aspect-ratio tests" -Command {
        & $Go -C $GoService test "-modfile=$GoModFile" `
            ./taskersink/aspectratio
    }
    Invoke-Checked -Description "MaaEnd Go Agent tests" -Command {
        & $Go -C $GoService test "-modfile=$GoModFile" ./...
    }
    $AgentOutputParent = Split-Path -Parent $AgentOutput
    New-Item -ItemType Directory -Path $AgentOutputParent -Force | Out-Null
    Invoke-Checked -Description "MaaEnd Go Agent build" -Command {
        & $Go -C $GoService build "-modfile=$GoModFile" `
            -trimpath -o $AgentOutput .
    }
}
finally {
    if ($null -eq $PreviousGoWork) {
        Remove-Item Env:GOWORK -ErrorAction SilentlyContinue
    }
    else {
        $env:GOWORK = $PreviousGoWork
    }
}

$CppSource = Join-Path $MaaEnd "agent\cpp-algo"
Invoke-Checked -Description "MaaEnd C++ Agent configuration" -Command {
    & $CMake `
        -S $CppSource `
        -B $CppBuildDirectory `
        -G Ninja `
        "-DCMAKE_MAKE_PROGRAM=$Ninja" `
        -DCMAKE_BUILD_TYPE=Release `
        -DMAADEPS_TRIPLET=maa-x64-windows `
        "-DDEPS_DIR=$FrameworkInstallDirectory"
}
# The upstream C++ target has generated and vendored headers whose dependency
# edges are not complete in every configuration.  An incremental build once
# linked objects compiled against different public controller layouts and
# crashed at Agent startup.  Always clean this isolated build directory before
# compiling the deployable agent so a public-header change cannot create a
# mixed ABI binary.
Invoke-Checked -Description "MaaEnd C++ Agent clean" -Command {
    & $CMake --build $CppBuildDirectory --target clean
}
Invoke-Checked -Description "MaaEnd C++ Agent build" -Command {
    & $CMake --build $CppBuildDirectory --target cpp-algo --parallel
}

$FrameworkBin = Join-Path $FrameworkBuildDirectory "bin"
$Outputs = @(
    (Join-Path $FrameworkBin "MaaFramework.dll"),
    (Join-Path $FrameworkBin "MaaAdbControlUnit.dll"),
    (Join-Path $FrameworkBin "MaaUtils.dll"),
    (Join-Path $FrameworkBin "MaaToolkit.dll"),
    (Join-Path $FrameworkBin "MaaAgentClient.dll"),
    (Join-Path $FrameworkBin "MaaAgentServer.dll"),
    $AgentOutput,
    $CppAgentOutput
)
foreach ($Output in $Outputs) {
    if (-not (Test-Path -LiteralPath $Output -PathType Leaf)) {
        throw "Build completed without required output: $Output"
    }
}
[pscustomobject]@{
    framework_head = $ExpectedFrameworkCommit
    maaend_head = $ExpectedMaaEndCommit
    go_binding_head = $ExpectedGoBindingCommit
    framework_bin = $FrameworkBin
    framework_install = $FrameworkInstallDirectory
    go_modfile = $GoModFile
    go_agent = $AgentOutput
    cpp_agent = $CppAgentOutput
    outputs = @($Outputs | ForEach-Object {
        [pscustomobject]@{
            path = $_
            sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $_).Hash
        }
    })
} | ConvertTo-Json -Depth 5
