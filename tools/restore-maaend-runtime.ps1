[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Plan", "Restore")]
    [string]$Action,

    [Parameter(Mandatory = $true)]
    [string]$RuntimeRoot,

    [Parameter(Mandatory = $true)]
    [string]$StagingRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ManagedDirectories = @(
    "agent",
    "data",
    "locales",
    "maafw",
    "resource",
    "resource_adb",
    "resource_playcover",
    "resource_wlroots",
    "tasks"
)
$ManagedFiles = @(
    "MaaEnd.exe",
    "LICENSE",
    "README.md",
    "interface.json"
)

function Assert-ChildPath {
    param(
        [Parameter(Mandatory = $true)][string]$Parent,
        [Parameter(Mandatory = $true)][string]$Child,
        [Parameter(Mandatory = $true)][string]$Description
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
    param([Parameter(Mandatory = $true)][string]$Runtime)

    $KnownNames = @(
        "MaaEnd",
        "MaaEnd.mobile-profiler-api-v2.20",
        "MaaEnd.mobile-profiler-v2.20",
        "MXU",
        "go-service",
        "cpp-algo"
    )
    $Running = @(
        Get-Process -ErrorAction SilentlyContinue | Where-Object {
            $KnownNames -contains $_.ProcessName
        }
    )
    if ($Running) {
        $Details = ($Running | ForEach-Object {
            "$($_.ProcessName)#$($_.Id)"
        }) -join ", "
        throw "MaaEnd-related processes are running: $Details"
    }
}

function Assert-HardenedStaging {
    param([Parameter(Mandatory = $true)][string]$Staging)

    foreach ($Name in $ManagedDirectories) {
        $Path = Join-Path $Staging $Name
        if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
            throw "Staging directory is missing: $Path"
        }
        Assert-ChildPath $Staging $Path "Staging directory"
    }
    foreach ($Name in $ManagedFiles) {
        $Path = Join-Path $Staging $Name
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
            throw "Staging file is missing: $Path"
        }
        Assert-ChildPath $Staging $Path "Staging file"
    }

    try {
        $Interface = Get-Content -LiteralPath (
            Join-Path $Staging "interface.json"
        ) -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw "Staging interface.json is not valid JSON"
    }
    if ($Interface.name -ne "MaaEnd" -or $Interface.version -ne "v2.20.0") {
        throw "Staging must remain pinned to MaaEnd v2.20.0"
    }
    $MirrorProperty = $Interface.PSObject.Properties["mirrorchyan_rid"]
    if ($null -ne $MirrorProperty -and $MirrorProperty.Value) {
        throw "Staging must disable automatic updates by clearing mirrorchyan_rid"
    }
}

function Get-ManagedInventory {
    param([Parameter(Mandatory = $true)][string]$Root)

    $NormalizedRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $Rows = @()
    foreach ($Name in $ManagedDirectories) {
        $Directory = Join-Path $NormalizedRoot $Name
        if (-not (Test-Path -LiteralPath $Directory -PathType Container)) {
            throw "Managed directory is missing: $Directory"
        }
        foreach ($File in Get-ChildItem -LiteralPath $Directory -Recurse -File) {
            $FullName = [System.IO.Path]::GetFullPath($File.FullName)
            Assert-ChildPath $NormalizedRoot $FullName "Managed inventory file"
            $Rows += [pscustomobject]@{
                path = $FullName.Substring($NormalizedRoot.Length + 1).Replace("\", "/")
                size = $File.Length
                last_write_utc = $File.LastWriteTimeUtc.ToString("o")
                sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $FullName).Hash
            }
        }
    }
    foreach ($Name in $ManagedFiles) {
        $Path = Join-Path $NormalizedRoot $Name
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
            throw "Managed file is missing: $Path"
        }
        $File = Get-Item -LiteralPath $Path
        $Rows += [pscustomobject]@{
            path = $Name
            size = $File.Length
            last_write_utc = $File.LastWriteTimeUtc.ToString("o")
            sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash
        }
    }
    return @($Rows | Sort-Object path)
}

function Get-InventoryIdentity {
    param([Parameter(Mandatory = $true)][object[]]$Inventory)

    return @($Inventory | ForEach-Object {
        "$($_.path)|$($_.size)|$($_.sha256)"
    })
}

function Assert-InventoryEqual {
    param(
        [Parameter(Mandatory = $true)][object[]]$Expected,
        [Parameter(Mandatory = $true)][object[]]$Actual,
        [Parameter(Mandatory = $true)][string]$Description
    )

    $Difference = @(
        Compare-Object -ReferenceObject (Get-InventoryIdentity $Expected) -DifferenceObject (Get-InventoryIdentity $Actual)
    )
    if ($Difference) {
        $Preview = ($Difference | Select-Object -First 8 | ForEach-Object {
            "$($_.SideIndicator) $($_.InputObject)"
        }) -join "; "
        throw "$Description inventory differs: $Preview"
    }
}

