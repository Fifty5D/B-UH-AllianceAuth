param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{40}$')]
    [string]$ReviewedCommit,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{64}$')]
    [string]$CollectorSha256,
    [Parameter(Mandatory = $true)]
    [ValidateLength(2, 4096)]
    [string]$TargetJson,
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$')]
    [string]$SshHost = 'b-uh'
)

$ErrorActionPreference = 'Stop'
$temporary = Join-Path $env:TEMP ('buh-retained-findings-' + [guid]::NewGuid().ToString('N') + '.py')
Invoke-WebRequest -UseBasicParsing -TimeoutSec 30 -Uri (
    'https://raw.githubusercontent.com/Fifty5D/B-UH-AllianceAuth/' +
    $ReviewedCommit + '/ops/deploy/collect_retained_log_findings.py'
) -OutFile $temporary
if ((Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash.ToLowerInvariant() -ne $CollectorSha256) {
    throw 'Read-only collector checksum mismatch; stopping before SSH.'
}
$payload = [Convert]::ToBase64String([IO.File]::ReadAllBytes($temporary))
$targetPayload = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($TargetJson))
$bootstrap = "import base64,sys;sys.argv=['<buh-retained-log-findings>','--target-json',base64.b64decode('" +
    $targetPayload + "').decode('utf-8')];exec(compile(base64.b64decode('" + $payload +
    "'),'<buh-retained-log-findings>','exec'))"
Write-Host 'Read-only: inspecting selected assets, exact handled callbacks and preserved recovery state.'
Write-Host 'No token refresh, data update, verifier installation, hold change, cleanup or deployment.'
$output = @($bootstrap | ssh -o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=15 `
    -o ServerAliveCountMax=3 $SshHost 'sudo -n timeout 600s /usr/bin/python3 -B -P -')
$exitCode = $LASTEXITCODE
$text = $output -join [Environment]::NewLine
try {
    if ([Text.Encoding]::UTF8.GetByteCount($text) -gt 2097152) { throw 'Report exceeded the bound.' }
    $result = $text | ConvertFrom-Json
    if ($result.schema_version -ne 1 -or $result.read_only -ne $true -or $result.diagnostic_only -ne $true -or
        $result.attempt_id -ne 'gh-36955595351-1') { throw 'Read-only report identity differs.' }
} catch {
    $text = (@{
        schema_version = 1
        read_only = $true
        diagnostic_only = $true
        scan_complete = $false
        attempt_id = 'gh-36955595351-1'
        error_type = 'NoCompleteStructuredReportReturned'
        ssh_exit_code = $exitCode
        collector_commit = $ReviewedCommit
        tooling_installation_attempted = $false
        supported_recovery_attempted = $false
        deployment_attempted = $false
        memberaudit_mutation_attempted = $false
        structures_mutation_attempted = $false
        token_refresh_attempted = $false
        preserve_recovery_resources = $true
    } | ConvertTo-Json -Depth 5)
}
$desktop = [Environment]::GetFolderPath('Desktop')
if (-not $desktop) { $desktop = (Get-Location).Path }
$reportPath = Join-Path $desktop ('BUH-Retained-Log-Findings-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.json')
[IO.File]::WriteAllText($reportPath, $text + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
Write-Host "Saved single read-only report: $reportPath"
Write-Host 'Keep the existing recovery hold and retained resources until the exact findings are verified.'
