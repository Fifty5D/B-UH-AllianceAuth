param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ReviewedCommit,
    [Parameter(Mandatory = $true)][string]$RepositoryPath,
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]*$')][string]$SshHost = 'b-uh'
)
# Staged diagnostics only: no application/receiver installation and no recovery mutation.
$ErrorActionPreference = 'Stop'
$taskDirectory = Join-Path $env:TEMP ('buh-sso-incident-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $taskDirectory | Out-Null
git -C $RepositoryPath fetch https://github.com/Fifty5D/B-UH-AllianceAuth.git $ReviewedCommit
if ($LASTEXITCODE -ne 0) { throw 'Could not fetch the reviewed incident diagnostic commit.' }
$archivePath = Join-Path $taskDirectory 'diagnostic.tar'
git -C $RepositoryPath archive --format=tar --output=$archivePath $ReviewedCommit -- ops/incidents/collect_sso_incident.py ops/incidents/database_report.py
if ($LASTEXITCODE -ne 0) { throw 'Could not archive the reviewed diagnostic source.' }
$archiveHash = (Get-FileHash -Algorithm SHA256 $archivePath).Hash.ToLowerInvariant()
$uploadDirectory = (ssh -o BatchMode=yes $SshHost 'umask 077; mktemp -d /tmp/buh-incident-upload.XXXXXXXXXX').Trim()
if ($LASTEXITCODE -ne 0 -or $uploadDirectory -notmatch '^/tmp/buh-incident-upload\.[A-Za-z0-9]+$') {
    throw 'Could not create a private diagnostic upload directory through the existing owner SSH profile.'
}
scp -o BatchMode=yes $archivePath "${SshHost}:${uploadDirectory}/diagnostic.tar"
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
import tarfile
import tempfile

try:
    upload, expected, commit = sys.argv[1:]
    if (not re.fullmatch(r"/tmp/buh-incident-upload\.[A-Za-z0-9]+", upload)
            or not re.fullmatch(r"[0-9a-f]{64}", expected)
            or not re.fullmatch(r"[0-9a-f]{40}", commit)):
        raise ValueError("invalid staging arguments")
    os.umask(0o077)
    descriptor = os.open(Path(upload) / "diagnostic.tar", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("invalid staging archive")
        archive = stream.read(256 * 1024 + 1)
    if len(archive) > 256 * 1024 or hashlib.sha256(archive).hexdigest() != expected:
        raise ValueError("diagnostic source changed or exceeded bound")
    stage = Path(tempfile.mkdtemp(prefix="buh-sso-incident-" + commit[:8] + "-", dir="/root"))
    wanted = {"ops/incidents/collect_sso_incident.py", "ops/incidents/database_report.py"}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        members = tar.getmembers()
        files = [member for member in members if member.isfile()]
        if (len(members) > 8 or {member.name for member in files} != wanted
                or len(files) != len(wanted)
                or any(not (member.isfile() or member.isdir())
                       or member.name.startswith("/") or ".." in Path(member.name).parts
                       for member in members)):
            raise ValueError("unexpected diagnostic archive members")
        for member in files:
            if member.size > 128 * 1024:
                raise ValueError("diagnostic source exceeds bound")
            destination = stage / Path(member.name).name
            destination.write_bytes(tar.extractfile(member).read())
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
$reportPath = Join-Path $taskDirectory 'incident-report.json'
$rootScript | ssh -o BatchMode=yes $SshHost "sudo -n timeout 960s /usr/bin/python3 -B - $uploadDirectory $archiveHash $ReviewedCommit" |
    Tee-Object -FilePath $reportPath -OutVariable buhIncidentOutput
$incidentExitCode = $LASTEXITCODE
try { $buhIncidentOutput | Set-Clipboard } catch { Write-Host 'Clipboard unavailable; the report file is retained.' }
Write-Host "Incident report: $reportPath"
Write-Host 'Existing tokens, links, owner settings, receiver, traffic and retained recovery resources were preserved.'
if ($incidentExitCode -ne 0) { Write-Host 'Some evidence was incomplete. Return the report, including its error categories.' }
