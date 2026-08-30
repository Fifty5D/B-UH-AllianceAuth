[CmdletBinding()]
param(
    [string]$SshTarget = "b-uh"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$ShellHelper = Join-Path $RepoRoot "ops\buh-rotate-discord-token.sh"
$PythonHelper = Join-Path $RepoRoot "ops\buh-rotate-discord-token.py"

foreach ($Command in @("ssh", "scp")) {
    if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) {
        throw "Windows OpenSSH command '$Command' is missing."
    }
}

Write-Host "This utility never prints, saves, or places the new token in a command line." -ForegroundColor Cyan
$SecureToken = Read-Host "Paste the newly reset Discord bot token" -AsSecureString
$TokenPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureToken)
$RemoteStagingPrepared = $false

try {
    $PlainToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($TokenPointer)
    if ([string]::IsNullOrWhiteSpace($PlainToken)) { throw "No token was entered." }

    & ssh $SshTarget "install -d -m 0700 /tmp/buh-discord-token-rotation"
    if ($LASTEXITCODE -ne 0) { throw "Could not prepare the VPS staging directory." }
    $RemoteStagingPrepared = $true

    & scp $ShellHelper $PythonHelper "${SshTarget}:/tmp/buh-discord-token-rotation/"
    if ($LASTEXITCODE -ne 0) { throw "Could not upload the guarded token rotation helper." }

    $RemoteCommand = @(
        "bash /tmp/buh-discord-token-rotation/buh-rotate-discord-token.sh",
        "/tmp/buh-discord-token-rotation/buh-rotate-discord-token.py"
    ) -join " "
    $PlainToken | & ssh $SshTarget $RemoteCommand
    if ($LASTEXITCODE -ne 0) { throw "Discord token rotation failed; the previous Auth configuration was restored." }
} finally {
    $PlainToken = $null
    $SecureToken = $null
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($TokenPointer)
    if ($RemoteStagingPrepared) {
        & ssh $SshTarget "rm -rf -- /tmp/buh-discord-token-rotation"
    }
}

Write-Host ""
Write-Host "Token rotation completed successfully." -ForegroundColor Green
Write-Host "Reinstalling the hardened read-only observer..." -ForegroundColor Cyan
& (Join-Path $PSScriptRoot "Setup-BUH-GitHubObserver.ps1") -SshTarget $SshTarget
if ($LASTEXITCODE -ne 0) { throw "Token rotation succeeded, but the observer reinstall failed." }
