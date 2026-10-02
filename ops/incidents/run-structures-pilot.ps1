param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ReviewedCommit,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$HostSha256,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$RecoverySha256,
    [Parameter(Mandatory = $true)][ValidatePattern('^gh-[0-9]+-[0-9]+$')][string]$AttemptId,
    [Parameter(Mandatory = $true)][ValidateLength(1,100)][string]$CharacterName,
    [ValidateSet('report','apply')][string]$Mode = 'report',
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]*$')][string]$SshHost = 'b-uh'
)
# report: no refresh or application mutations.
# apply: refresh ONE existing token and complete ONE owner's native sync,
# then re-enable only its matching stale disabled selector after full success.
# Neither mode deploys code, reconciles/cleans the retained attempt, or changes MA.
$ErrorActionPreference = 'Stop'
$taskDirectory = Join-Path $env:TEMP ('buh-structures-pilot-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $taskDirectory | Out-Null
$sourceDirectory = Join-Path $taskDirectory 'source'
New-Item -ItemType Directory -Path $sourceDirectory | Out-Null
$expectedFiles = @{
    'run_structures_pilot.py' = $HostSha256
    'structures_recovery.py' = $RecoverySha256
}
foreach ($name in $expectedFiles.Keys) {
    $path = Join-Path $sourceDirectory $name
    $url = "https://raw.githubusercontent.com/Fifty5D/B-UH-AllianceAuth/$ReviewedCommit/ops/incidents/$name"
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $path
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
$nameBytes = [System.Text.Encoding]::UTF8.GetBytes($CharacterName)
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
try:
    upload, expected, commit, host_hash, recovery_hash, attempt, name_encoded, mode = sys.argv[1:]
    if (not re.fullmatch(r"/tmp/buh-incident-upload\.[A-Za-z0-9]+", upload)
            or any(not re.fullmatch(r"[0-9a-f]{64}", item)
                   for item in (expected, host_hash, recovery_hash))
            or not re.fullmatch(r"[0-9a-f]{40}", commit)
            or not re.fullmatch(r"gh-[0-9]+-[0-9]+", attempt)
            or mode not in {"apply", "report"}):
        raise ValueError("invalid staging arguments")
    name = base64.b64decode(name_encoded, validate=True).decode("utf-8")
    if not name or len(name) > 100 or any(ord(char) < 32 for char in name):
        raise ValueError("invalid name")
    os.umask(0o077)
    descriptor = os.open(Path(upload) / "pilot.zip", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("invalid staging archive")
        archive = stream.read(256 * 1024 + 1)
    if len(archive) > 256 * 1024 or hashlib.sha256(archive).hexdigest() != expected:
        raise ValueError("pilot source changed or exceeded bound")
    stage = Path(tempfile.mkdtemp(prefix="buh-structures-pilot-" + commit[:8] + "-", dir="/root"))
    wanted = {"run_structures_pilot.py": host_hash, "structures_recovery.py": recovery_hash}
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        members = zipped.infolist()
        if len(members) != 2 or {member.filename for member in members} != set(wanted):
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
    (stage / "invocation.json").write_text(json.dumps({
        "source_commit": commit, "attempt_id": attempt, "mode": mode,
        "character_name": name, "host_sha256": host_hash, "recovery_sha256": recovery_hash,
    }, indent=2), encoding="utf-8")
    result = subprocess.run(
        ["/usr/bin/python3", "-B", "-P", str(stage / "run_structures_pilot.py"), attempt, name, mode],
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "PYTHONPATH": "/usr/local/lib/buh-platform-v2"},
        cwd="/", stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600,
    )
    if not result.stdout or len(result.stdout) > 256 * 1024:
        raise ValueError("pilot output missing or exceeded bound")
    report = json.loads(result.stdout)
    if report.get("read_only") != (mode == "report") or not report.get("single_character_pilot"):
        raise ValueError("unexpected report")
    report["pilot_source_commit"] = commit
    report["staged_report_path"] = str(stage / "pilot-report.json")
    report["process_exit_code"] = result.returncode
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    (stage / "pilot-report.json").write_text(encoded, encoding="utf-8")
    print(encoded, end="", flush=True)
    raise SystemExit(result.returncode)
except SystemExit:
    raise
except Exception as error:
    report = {"read_only": mode == "report", "single_character_pilot": True,
              "scan_complete": False, "preserve_recovery_resources": True,
              "mutation_result": "unknown_requires_report_review",
              "error_type": type(error).__name__}
    if stage is not None:
        report["staged_report_path"] = str(stage / "pilot-failure.json")
        (stage / "pilot-failure.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)
    raise SystemExit(1)
'@
$sshOutput = @($rootScript | ssh -o BatchMode=yes -o ConnectTimeout=20 $SshHost "sudo -n timeout 660s /usr/bin/python3 -B - $uploadDirectory $archiveHash $ReviewedCommit $HostSha256 $RecoverySha256 $AttemptId $nameEncoded $Mode")
$pilotExitCode = $LASTEXITCODE
$reportText = $sshOutput -join [Environment]::NewLine
try { $report = $reportText | ConvertFrom-Json } catch {
    throw 'SSH returned no valid report. Preserve all recovery resources; do not blindly repeat an apply run.'
}
$desktop = [Environment]::GetFolderPath('Desktop')
if (-not $desktop) { $desktop = (Get-Location).Path }
$reportPath = Join-Path $desktop ('BUH-Structures-Pilot-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.json')
[System.IO.File]::WriteAllText($reportPath, $reportText + [Environment]::NewLine, (New-Object System.Text.UTF8Encoding($false)))
$handoff = [ordered]@{
    scan_complete = $report.scan_complete
    read_only = $report.read_only
    source_commit = $report.pilot_source_commit
    error_type = $report.error_type
    guard_reason = $report.guard_reason
    failure_sites = $report.failure_sites
    retained_recovery_changed = $report.retained_recovery_changed
    deployment_attempted = $report.deployment_attempted
    result = $report.result
    before_host = $report.before_host
    after_host = $report.after_host
}
$handoffText = $handoff | ConvertTo-Json -Depth 20 -Compress
if ($handoffText.Length -le 10000) {
    Set-Clipboard -Value $handoffText
    Write-Host 'A compact result is copied. Paste it directly into ChatGPT.'
} else {
    Write-Host 'The full bounded report is saved; attach it if the result exceeds the clipboard limit.'
}
Write-Host "Saved report: $reportPath"
Write-Host "Mode: $Mode. Only the selected Structures owner was eligible; Member Audit and retained deployment were not changed."
if ($pilotExitCode -ne 0 -or $report.scan_complete -ne $true -or ($Mode -eq 'apply' -and $report.result.recovered -ne $true)) {
    Write-Host 'The pilot is not proven recovered. Review the result before another apply operation.'
}
