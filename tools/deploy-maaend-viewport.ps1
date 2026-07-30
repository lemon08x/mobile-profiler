[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Deploy", "Rollback")]
    [string]$Action,

    [Parameter(Mandatory = $true)]
    [string]$RuntimeRoot,

    [string]$FrameworkBin = "",

    [string]$AgentExecutable = "",

    [string]$CppAgentExecutable = "",

    [string]$MxuConfig = "",

    [string]$InterfaceFile = "",

    [string]$BackupRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Runtime = (Resolve-Path -LiteralPath $RuntimeRoot).Path
$BackupParent = [System.IO.Path]::GetFullPath(
    (Join-Path $Runtime "mobile-profiler-backups")
)

function Assert-ChildPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Parent,
        [Parameter(Mandatory = $true)]
        [string]$Child,
        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    $NormalizedParent = [System.IO.Path]::GetFullPath($Parent).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $NormalizedChild = [System.IO.Path]::GetFullPath($Child)
    $Prefix = $NormalizedParent + [System.IO.Path]::DirectorySeparatorChar
    if (-not $NormalizedChild.StartsWith(
        $Prefix,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "$Description escaped its allowed parent: $NormalizedChild"
    }
}

function Assert-RuntimeIdle {
    $ProcessNames = @(
        "MaaEnd",
        "MaaEnd.mobile-profiler-api-v2.20",
        "MXU",
        "go-service",
        "cpp-algo"
    )
    $Running = Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ProcessName -in $ProcessNames }
    if ($Running) {
        $Details = ($Running | ForEach-Object {
            "$($_.ProcessName)#$($_.Id)"
        }) -join ", "
        throw "MaaEnd-related processes are running: $Details"
    }
}

function Assert-File {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required file is missing: $Path"
    }
}

function Assert-HardenedInterface {
    param([Parameter(Mandatory = $true)][string]$Path)
    Assert-File $Path
    try {
        $Value = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 |
            ConvertFrom-Json
    }
    catch {
        throw "MaaEnd interface.json is not valid JSON: $Path"
    }
    if ($Value.name -ne "MaaEnd" -or $Value.version -ne "v2.20.0") {
        throw "MaaEnd interface.json must remain pinned to MaaEnd v2.20.0: $Path"
    }
    $MirrorProperty = $Value.PSObject.Properties["mirrorchyan_rid"]
    if ($null -ne $MirrorProperty -and $MirrorProperty.Value) {
        throw "Managed MaaEnd interface.json must disable automatic updates by clearing mirrorchyan_rid: $Path"
    }
}

function Copy-And-Verify {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    Assert-File $Source
    $ResolvedSource = (Resolve-Path -LiteralPath $Source).Path
    $ResolvedDestination = [System.IO.Path]::GetFullPath($Destination)
    $SamePath = $ResolvedSource.Equals(
        $ResolvedDestination,
        [System.StringComparison]::OrdinalIgnoreCase
    )
    if (-not $SamePath) {
        Copy-Item -LiteralPath $ResolvedSource -Destination $ResolvedDestination -Force
    }
    $SourceHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ResolvedSource).Hash
    $DestinationHash = (
        Get-FileHash -Algorithm SHA256 -LiteralPath $ResolvedDestination
    ).Hash
    if ($SourceHash -ne $DestinationHash) {
        throw "Deployment hash mismatch: $ResolvedDestination"
    }
    [pscustomobject]@{
        file = [System.IO.Path]::GetFileName($ResolvedDestination)
        destination = $ResolvedDestination
        sha256 = $DestinationHash
        verified = $true
        copied = -not $SamePath
    }
}

Assert-RuntimeIdle

$FrameworkNames = @(
    "MaaFramework.dll",
    "MaaAdbControlUnit.dll",
    "MaaUtils.dll",
    "MaaToolkit.dll",
    "MaaAgentClient.dll",
    "MaaAgentServer.dll"
)
$RuntimeFiles = @($FrameworkNames | ForEach-Object {
    Join-Path $Runtime "maafw\$_"
})
$RuntimeFiles += @(
    (Join-Path $Runtime "agent\go-service.exe"),
    (Join-Path $Runtime "agent\cpp-algo.exe"),
    (Join-Path $Runtime "config\mxu-MaaEnd.json"),
    (Join-Path $Runtime "interface.json")
)
foreach ($Path in $RuntimeFiles) {
    Assert-File $Path
}

