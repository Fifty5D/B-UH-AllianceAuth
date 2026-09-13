param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ReviewedCommit,
    [Parameter(Mandatory = $true)][string]$RepositoryPath,
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]*$')][string]$SshHost = 'b-uh'
)
# Run this file from the reviewed commit. It stages a diagnostic tool, never an installer.
$ErrorActionPreference = 'Stop'
$taskDirectory = Join-Path $env:TEMP ('buh-recovery-audit-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $taskDirectory | Out-Null
git -C $RepositoryPath fetch https://github.com/Fifty5D/B-UH-AllianceAuth.git $ReviewedCommit
if ($LASTEXITCODE -ne 0) { throw 'Could not fetch the reviewed diagnostic commit.' }
$sourceArchive = Join-Path $taskDirectory 'source.tar'
git -C $RepositoryPath archive --format=tar --output=$sourceArchive $ReviewedCommit
if ($LASTEXITCODE -ne 0) { throw 'Could not archive the reviewed diagnostic source.' }
$sourceHash = (Get-FileHash -Algorithm SHA256 $sourceArchive).Hash.ToLowerInvariant()
$uploadDirectory = (ssh $SshHost 'umask 077; mktemp -d /tmp/buh-audit-upload.XXXXXXXXXX').Trim()
if ($LASTEXITCODE -ne 0 -or $uploadDirectory -notmatch '^/tmp/buh-audit-upload\.[A-Za-z0-9]+$') {
    throw 'Could not create a private diagnostic upload directory.'
}
scp $sourceArchive "${SshHost}:${uploadDirectory}/source.tar"
if ($LASTEXITCODE -ne 0) { throw 'Diagnostic upload failed.' }
$rootScript = @'
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import tempfile

try:
    upload, expected_source, commit = sys.argv[1:]
    os.umask(0o077)
    with (Path(upload) / "source.tar").open("rb") as stream:
        archive = stream.read(128 * 1024 * 1024 + 1)
    if len(archive) > 128 * 1024 * 1024 or hashlib.sha256(archive).hexdigest() != expected_source:
        raise SystemExit("Diagnostic source archive is oversized or changed")
    stage = Path(tempfile.mkdtemp(prefix="buh-recovery-audit-" + commit[:8] + "-", dir="/root"))
    source = stage / "source"
    source.mkdir(mode=0o700)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        members = tar.getmembers()
        if len(members) > 10000 or any(
            not (m.isfile() or m.isdir()) or PurePosixPath(m.name).is_absolute()
            or ".." in PurePosixPath(m.name).parts for m in members
        ):
            raise SystemExit("Diagnostic source archive contains unsafe entries")
        tar.extractall(source, filter="data")
    expected_archive = "0d1dec44c94a5cad8c6568b195a206954d3f8b660c304af418de4f6e12273ad0"
    verification_archive = None
    for pattern in (
        "buh-classifier-repair-*/v062-verification-only.tar.gz",
        "buh-log-repair-*/v062-verification-only.tar.gz",
        "buh-worker-repair-*/v062-verification-only.tar.gz",
    ):
        candidates = list(Path("/root").glob(pattern))
        if len(candidates) > 32:
            raise SystemExit("Unexpected verification archive inventory; report without changing it")
        for candidate in candidates:
            if candidate.is_symlink() or candidate.stat().st_size > 8 * 1024 * 1024:
                continue
            if hashlib.sha256(candidate.read_bytes()).hexdigest() == expected_archive:
                verification_archive = candidate
                break
        if verification_archive is not None:
            break
    if verification_archive is None:
        raise SystemExit("Pinned retained verification archive is unavailable; preserve resources")
    runtime = hashlib.sha256((source / "ops/deploy/docker_host.py").read_bytes()).hexdigest()
    result = subprocess.run(
        ["/usr/bin/python3", "-B", "-P", "-m", "ops.deploy.recovery_audit",
         "--runtime-sha256", runtime, "--archive", str(verification_archive)],
        cwd=source, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "PYTHONPATH": str(source)},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=1800,
    )
    if not result.stdout.strip():
        raise SystemExit("Diagnostic process returned no structured report; preserve resources")
    print(result.stdout, end="", flush=True)
    (stage / "diagnostic-report.json").write_text(result.stdout, encoding="utf-8")
    raise SystemExit(result.returncode)
except SystemExit as error:
    if isinstance(error.code, str):
        print(json.dumps({"result": "diagnostic-blocked", "preserve_resources": True,
                          "error": error.code[:200]}), flush=True)
        raise SystemExit(1)
    raise
except Exception as error:
    print(json.dumps({"result": "diagnostic-blocked", "preserve_resources": True,
                      "error_type": type(error).__name__,
                      "error": "Diagnostic staging or collection failed; preserve resources"}), flush=True)
    raise SystemExit(1)
'@
$reportPath = Join-Path $taskDirectory 'diagnostic-report.txt'
$rootScript | ssh $SshHost "sudo -n timeout 1900s /usr/bin/python3 -B - $uploadDirectory $sourceHash $ReviewedCommit" |
    Tee-Object -FilePath $reportPath -Variable buhAuditOutput
$auditExitCode = $LASTEXITCODE
# Copy the report on failure too: finding blockers is a useful diagnostic result.
$buhAuditOutput | Set-Clipboard
Write-Host "Diagnostic output copied. Local report: $reportPath"
if ($auditExitCode -ne 0) {
    Write-Host 'Diagnosis found blockers or incomplete evidence. Paste the copied output into Work.'
} else {
    Write-Host 'Diagnostic probes passed. Paste the copied output into Work for review.'
}
Write-Host 'The receiver, deployment state, backups and recovery resources were preserved.'
