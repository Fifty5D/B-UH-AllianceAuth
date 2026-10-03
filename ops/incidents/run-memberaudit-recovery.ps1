param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ReviewedCommit,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$HostSha256,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$SelectionSha256,
    [ValidatePattern('^/root/buh-structures-pilot-[0-9a-f]{8}-[a-z0-9_]+$')][string]$PreviousStage = '/root/buh-structures-pilot-badc74fd-nsz7zc1a',
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$RecoverySha256,
    [Parameter(Mandatory = $true)][ValidatePattern('^gh-[0-9]+-[0-9]+$')][string]$AttemptId,
    [Parameter(Mandatory = $true)][ValidateLength(1,100)][string]$PilotCharacterName,
    [ValidateSet('report','apply')][string]$Mode = 'report',
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]*$')][string]$SshHost = 'b-uh',
    [switch]$PassThru
)
# report: read-only eligibility; apply: Fifty5D, three representatives, then 5/10 sequential batches.
# Application data changes only. No token replacement/deletion, Auth relinking or deployment recovery.
$ErrorActionPreference = 'Stop'
$taskDirectory = Join-Path $env:TEMP ('buh-memberaudit-recovery-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $taskDirectory | Out-Null
$sourceDirectory = Join-Path $taskDirectory 'source'
New-Item -ItemType Directory -Path $sourceDirectory | Out-Null
$expectedFiles = @{
    'run_memberaudit_recovery.py' = $HostSha256
    'memberaudit_selection.py' = $SelectionSha256
    'memberaudit_recovery.py' = $RecoverySha256
}
foreach ($name in $expectedFiles.Keys) {
    $path = Join-Path $sourceDirectory $name
    $url = "https://raw.githubusercontent.com/Fifty5D/B-UH-AllianceAuth/$ReviewedCommit/ops/incidents/$name"
    Invoke-WebRequest -UseBasicParsing -TimeoutSec 30 -Uri $url -OutFile $path
    if ((Get-FileHash -Algorithm SHA256 $path).Hash.ToLowerInvariant() -ne $expectedFiles[$name]) {
        throw 'Downloaded pilot source did not match its tested hash.'
    }
}
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archivePath = Join-Path $taskDirectory 'pilot.zip'
[System.IO.Compression.ZipFile]::CreateFromDirectory($sourceDirectory, $archivePath)
$archiveHash = (Get-FileHash -Algorithm SHA256 $archivePath).Hash.ToLowerInvariant()
$uploadOutput = @(ssh -o BatchMode=yes -o ConnectTimeout=20 $SshHost 'umask 077; mktemp -d /tmp/buh-incident-upload.XXXXXXXXXX')
if ($LASTEXITCODE -ne 0 -or $uploadOutput.Count -ne 1) {
    throw 'Could not stage the pilot through the existing owner SSH profile.'
}
$uploadDirectory = $uploadOutput[0].Trim()
if ($uploadDirectory -notmatch '^/tmp/buh-incident-upload\.[A-Za-z0-9]+$') {
    throw 'Unexpected pilot upload directory.'
}
scp -o BatchMode=yes -o ConnectTimeout=20 $archivePath ($SshHost + ':' + $uploadDirectory + '/pilot.zip')
if ($LASTEXITCODE -ne 0) { throw 'Pilot upload failed.' }
# Pass the character name as base64 data, never as executable shell text.
$nameBytes = [System.Text.Encoding]::UTF8.GetBytes($PilotCharacterName)
$nameEncoded = [Convert]::ToBase64String($nameBytes)
$rootScript = @'
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import zipfile

stage = None
mode = "unknown"
phase = "validate_arguments"
process_started = False
output_evidence = {"stdout_bytes": None, "stderr_bytes": None, "limit_bytes": None}
safe_guard_reasons = {
    "invalid staging arguments": "invalid_staging_arguments",
    "invalid name": "invalid_name",
    "invalid staging archive": "invalid_staging_archive",
    "pilot source changed or exceeded bound": "staging_archive_changed_or_oversized",
    "unexpected pilot archive members": "unexpected_archive_members",
    "pilot source exceeds bound": "source_file_exceeded_bound",
    "pilot source hash mismatch": "source_hash_mismatch",
    "pilot output missing": "pilot_output_missing",
    "pilot output exceeded bound": "pilot_output_exceeded_bound",
    "unexpected report": "unexpected_report_schema",
}
try:
    upload, expected, commit, host_hash, recovery_hash, selection_hash, attempt, name_encoded, mode, previous_stage = sys.argv[1:]
    if (not re.fullmatch(r"/tmp/buh-incident-upload\.[A-Za-z0-9]+", upload)
            or any(not re.fullmatch(r"[0-9a-f]{64}", item)
                   for item in (expected, host_hash, recovery_hash, selection_hash))
            or not re.fullmatch(r"[0-9a-f]{40}", commit)
            or not re.fullmatch(r"gh-[0-9]+-[0-9]+", attempt)
            or mode not in {"apply", "report"}
            or not re.fullmatch(r"/root/buh-structures-pilot-[0-9a-f]{8}-[a-z0-9_]+", previous_stage)):
        raise ValueError("invalid staging arguments")
    output_evidence["limit_bytes"] = 2 * 1024 * 1024
    phase = "validate_character_name"
    name = base64.b64decode(name_encoded, validate=True).decode("utf-8")
    if not name or len(name) > 100 or any(ord(char) < 32 for char in name):
        raise ValueError("invalid name")
    os.umask(0o077)
    phase = "validate_archive"
    descriptor = os.open(Path(upload) / "pilot.zip", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("invalid staging archive")
        archive = stream.read(256 * 1024 + 1)
    if len(archive) > 256 * 1024 or hashlib.sha256(archive).hexdigest() != expected:
        raise ValueError("pilot source changed or exceeded bound")
    stage = Path(tempfile.mkdtemp(prefix="buh-memberaudit-recovery-" + commit[:8] + "-", dir="/root"))
    wanted = {"run_memberaudit_recovery.py": host_hash, "memberaudit_recovery.py": recovery_hash,
              "memberaudit_selection.py": selection_hash}
    phase = "validate_and_extract_sources"
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        members = zipped.infolist()
        if len(members) != 3 or {member.filename for member in members} != set(wanted):
            raise ValueError("unexpected pilot archive members")
        for member in members:
            if member.is_dir() or member.file_size > 128 * 1024:
                raise ValueError("pilot source exceeds bound")
            with zipped.open(member) as stream:
                data = stream.read(128 * 1024 + 1)
            if len(data) != member.file_size or hashlib.sha256(data).hexdigest() != wanted[member.filename]:
                raise ValueError("pilot source hash mismatch")
            destination = stage / member.filename
            destination.write_bytes(data)
            destination.chmod(0o600)
    phase = "record_invocation"
    (stage / "invocation.json").write_text(json.dumps({
        "source_commit": commit, "attempt_id": attempt, "mode": mode,
        "character_name": name, "host_sha256": host_hash, "recovery_sha256": recovery_hash,
    }, indent=2), encoding="utf-8")
    phase = "run_pilot"
    process_started = True
    result = subprocess.run(
        ["/usr/bin/python3", "-B", "-P", str(stage / "run_memberaudit_recovery.py"), attempt, name, mode, previous_stage],
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
             "PYTHONPATH": str(stage) + ":/usr/local/lib/buh-platform-v2"},
        cwd="/", stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3300,
    )
    phase = "check_output_size"
    output_evidence.update({"stdout_bytes": len(result.stdout), "stderr_bytes": len(result.stderr)})
    if not result.stdout:
        raise ValueError("pilot output missing")
    if len(result.stdout) > output_evidence["limit_bytes"]:
        raise ValueError("pilot output exceeded bound")
    phase = "parse_report"
    report = json.loads(result.stdout)
    phase = "validate_report"
    if (not isinstance(report, dict) or report.get("schema_version") != 1
            or type(report.get("scan_complete")) is not bool
            or report.get("read_only") is not (mode == "report")
            or report.get("scope") != "memberaudit_october_outage"):
        raise ValueError("unexpected report")
    report["launcher_output"] = output_evidence
    report["recovery_source_commit"] = commit
    report["staged_report_path"] = str(stage / "memberaudit-report.json")
    report["process_exit_code"] = result.returncode
    phase = "save_report"
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    (stage / "memberaudit-report.json").write_text(encoded, encoding="utf-8")
    print(encoded, end="", flush=True)
    raise SystemExit(result.returncode)
except SystemExit:
    raise
except Exception as error:
    report = {"read_only": mode == "report", "scope": "memberaudit_october_outage",
              "scan_complete": False, "preserve_recovery_resources": True,
              "mutation_result": ("not_attempted_read_only" if mode == "report"
                                  else "unknown_requires_report_review" if process_started else "not_started"),
              "error_type": type(error).__name__, "failure_phase": phase,
              "guard_reason": safe_guard_reasons.get(str(error)),
              "launcher_output": output_evidence, "failure_sites": []}
    trace = error.__traceback__
    while trace is not None and len(report["failure_sites"]) < 8:
        if trace.tb_frame.f_globals is globals():
            report["failure_sites"].append({"component": "launcher", "function": "root_launcher",
                                            "line": trace.tb_lineno})
        trace = trace.tb_next
    if stage is not None:
        report["staged_report_path"] = str(stage / "memberaudit-launcher-failure.json")
        (stage / "memberaudit-launcher-failure.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)
    raise SystemExit(1)
'@
$sshOutput = @($rootScript | ssh -o BatchMode=yes -o ConnectTimeout=20 $SshHost "sudo -n timeout 3360s /usr/bin/python3 -B - $uploadDirectory $archiveHash $ReviewedCommit $HostSha256 $RecoverySha256 $SelectionSha256 $AttemptId $nameEncoded $Mode $PreviousStage")
$pilotExitCode = $LASTEXITCODE
$reportText = $sshOutput -join [Environment]::NewLine
try { $report = $reportText | ConvertFrom-Json } catch {
    throw 'SSH returned no valid report. Preserve all recovery resources; do not blindly repeat an apply run.'
}
$desktop = [Environment]::GetFolderPath('Desktop')
if (-not $desktop) { $desktop = (Get-Location).Path }
$reportPath = Join-Path $desktop ('BUH-MemberAudit-Recovery-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.json')
[System.IO.File]::WriteAllText($reportPath, $reportText + [Environment]::NewLine, (New-Object System.Text.UTF8Encoding($false)))
$summary = [ordered]@{
    scan_complete = $report.scan_complete
    recovery_complete = $report.recovery_complete
    source_commit = $report.recovery_source_commit
    counts = $report.counts
    stop_reason = $report.stop_reason
    error_type = $report.error_type
    guard_reason = $report.guard_reason
    retained_resources_unchanged_verified = $report.retained_resources_unchanged_verified
    excluded_before = $report.excluded_before
    excluded_after = $report.excluded_after
    platform_version = $report.after_host.platform_version
    observed_disk_growth_bytes = $report.observed_disk_growth_bytes
}
$summaryText = $summary | ConvertTo-Json -Depth 10 -Compress
try { Set-Clipboard -Value $summaryText } catch { }
Write-Host $summaryText
Write-Host "Saved one report: $reportPath"
Write-Host "Mode: $Mode. Member Audit October outage only. Deployment hold and recovery resources are preserved."
if ($pilotExitCode -ne 0 -or $report.scan_complete -ne $true -or ($Mode -eq 'apply' -and $report.recovery_complete -ne $true)) {
    Write-Host 'Recovery stopped or remains incomplete. Review this report before continuing.'
}
if ($PassThru) {
    [pscustomobject]@{ report = $report; report_path = $reportPath; exit_code = $pilotExitCode }
}