function Write-JsonFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][object]$Value
    )

    $Value | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $Path -Encoding UTF8
}

$Runtime = (Resolve-Path -LiteralPath $RuntimeRoot).Path
$Staging = (Resolve-Path -LiteralPath $StagingRoot).Path
if ($Runtime -eq $Staging) {
    throw "Runtime and staging roots must differ"
}
Assert-HardenedStaging $Staging
Assert-RuntimeIdle $Runtime

$RuntimeInventory = @(Get-ManagedInventory $Runtime)
$StagingInventory = @(Get-ManagedInventory $Staging)
$RuntimeIdentity = @(Get-InventoryIdentity $RuntimeInventory)
$StagingIdentity = @(Get-InventoryIdentity $StagingInventory)
$Diff = @(Compare-Object -ReferenceObject $RuntimeIdentity -DifferenceObject $StagingIdentity)

if ($Action -eq "Plan") {
    [pscustomobject]@{
        action = "Plan"
        runtime = $Runtime
        staging = $Staging
        runtime_file_count = $RuntimeInventory.Count
        staging_file_count = $StagingInventory.Count
        differing_inventory_rows = $Diff.Count
        runtime_only_or_changed = @($Diff | Where-Object SideIndicator -eq "<=").Count
        staging_only_or_changed = @($Diff | Where-Object SideIndicator -eq "=>").Count
        webview_cache_present = Test-Path -LiteralPath (
            Join-Path $Runtime "cache\webview_data"
        ) -PathType Container
    } | ConvertTo-Json -Depth 4
    exit 0
}

$BackupParent = [System.IO.Path]::GetFullPath(
    (Join-Path $Runtime "mobile-profiler-backups")
)
if (-not (Test-Path -LiteralPath $BackupParent -PathType Container)) {
    New-Item -ItemType Directory -Path $BackupParent -Force | Out-Null
}
$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
$EvidenceRoot = Join-Path $BackupParent "runtime-drift-$Timestamp"
$PrepareRoot = Join-Path $BackupParent ".restore-stage-$Timestamp"
Assert-ChildPath $BackupParent $EvidenceRoot "Runtime drift evidence directory"
Assert-ChildPath $BackupParent $PrepareRoot "Prepared restore directory"
if (Test-Path -LiteralPath $EvidenceRoot) {
    throw "Evidence directory already exists: $EvidenceRoot"
}
if (Test-Path -LiteralPath $PrepareRoot) {
    throw "Prepared restore directory already exists: $PrepareRoot"
}

New-Item -ItemType Directory -Path $EvidenceRoot | Out-Null
New-Item -ItemType Directory -Path $PrepareRoot | Out-Null
Write-JsonFile -Path (Join-Path $EvidenceRoot "preflight.json") -Value ([pscustomobject]@{
    schema_version = 1
    captured_at = (Get-Date).ToUniversalTime().ToString("o")
    action = "Restore"
    runtime = $Runtime
    staging = $Staging
    runtime_inventory = $RuntimeInventory
    staging_inventory = $StagingInventory
})

foreach ($Name in $ManagedDirectories) {
    $Source = Join-Path $Staging $Name
    $Destination = Join-Path $PrepareRoot $Name
    Assert-ChildPath $PrepareRoot $Destination "Prepared directory"
    Copy-Item -LiteralPath $Source -Destination $Destination -Recurse
}
foreach ($Name in $ManagedFiles) {
    $Source = Join-Path $Staging $Name
    $Destination = Join-Path $PrepareRoot $Name
    Assert-ChildPath $PrepareRoot $Destination "Prepared file"
    Copy-Item -LiteralPath $Source -Destination $Destination
}
$PreparedInventory = @(Get-ManagedInventory $PrepareRoot)
Assert-InventoryEqual $StagingInventory $PreparedInventory "Prepared v2.20.0 tree"

