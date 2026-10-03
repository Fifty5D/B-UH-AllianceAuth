param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ReviewedCommit,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$SourceSha256,
    [Parameter(Mandatory = $true)][ValidatePattern('^/root/buh-structures-pilot-badc74fd-[A-Za-z0-9_-]+$')][string]$PilotSourceDirectory,
    [Parameter(Mandatory = $true)][ValidatePattern('^gh-[0-9]+-[0-9]+$')][string]$AttemptId,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$HoldSha256,
    [Parameter(Mandatory = $true)][ValidateLength(1,100)][string]$PilotCharacterName,
    [ValidateSet('report','recover')][string]$Mode = 'report',
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]*$')][string]$SshHost = 'b-uh'
)
# report: read-only application/host verification plus one bounded backup hash.
# recover: the installed receiver's supported rollback verification and cleanup.
# No new deployment, token refresh, Auth/MA mutation or manual notification sends.
$ErrorActionPreference = 'Stop'
$taskDirectory = Join-Path $env:TEMP ('buh-retained-recovery-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $taskDirectory | Out-Null
$source = Join-Path $taskDirectory 'reconcile_structures_attempt.py'
Invoke-WebRequest -UseBasicParsing -TimeoutSec 30 `
    -Uri "https://raw.githubusercontent.com/Fifty5D/B-UH-AllianceAuth/$ReviewedCommit/ops/incidents/reconcile_structures_attempt.py" `
    -OutFile $source
if ((Get-FileHash -Algorithm SHA256 $source).Hash.ToLowerInvariant() -cne $SourceSha256) {
    throw 'Recovery bridge source checksum mismatch.'
}
$uploadOutput = @(ssh -o BatchMode=yes -o ConnectTimeout=20 $SshHost 'umask 077; mktemp -d /tmp/buh-recovery-upload.XXXXXXXXXX')
if ($LASTEXITCODE -ne 0 -or $uploadOutput.Count -ne 1) { throw 'Could not stage the recovery bridge.' }
$upload = $uploadOutput[0].Trim()
if ($upload -notmatch '^/tmp/buh-recovery-upload\.[A-Za-z0-9]+$') { throw 'Unexpected recovery upload directory.' }
scp -o BatchMode=yes -o ConnectTimeout=20 $source ($SshHost + ':' + $upload + '/recovery.py')
if ($LASTEXITCODE -ne 0) { throw 'Recovery bridge upload failed.' }
$nameEncoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($PilotCharacterName))
$rootScript = @'
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

stage = None
started = False
mode = "unknown"
phase = "validate_arguments"
try:
    upload, digest, commit, prior, attempt, hold, encoded_name, mode = sys.argv[1:]
    if (not re.fullmatch(r"/tmp/buh-recovery-upload\.[A-Za-z0-9]+", upload)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not re.fullmatch(r"[0-9a-f]{64}", hold)
            or not re.fullmatch(r"[0-9a-f]{40}", commit)
            or not re.fullmatch(r"gh-[0-9]+-[0-9]+", attempt)
            or not re.fullmatch(r"/root/buh-structures-pilot-badc74fd-[A-Za-z0-9_-]+", prior)
            or mode not in {"report", "recover"}):
        raise ValueError()
    name = base64.b64decode(encoded_name, validate=True).decode("utf-8")
    if not 1 <= len(name) <= 100 or any(ord(c) < 32 for c in name):
        raise ValueError()
    os.umask(0o077)
    phase = "validate_source"
    descriptor = os.open(Path(upload) / "recovery.py", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError()
        data = stream.read(64 * 1024 + 1)
    if len(data) > 64 * 1024 or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError()
    stage = Path(tempfile.mkdtemp(prefix="buh-retained-recovery-" + commit[:8] + "-", dir="/root"))
    program = stage / "reconcile_structures_attempt.py"
    program.write_bytes(data)
    program.chmod(0o600)
    phase = "supported_recovery_bridge"
    started = True
    result = subprocess.run(
        ["/usr/bin/python3", "-B", "-P", str(program), prior, attempt, hold, name, mode],
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "PYTHONPATH": "/usr/local/lib/buh-platform-v2"},
        cwd="/", stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1200,
    )
    phase = "validate_report"
    if not result.stdout or len(result.stdout) > 256 * 1024:
        raise ValueError()
    report = json.loads(result.stdout)
    if (not isinstance(report, dict) or report.get("schema_version") != 1
            or type(report.get("scan_complete")) is not bool or report.get("read_only") is not (mode == "report")):
        raise ValueError()
    report["source_commit"] = commit
    report["process_exit_code"] = result.returncode
    report["staged_report_path"] = str(stage / "recovery-report.json")
    report["launcher_output"] = {"stdout_bytes": len(result.stdout), "stderr_bytes": len(result.stderr), "limit_bytes": 256 * 1024}
    text = json.dumps(report, indent=2, sort_keys=True)
    (stage / "recovery-report.json").write_text(text + "\n", encoding="utf-8")
    print(text, flush=True)
    raise SystemExit(result.returncode)
except SystemExit:
    raise
except Exception as error:
    report = {"schema_version": 1, "scan_complete": False, "read_only": mode == "report",
              "failure_phase": phase, "error_type": type(error).__name__,
              "mutation_result": "unknown_requires_report_review" if started and mode == "recover" else "not_attempted",
              "deployment_attempted": False, "memberaudit_mutation_attempted": False}
    if stage is not None:
        report["staged_report_path"] = str(stage / "recovery-failure.json")
        (stage / "recovery-failure.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)
    raise SystemExit(1)
'@
$sshOutput = @($rootScript | ssh -o BatchMode=yes -o ConnectTimeout=20 $SshHost "sudo -n timeout 1260s /usr/bin/python3 -B - $upload $SourceSha256 $ReviewedCommit $PilotSourceDirectory $AttemptId $HoldSha256 $nameEncoded $Mode")
$exitCode = $LASTEXITCODE
$text = $sshOutput -join [Environment]::NewLine
try { $report = $text | ConvertFrom-Json } catch {
    throw 'No structured recovery report returned. Do not repeat recovery or start deployment until this is reviewed.'
}
$desktop = [Environment]::GetFolderPath('Desktop')
if (-not $desktop) { $desktop = (Get-Location).Path }
$reportPath = Join-Path $desktop ('BUH-Retained-Recovery-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.json')
[IO.File]::WriteAllText($reportPath, $text + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
Write-Host "Saved single report: $reportPath"
Write-Host "Mode: $Mode. No new application deployment or Member Audit recovery was started."
if ($exitCode -ne 0 -or $report.scan_complete -ne $true) {
    Write-Host 'Recovery is not fully proven. Do not start another deployment attempt.'
} elseif ($Mode -eq 'recover') {
    Write-Host 'The retained attempt was reconciled by the installed supported recovery path.'
} else {
    Write-Host 'Read-only verification completed; retained resources are unchanged.'
}