if ($Action -eq "Deploy") {
    if (-not $FrameworkBin -or -not $AgentExecutable -or `
        -not $CppAgentExecutable -or -not $MxuConfig -or `
        -not $InterfaceFile) {
        throw "Deploy requires -FrameworkBin, -AgentExecutable, -CppAgentExecutable, -MxuConfig, and -InterfaceFile"
    }
    $Framework = (Resolve-Path -LiteralPath $FrameworkBin).Path
    $Agent = (Resolve-Path -LiteralPath $AgentExecutable).Path
    $CppAgent = (Resolve-Path -LiteralPath $CppAgentExecutable).Path
    $Config = (Resolve-Path -LiteralPath $MxuConfig).Path
    $Interface = (Resolve-Path -LiteralPath $InterfaceFile).Path
    foreach ($Name in $FrameworkNames) {
        Assert-File (Join-Path $Framework $Name)
    }
    Assert-File $Agent
    Assert-File $CppAgent
    Assert-File $Config
    Assert-HardenedInterface $Interface

    $Timestamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
    $SelectedBackup = Join-Path $BackupParent $Timestamp
    Assert-ChildPath $BackupParent $SelectedBackup "Backup directory"
    foreach ($Directory in @("maafw", "agent", "config")) {
        New-Item -ItemType Directory -Path (
            Join-Path $SelectedBackup $Directory
        ) -Force | Out-Null
    }
    $BackupResults = @()
    foreach ($Name in $FrameworkNames) {
        $BackupResults += Copy-And-Verify `
            -Source (Join-Path $Runtime "maafw\$Name") `
            -Destination (Join-Path $SelectedBackup "maafw\$Name")
    }
    $BackupResults += Copy-And-Verify `
        -Source (Join-Path $Runtime "agent\go-service.exe") `
        -Destination (Join-Path $SelectedBackup "agent\go-service.exe")
    $BackupResults += Copy-And-Verify `
        -Source (Join-Path $Runtime "agent\cpp-algo.exe") `
        -Destination (Join-Path $SelectedBackup "agent\cpp-algo.exe")
    $BackupResults += Copy-And-Verify `
        -Source (Join-Path $Runtime "config\mxu-MaaEnd.json") `
        -Destination (Join-Path $SelectedBackup "config\mxu-MaaEnd.json")
    $BackupResults += Copy-And-Verify `
        -Source (Join-Path $Runtime "interface.json") `
        -Destination (Join-Path $SelectedBackup "interface.json")

    $Results = @()
    foreach ($Name in $FrameworkNames) {
        $Results += Copy-And-Verify `
            -Source (Join-Path $Framework $Name) `
            -Destination (Join-Path $Runtime "maafw\$Name")
    }
    $Results += Copy-And-Verify `
        -Source $Agent `
        -Destination (Join-Path $Runtime "agent\go-service.exe")
    $Results += Copy-And-Verify `
        -Source $CppAgent `
        -Destination (Join-Path $Runtime "agent\cpp-algo.exe")
    $Results += Copy-And-Verify `
        -Source $Config `
        -Destination (Join-Path $Runtime "config\mxu-MaaEnd.json")
    $Results += Copy-And-Verify `
        -Source $Interface `
        -Destination (Join-Path $Runtime "interface.json")
    Assert-HardenedInterface (Join-Path $Runtime "interface.json")
    [pscustomobject]@{
        action = "Deploy"
        runtime = $Runtime
        backup_root = $SelectedBackup
        backup_files = $BackupResults
        files = $Results
    } | ConvertTo-Json -Depth 5
    exit 0
}

if (-not $BackupRoot) {
    throw "Rollback requires an explicit -BackupRoot"
}
$SelectedBackup = (Resolve-Path -LiteralPath $BackupRoot).Path
Assert-ChildPath $BackupParent $SelectedBackup "Rollback backup"
$BackupFiles = @($FrameworkNames | ForEach-Object {
    Join-Path $SelectedBackup "maafw\$_"
})
$BackupFiles += @(
    (Join-Path $SelectedBackup "agent\go-service.exe"),
    (Join-Path $SelectedBackup "agent\cpp-algo.exe"),
    (Join-Path $SelectedBackup "config\mxu-MaaEnd.json"),
    (Join-Path $SelectedBackup "interface.json")
)
foreach ($Path in $BackupFiles) {
    Assert-File $Path
}
$Results = @()
foreach ($Name in $FrameworkNames) {
    $Results += Copy-And-Verify `
        -Source (Join-Path $SelectedBackup "maafw\$Name") `
        -Destination (Join-Path $Runtime "maafw\$Name")
}
$Results += Copy-And-Verify `
    -Source (Join-Path $SelectedBackup "agent\go-service.exe") `
    -Destination (Join-Path $Runtime "agent\go-service.exe")
$Results += Copy-And-Verify `
    -Source (Join-Path $SelectedBackup "agent\cpp-algo.exe") `
    -Destination (Join-Path $Runtime "agent\cpp-algo.exe")
$Results += Copy-And-Verify `
    -Source (Join-Path $SelectedBackup "config\mxu-MaaEnd.json") `
    -Destination (Join-Path $Runtime "config\mxu-MaaEnd.json")
$Results += Copy-And-Verify `
    -Source (Join-Path $SelectedBackup "interface.json") `
    -Destination (Join-Path $Runtime "interface.json")
[pscustomobject]@{
    action = "Rollback"
    runtime = $Runtime
    backup_root = $SelectedBackup
    files = $Results
} | ConvertTo-Json -Depth 5
