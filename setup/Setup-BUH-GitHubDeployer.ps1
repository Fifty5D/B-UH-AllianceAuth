[CmdletBinding()]
param(
    [string]$SshTarget = "b-uh",
    [string]$AuthDirectory = "/opt/aa-docker",
    [string]$AdminCharacter = "Fifty5D",
    [string]$PaymentCorporation = "Bureau of Unified Harvesting",
    [string]$Repository = "Fifty5D/B-UH-AllianceAuth"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$KeyPath = Join-Path $env:USERPROFILE ".ssh\buh_github_deployer_ed25519"

foreach ($Command in @("ssh", "scp", "ssh-keygen")) {
    if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) {
        throw "Windows OpenSSH command '$Command' is missing."
    }
}
if ($SshTarget -notmatch '^[A-Za-z0-9._-]+$') {
    throw "SshTarget contains unsupported characters."
}
if ($AuthDirectory -notmatch '^/[A-Za-z0-9._/-]+$') {
    throw "AuthDirectory must be a Linux absolute path without spaces."
}
if ($AdminCharacter -notmatch '^[A-Za-z0-9._ -]+$') {
    throw "AdminCharacter contains unsupported characters."
}
if ([string]::IsNullOrWhiteSpace($PaymentCorporation) -or $PaymentCorporation.Length -gt 255 -or
    $PaymentCorporation.Contains("`n") -or $PaymentCorporation.Contains("`r") -or
    $PaymentCorporation.Contains("'")) {
    throw "PaymentCorporation contains unsupported characters."
}
if ($Repository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') {
    throw "Repository must use owner/name format."
}

$SshConfig = & ssh -G $SshTarget
if ($LASTEXITCODE -ne 0) { throw "Could not resolve SSH target '$SshTarget'." }
$ResolvedHost = ($SshConfig | Select-String '^hostname\s+(.+)$').Matches.Groups[1].Value.Trim()
$ResolvedPort = ($SshConfig | Select-String '^port\s+(.+)$').Matches.Groups[1].Value.Trim()
if (-not $ResolvedHost) { throw "Could not determine the VPS hostname from ssh -G." }
if (-not $ResolvedPort) { $ResolvedPort = "22" }

if (-not (Test-Path -LiteralPath $KeyPath)) {
    Write-Host "Creating a dedicated guarded deployment key..." -ForegroundColor Cyan
    & ssh-keygen -q -t ed25519 -N '""' -C "buh-github-deployer" -f $KeyPath
    if ($LASTEXITCODE -ne 0) { throw "ssh-keygen failed." }
}

$EntryScript = Join-Path $RepoRoot "ops\buh-github-deploy-entry"
$RootScript = Join-Path $RepoRoot "ops\buh-github-deploy-root"
$BootstrapScript = Join-Path $RepoRoot "ops\bootstrap-deployer.sh"
$PublicKey = "$KeyPath.pub"
foreach ($Path in @($EntryScript, $RootScript, $BootstrapScript, $PublicKey)) {
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Required deployment file is missing: $Path"
    }
}

Write-Host "Uploading the guarded deployment bridge..." -ForegroundColor Cyan
& ssh $SshTarget "install -d -m 0700 /tmp/buh-github-deployer-setup"
if ($LASTEXITCODE -ne 0) { throw "Could not prepare the VPS staging directory." }
& scp $EntryScript $RootScript $BootstrapScript $PublicKey "${SshTarget}:/tmp/buh-github-deployer-setup/"
if ($LASTEXITCODE -ne 0) { throw "Could not upload the deployment bridge." }

$RemoteCommand = @(
    "bash /tmp/buh-github-deployer-setup/bootstrap-deployer.sh",
    "/tmp/buh-github-deployer-setup/buh-github-deploy-entry",
    "/tmp/buh-github-deployer-setup/buh-github-deploy-root",
    "/tmp/buh-github-deployer-setup/buh_github_deployer_ed25519.pub",
    "'$AuthDirectory'",
    "'$AdminCharacter'",
    "'$PaymentCorporation'"
) -join " "
& ssh $SshTarget $RemoteCommand
if ($LASTEXITCODE -ne 0) { throw "The VPS deployment bootstrap failed." }
& ssh $SshTarget "rm -rf -- /tmp/buh-github-deployer-setup"

$Gh = Get-Command gh -ErrorAction SilentlyContinue
if ($Gh) {
    Write-Host "GitHub CLI found. Adding the deployment key without printing it..." -ForegroundColor Cyan
    Get-Content -LiteralPath $KeyPath -Raw | & $Gh.Source secret set BUH_DEPLOY_SSH_KEY --repo $Repository
    if ($LASTEXITCODE -ne 0) {
        throw "GitHub CLI could not save BUH_DEPLOY_SSH_KEY. The VPS bridge is installed; add the secret manually."
    }
    & $Gh.Source variable set BUH_DEPLOY_USER --body "buh-deployer" --repo $Repository
    if ($LASTEXITCODE -ne 0) { throw "GitHub CLI could not save BUH_DEPLOY_USER." }
    & $Gh.Source variable set BUH_VPS_HOST --body $ResolvedHost --repo $Repository
    if ($LASTEXITCODE -ne 0) { throw "GitHub CLI could not save BUH_VPS_HOST." }
    & $Gh.Source variable set BUH_VPS_PORT --body $ResolvedPort --repo $Repository
    if ($LASTEXITCODE -ne 0) { throw "GitHub CLI could not save BUH_VPS_PORT." }
    Write-Host "GitHub deployment settings saved." -ForegroundColor Green
}
else {
    $SecretsUrl = "https://github.com/$Repository/settings/secrets/actions"
    Write-Host ""
    Write-Host "One GitHub secret remains to be added:" -ForegroundColor Yellow
    Write-Host "  Name:  BUH_DEPLOY_SSH_KEY"
    Write-Host "  Value: complete contents of $KeyPath"
    Write-Host ""
    Write-Host "Repository variables:"
    Write-Host "  BUH_VPS_HOST = $ResolvedHost"
    Write-Host "  BUH_VPS_PORT = $ResolvedPort"
    Write-Host "Your existing BUH_VPS_KNOWN_HOSTS secret is reused."
    Write-Host "GitHub Actions defaults the deployment user to buh-deployer."
    Write-Host "Open: $SecretsUrl"
    $OpenSettings = Read-Host "Type OPEN to open the GitHub Actions secrets page"
    if ($OpenSettings -ceq "OPEN") {
        Start-Process $SecretsUrl
    }
}

Write-Host ""
Write-Host "Guarded GitHub deployment bridge installed successfully." -ForegroundColor Green
Write-Host "The key cannot open a shell and the VPS accepts only a checked Moon Tax release." -ForegroundColor Green
Write-Host "Never paste the private key into chat or commit it to the repository." -ForegroundColor Yellow
Write-Host "Private key retained at: $KeyPath"
