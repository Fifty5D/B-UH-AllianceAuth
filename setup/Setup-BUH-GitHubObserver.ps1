[CmdletBinding()]
param(
    [string]$SshTarget = "b-uh"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$KeyPath = Join-Path $env:USERPROFILE ".ssh\buh_github_observer_ed25519"

foreach ($Command in @("ssh", "scp", "ssh-keygen", "ssh-keyscan")) {
    if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) {
        throw "Windows OpenSSH command '$Command' is missing."
    }
}

if (-not (Test-Path $KeyPath)) {
    Write-Host "Creating a dedicated read-only GitHub observer key..." -ForegroundColor Cyan
    & ssh-keygen -q -t ed25519 -N '""' -C "buh-github-observer" -f $KeyPath
    if ($LASTEXITCODE -ne 0) { throw "ssh-keygen failed." }
}

$EntryScript = Join-Path $RepoRoot "ops\buh-github-observe-entry"
$RootScript = Join-Path $RepoRoot "ops\buh-github-observe-root"
$RedactorScript = Join-Path $RepoRoot "ops\buh-redact-diagnostics.py"
$BootstrapScript = Join-Path $RepoRoot "ops\bootstrap-observer.sh"
$PublicKey = "$KeyPath.pub"

Write-Host "Uploading the guarded observer bridge..." -ForegroundColor Cyan
& ssh $SshTarget "install -d -m 0700 /tmp/buh-github-observer-setup"
if ($LASTEXITCODE -ne 0) { throw "Could not prepare the VPS staging directory." }

& scp $EntryScript $RootScript $RedactorScript $BootstrapScript $PublicKey "${SshTarget}:/tmp/buh-github-observer-setup/"
if ($LASTEXITCODE -ne 0) { throw "Could not upload the observer files." }

$RemoteCommand = @(
    "bash /tmp/buh-github-observer-setup/bootstrap-observer.sh",
    "/tmp/buh-github-observer-setup/buh-github-observe-entry",
    "/tmp/buh-github-observer-setup/buh-github-observe-root",
    "/tmp/buh-github-observer-setup/buh-redact-diagnostics.py",
    "/tmp/buh-github-observer-setup/buh_github_observer_ed25519.pub"
) -join " "
& ssh $SshTarget $RemoteCommand
if ($LASTEXITCODE -ne 0) { throw "The VPS observer bootstrap failed." }

$SshConfig = & ssh -G $SshTarget
if ($LASTEXITCODE -ne 0) { throw "Could not resolve SSH target '$SshTarget'." }
$HostName = ($SshConfig | Select-String '^hostname\s+(.+)$').Matches.Groups[1].Value.Trim()
$Port = ($SshConfig | Select-String '^port\s+(.+)$').Matches.Groups[1].Value.Trim()
if (-not $HostName) { throw "Could not determine the VPS hostname from ssh -G." }
if (-not $Port) { $Port = "22" }

$KnownHostsPath = Join-Path $env:TEMP "buh_vps_known_hosts.txt"
$KnownHostsSource = Join-Path $env:USERPROFILE ".ssh\known_hosts"
$KnownHostLookup = if ($Port -eq "22") { $HostName } else { "[$HostName]:$Port" }
$ExistingHostKeys = @()
if (Test-Path $KnownHostsSource) {
    $ExistingHostKeys = @(& ssh-keygen -F $KnownHostLookup -f $KnownHostsSource 2>$null |
        Where-Object { $_ -and -not $_.StartsWith("#") })
}
if ($ExistingHostKeys.Count -gt 0) {
    $ExistingHostKeys | Set-Content -Encoding ascii $KnownHostsPath
} else {
    Write-Warning "The resolved host was not found in your existing known_hosts file; collecting its current keys."
    & ssh-keyscan -p $Port $HostName 2>$null | Set-Content -Encoding ascii $KnownHostsPath
}
if (-not (Test-Path $KnownHostsPath) -or (Get-Item $KnownHostsPath).Length -eq 0) {
    throw "Could not collect the VPS SSH host key."
}

& ssh $SshTarget "rm -rf -- /tmp/buh-github-observer-setup"

Write-Host ""
Write-Host "Observer bridge installed successfully." -ForegroundColor Green
Write-Host ""
Write-Host "Add these GitHub Actions settings:" -ForegroundColor Yellow
Write-Host "Repository variables:"
Write-Host "  BUH_VPS_HOST = $HostName"
Write-Host "  BUH_VPS_PORT = $Port"
Write-Host "  BUH_OBSERVER_USER = buh-observer"
Write-Host "Repository secrets:"
Write-Host "  BUH_OBSERVER_SSH_KEY = contents of $KeyPath"
Write-Host "  BUH_VPS_KNOWN_HOSTS = contents of $KnownHostsPath"
Write-Host ""
Write-Host "Never paste the private key into chat or commit it to GitHub." -ForegroundColor Yellow
Write-Host "The private key remains at: $KeyPath"
