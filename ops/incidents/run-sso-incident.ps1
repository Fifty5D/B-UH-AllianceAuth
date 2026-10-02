param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ReviewedCommit,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$CollectorSha256,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$DatabaseSha256,
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]*$')][string]$SshHost = 'b-uh'
)
# Read-only production evidence. Only diagnostic staging/report files are written.
$ErrorActionPreference = 'Stop'
$taskDirectory = Join-Path $env:TEMP ('buh-sso-incident-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $taskDirectory | Out-Null
$sourceDirectory = Join-Path $taskDirectory 'source'
New-Item -ItemType Directory -Path $sourceDirectory | Out-Null
$expectedFiles = @{
    'collect_sso_incident.py' = $CollectorSha256
    'database_report.py' = $DatabaseSha256
}
foreach ($name in $expectedFiles.Keys) {
    $path = Join-Path $sourceDirectory $name
    $url = "https://raw.githubusercontent.com/Fifty5D/B-UH-AllianceAuth/$ReviewedCommit/ops/incidents/$name"
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $path
    if ((Get-FileHash -Algorithm SHA256 $path).Hash.ToLowerInvariant() -ne $expectedFiles[$name]) {
        throw 'Downloaded diagnostic source did not match its reviewed hash.'
    }
}
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archivePath = Join-Path $taskDirectory 'diagnostic.zip'
[System.IO.Compression.ZipFile]::CreateFromDirectory($sourceDirectory, $archivePath)
$archiveHash = (Get-FileHash -Algorithm SHA256 $archivePath).Hash.ToLowerInvariant()
$uploadOutput = @(ssh -o BatchMode=yes -o ConnectTimeout=20 $SshHost 'umask 077; mktemp -d /tmp/buh-incident-upload.XXXXXXXXXX')
if ($LASTEXITCODE -ne 0 -or $uploadOutput.Count -ne 1) {
    throw 'Could not create a diagnostic upload directory through the existing owner SSH profile.'
}
$uploadDirectory = $uploadOutput[0].Trim()
if ($uploadDirectory -notmatch '^/tmp/buh-incident-upload\.[A-Za-z0-9]+$') {
    throw 'Unexpected diagnostic upload directory.'
}
scp -o BatchMode=yes -o ConnectTimeout=20 $archivePath ($SshHost + ':' + $uploadDirectory + '/diagnostic.zip')
if ($LASTEXITCODE -ne 0) { throw 'Diagnostic upload failed.' }
$rootScript = @'
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

try:
    upload, expected, commit, collector_hash, database_hash = sys.argv[1:]
    if (not re.fullmatch(r"/tmp/buh-incident-upload\.[A-Za-z0-9]+", upload)
            or any(not re.fullmatch(r"[0-9a-f]{64}", item)
                   for item in (expected, collector_hash, database_hash))
            or not re.fullmatch(r"[0-9a-f]{40}", commit)):
        raise ValueError("invalid staging arguments")
    os.umask(0o077)
    descriptor = os.open(Path(upload) / "diagnostic.zip", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("invalid staging archive")
        archive = stream.read(256 * 1024 + 1)
    if len(archive) > 256 * 1024 or hashlib.sha256(archive).hexdigest() != expected:
        raise ValueError("diagnostic source changed or exceeded bound")
    stage = Path(tempfile.mkdtemp(prefix="buh-sso-incident-" + commit[:8] + "-", dir="/root"))
    wanted = {"collect_sso_incident.py": collector_hash, "database_report.py": database_hash}
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        members = zipped.infolist()
        if len(members) != 2 or {member.filename for member in members} != set(wanted):
            raise ValueError("unexpected diagnostic archive members")
        for member in members:
            if member.is_dir() or member.file_size > 128 * 1024:
                raise ValueError("diagnostic source exceeds bound")
            with zipped.open(member) as stream:
                data = stream.read(128 * 1024 + 1)
            if len(data) != member.file_size or hashlib.sha256(data).hexdigest() != wanted[member.filename]:
                raise ValueError("diagnostic source hash mismatch")
            destination = stage / member.filename
            destination.write_bytes(data)
            destination.chmod(0o600)
    result = subprocess.run(
        ["/usr/bin/python3", "-B", "-P", str(stage / "collect_sso_incident.py")],
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "PYTHONPATH": "/usr/local/lib/buh-platform-v2"},
        cwd="/", stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=900,
    )
    if not result.stdout or len(result.stdout) > 8 * 1024 * 1024:
        raise ValueError("diagnostic output missing or exceeded bound")
    report = json.loads(result.stdout)
    if report.get("read_only") is not True:
        raise ValueError("unexpected report")
    report["diagnostic_source_commit"] = commit
    report["staged_report_path"] = str(stage / "incident-report.json")
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    (stage / "incident-report.json").write_text(encoded, encoding="utf-8")
    print(encoded, end="", flush=True)
    raise SystemExit(result.returncode)
except SystemExit:
    raise
except Exception as error:
    print(json.dumps({"read_only": True, "scan_complete": False,
                      "preserve_recovery_resources": True,
                      "error_type": type(error).__name__}, indent=2), flush=True)
    raise SystemExit(1)
'@
$sshOutput = @($rootScript | ssh -o BatchMode=yes -o ConnectTimeout=20 $SshHost "sudo -n timeout 960s /usr/bin/python3 -B - $uploadDirectory $archiveHash $ReviewedCommit $CollectorSha256 $DatabaseSha256")
$incidentExitCode = $LASTEXITCODE
$reportText = $sshOutput -join [Environment]::NewLine
try { $report = $reportText | ConvertFrom-Json } catch {
    throw 'SSH returned no valid JSON report. No application recovery or deployment was attempted.'
}
if ($report.read_only -ne $true) { throw 'Unexpected report type.' }
$desktop = [Environment]::GetFolderPath('Desktop')
if (-not $desktop) { $desktop = (Get-Location).Path }
$reportPath = Join-Path $desktop ('BUH-SSO-Incident-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.json')
[System.IO.File]::WriteAllText($reportPath, $reportText + [Environment]::NewLine, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "Saved report: $reportPath"
Write-Host 'Existing tokens, links, owner settings, receiver, traffic and recovery resources were preserved.'
if ($incidentExitCode -ne 0 -or $report.scan_complete -ne $true) {
    Write-Host 'Some evidence was incomplete. Attach the report, including its safe error categories.'
} else { Write-Host 'Collection complete. Attach the JSON report in ChatGPT.' }