$EvidenceRuntime = Join-Path $EvidenceRoot "runtime"
$EvidenceCache = Join-Path $EvidenceRoot "cache"
New-Item -ItemType Directory -Path $EvidenceRuntime | Out-Null
New-Item -ItemType Directory -Path $EvidenceCache | Out-Null
$MovedOriginals = @()
$Installed = @()
$WebviewMoved = $false
$Succeeded = $false
try {
    foreach ($Name in @($ManagedDirectories) + @($ManagedFiles)) {
        $Source = Join-Path $Runtime $Name
        $Destination = Join-Path $EvidenceRuntime $Name
        Assert-ChildPath $Runtime $Source "Runtime managed path"
        Assert-ChildPath $EvidenceRuntime $Destination "Evidence managed path"
        Move-Item -LiteralPath $Source -Destination $Destination
        $MovedOriginals += $Name
    }

    $WebviewSource = Join-Path $Runtime "cache\webview_data"
    $WebviewDestination = Join-Path $EvidenceCache "webview_data"
    Assert-ChildPath $Runtime $WebviewSource "WebView cache"
    Assert-ChildPath $EvidenceCache $WebviewDestination "Evidence WebView cache"
    if (Test-Path -LiteralPath $WebviewSource -PathType Container) {
        Move-Item -LiteralPath $WebviewSource -Destination $WebviewDestination
        $WebviewMoved = $true
    }

    foreach ($Name in @($ManagedDirectories) + @($ManagedFiles)) {
        $Source = Join-Path $PrepareRoot $Name
        $Destination = Join-Path $Runtime $Name
        Assert-ChildPath $PrepareRoot $Source "Prepared managed path"
        Assert-ChildPath $Runtime $Destination "Restored runtime path"
        Move-Item -LiteralPath $Source -Destination $Destination
        $Installed += $Name
    }

    $RestoredInventory = @(Get-ManagedInventory $Runtime)
    Assert-InventoryEqual $StagingInventory $RestoredInventory "Restored runtime"
    $Succeeded = $true
}
catch {
    $Failure = $_
    $FailedNewRoot = Join-Path $EvidenceRoot "failed-new-runtime"
    New-Item -ItemType Directory -Path $FailedNewRoot -Force | Out-Null
    $InstalledReverse = @($Installed)
    [array]::Reverse($InstalledReverse)
    foreach ($Name in $InstalledReverse) {
        $Source = Join-Path $Runtime $Name
        $Destination = Join-Path $FailedNewRoot $Name
        if (Test-Path -LiteralPath $Source) {
            Assert-ChildPath $Runtime $Source "Failed restored path"
            Assert-ChildPath $FailedNewRoot $Destination "Failed runtime evidence path"
            Move-Item -LiteralPath $Source -Destination $Destination
        }
    }
    foreach ($Name in $MovedOriginals) {
        $Source = Join-Path $EvidenceRuntime $Name
        $Destination = Join-Path $Runtime $Name
        if ((Test-Path -LiteralPath $Source) -and -not (Test-Path -LiteralPath $Destination)) {
            Assert-ChildPath $EvidenceRuntime $Source "Rollback evidence path"
            Assert-ChildPath $Runtime $Destination "Rollback runtime path"
            Move-Item -LiteralPath $Source -Destination $Destination
        }
    }
    if ($WebviewMoved) {
        $WebviewSource = Join-Path $EvidenceCache "webview_data"
        $WebviewDestination = Join-Path $Runtime "cache\webview_data"
        if ((Test-Path -LiteralPath $WebviewSource) -and -not (Test-Path -LiteralPath $WebviewDestination)) {
            Move-Item -LiteralPath $WebviewSource -Destination $WebviewDestination
        }
    }
    Write-JsonFile -Path (Join-Path $EvidenceRoot "failure.json") -Value ([pscustomobject]@{
        schema_version = 1
        failed_at = (Get-Date).ToUniversalTime().ToString("o")
        error = $Failure.Exception.Message
        originals_restored = $MovedOriginals
        installed_before_failure = $Installed
    })
    throw $Failure
}

if (-not $Succeeded) {
    throw "MaaEnd runtime restore did not reach a verified terminal state"
}

$Result = [pscustomobject]@{
    schema_version = 1
    action = "Restore"
    restored_at = (Get-Date).ToUniversalTime().ToString("o")
    runtime = $Runtime
    staging = $Staging
    evidence_root = $EvidenceRoot
    restored_file_count = $StagingInventory.Count
    old_runtime_file_count = $RuntimeInventory.Count
    webview_cache_quarantined = $WebviewMoved
    preserved_paths = @(
        "config",
        "debug",
        "mobile-profiler-backups",
        "cache (except webview_data)",
        "MaaEnd.mobile-profiler-api-v2.20.exe",
        "MaaEnd.mobile-profiler-v2.20.exe"
    )
    verified = $true
}
Write-JsonFile -Path (Join-Path $EvidenceRoot "result.json") -Value $Result
if ((Get-ChildItem -LiteralPath $PrepareRoot -Force | Measure-Object).Count -eq 0) {
    Remove-Item -LiteralPath $PrepareRoot
}
$Result | ConvertTo-Json -Depth 5
