[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ReviewedCommit,
    [Parameter(Mandatory = $true)]
    [string]$OutputPath
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$ReleaseCommit = "43234a8c0b6371fdfc62b59fa75a7980924ac3a5"

function Assert-NativeSuccess([string]$Description) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed."
    }
}

if ($ReviewedCommit -cnotmatch '^[0-9a-f]{40}$') {
    throw "ReviewedCommit must be exactly 40 lowercase hexadecimal characters."
}
$TopLevel = (& git -C $RepoRoot rev-parse --show-toplevel 2>$null | Out-String).Trim()
Assert-NativeSuccess "Git repository discovery"
if ([IO.Path]::GetFullPath($TopLevel) -ne [IO.Path]::GetFullPath($RepoRoot)) {
    throw "Run this helper from its original repository checkout."
}
$HeadCommit = (& git -C $RepoRoot rev-parse HEAD 2>$null | Out-String).Trim()
Assert-NativeSuccess "Git commit verification"
if ($HeadCommit -cne $ReviewedCommit) {
    throw "The local checkout does not match the reviewed commit."
}
$Dirty = (& git -C $RepoRoot status --porcelain=v1 --untracked-files=all --ignored=matching |
    Out-String).Trim()
Assert-NativeSuccess "Git cleanliness verification"
if ($Dirty) {
    throw "The local checkout contains tracked, untracked, or ignored files."
}

$ResolvedOutput = [IO.Path]::GetFullPath($OutputPath)
$ResolvedRoot = [IO.Path]::GetFullPath($RepoRoot).TrimEnd([IO.Path]::DirectorySeparatorChar) +
    [IO.Path]::DirectorySeparatorChar
if ($ResolvedOutput.StartsWith($ResolvedRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Write the generated request outside the clean repository checkout."
}
if (Test-Path -LiteralPath $ResolvedOutput) {
    throw "The output path already exists."
}
$OutputParent = Split-Path -Parent $ResolvedOutput
if (-not (Test-Path -LiteralPath $OutputParent -PathType Container)) {
    throw "The output parent directory does not exist."
}

$Python = $null
$PythonArguments = @()
foreach ($Candidate in @("python3", "python", "py")) {
    $Command = Get-Command $Candidate -ErrorAction SilentlyContinue
    if ($Command) {
        $Python = $Command.Source
        if ($Candidate -eq "py") { $PythonArguments = @("-3") }
        break
    }
}
if (-not $Python) {
    throw "Python 3 is required to prepare the reviewed request archive."
}

$RunId = ([Convert]::ToUInt64($ReviewedCommit.Substring(0, 15), 16) + 1).ToString()
$Metadata = "$ResolvedOutput.metadata.json"
$Second = "$ResolvedOutput.reproducibility-check"
$ExactCheckout = Join-Path (
    [IO.Path]::GetTempPath()
) "buh-reviewed-preflight-$([Guid]::NewGuid().ToString('N'))"
try {
    # Build from a disposable exact-byte checkout. This prevents a Windows
    # core.autocrlf setting from changing immutable JSON bytes while retaining
    # the original checkout as the reviewed identity/cleanliness boundary.
    & git clone --no-checkout --no-hardlinks -- $RepoRoot $ExactCheckout *> $null
    Assert-NativeSuccess "Reviewed local clone"
    & git -C $ExactCheckout config core.autocrlf false
    Assert-NativeSuccess "Exact-byte checkout configuration"
    & git -C $ExactCheckout checkout --detach $ReviewedCommit *> $null
    Assert-NativeSuccess "Reviewed exact-byte checkout"
    $ExactHead = (& git -C $ExactCheckout rev-parse HEAD 2>$null | Out-String).Trim()
    Assert-NativeSuccess "Exact-byte commit verification"
    if ($ExactHead -cne $ReviewedCommit) {
        throw "The disposable checkout does not match the reviewed commit."
    }
    $ArchiveBuilder = Join-Path $ExactCheckout "ops/deploy/request_archive.py"
    $ReleaseDirectory = Join-Path $ExactCheckout "releases/platform/v0.6.1"
    $Arguments = @(
        $ArchiveBuilder,
        "--root", $ExactCheckout,
        "--release-dir", $ReleaseDirectory,
        "--repository", "Fifty5D/B-UH-AllianceAuth",
        "--release-commit", $ReleaseCommit,
        "--mode", "preflight",
        "--workflow-run-id", $RunId,
        "--workflow-run-attempt", "1",
        "--bootstrap-recovery",
        "--output", $ResolvedOutput,
        "--metadata-output", $Metadata
    )
    & $Python @PythonArguments @Arguments *> $null
    Assert-NativeSuccess "Reviewed preflight request preparation"
    $SecondArguments = @($Arguments)
    $OutputIndex = [Array]::IndexOf($SecondArguments, "--output") + 1
    $MetadataIndex = [Array]::IndexOf($SecondArguments, "--metadata-output")
    $SecondArguments[$OutputIndex] = $Second
    $SecondArguments = @($SecondArguments[0..($MetadataIndex - 1)])
    & $Python @PythonArguments @SecondArguments *> $null
    Assert-NativeSuccess "Reviewed preflight request reproducibility check"
    $FirstHash = (Get-FileHash -LiteralPath $ResolvedOutput -Algorithm SHA256).Hash
    $SecondHash = (Get-FileHash -LiteralPath $Second -Algorithm SHA256).Hash
    if ($FirstHash -cne $SecondHash) {
        throw "The reviewed preflight request was not deterministic."
    }
    Write-Host "Prepared exact receiver-upgrade preflight request:" -ForegroundColor Cyan
    Write-Host $ResolvedOutput
    Write-Host "SHA256=$($FirstHash.ToLowerInvariant())"
    Write-Host "This schema-v1-host preflight is receiver-upgrade evidence only, not production readiness."
}
catch {
    Remove-Item -LiteralPath $ResolvedOutput -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $Metadata -Force -ErrorAction SilentlyContinue
    throw
}
finally {
    Remove-Item -LiteralPath $Second -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $ExactCheckout -PathType Container) {
        Remove-Item -LiteralPath $ExactCheckout -Recurse -Force -ErrorAction SilentlyContinue
    }
}
