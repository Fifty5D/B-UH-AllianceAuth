param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ReviewedCommit,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$ReceiverSha256,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$ReviewSha256,
    [Parameter(Mandatory = $true)][string]$RepositoryPath,
    [Parameter(Mandatory = $true)][string]$ReviewPath,
    [Parameter(Mandatory = $true)][string]$HistoricalLogPath,
    [ValidateSet('Verify', 'InstallAndVerify', 'Complete')][string]$Operation = 'Verify',
    [string]$Confirmation = '',
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]*$')][string]$SshHost = 'b-uh'
)
# Work supplies the reviewed commit, payload hash and private assessment hash.
# Installation and completion accept separate exact owner approval phrases.
$ErrorActionPreference = 'Stop'
if ($Operation -eq 'InstallAndVerify' -and $Confirmation -cne "INSTALL OWNER EXHAUSTION CHECK $ReceiverSha256") {
    throw 'Separate approval for this exact receiver file is required.'
}
if ($Operation -eq 'Complete' -and $Confirmation -cne "COMPLETE REVIEWED ROLLBACK gh-34713182347-1 $ReviewSha256") {
    throw 'Separate approval for this exact recovery completion is required.'
}
if ((Get-FileHash -Algorithm SHA256 $ReviewPath).Hash.ToLowerInvariant() -cne $ReviewSha256) {
    throw 'The reviewed recovery assessment changed.'
}
$review = Get-Content -Raw $ReviewPath | ConvertFrom-Json
if ((Get-FileHash -Algorithm SHA256 $HistoricalLogPath).Hash.ToLowerInvariant() -cne $review.historical_log_evidence_sha256) {
    throw 'The reviewed historical evidence changed.'
}
$taskDirectory = Join-Path $env:TEMP ('buh-reviewed-recovery-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $taskDirectory | Out-Null
git -C $RepositoryPath fetch https://github.com/Fifty5D/B-UH-AllianceAuth.git $ReviewedCommit
if ($LASTEXITCODE -ne 0) { throw 'Could not fetch the reviewed recovery commit.' }
$sourceArchive = Join-Path $taskDirectory 'source.tar'
git -C $RepositoryPath archive --format=tar --output=$sourceArchive $ReviewedCommit
if ($LASTEXITCODE -ne 0) { throw 'Could not archive the reviewed source.' }
$sourceHash = (Get-FileHash -Algorithm SHA256 $sourceArchive).Hash.ToLowerInvariant()
$installerHash = '-'
$installerCommit = '-'
if ($Operation -eq 'InstallAndVerify') {
    # Keep the previously approved installer source separate from staged checks.
    $installerCommit = $review.installer_commit
    if ($installerCommit -notmatch '^[0-9a-f]{40}$') { throw 'Approved installer commit is missing.' }
    git -C $RepositoryPath fetch https://github.com/Fifty5D/B-UH-AllianceAuth.git $installerCommit
    if ($LASTEXITCODE -ne 0) { throw 'Could not fetch the approved installer commit.' }
    $installerArchive = Join-Path $taskDirectory 'installer.tar'
    git -C $RepositoryPath archive --format=tar --output=$installerArchive $installerCommit ops
    if ($LASTEXITCODE -ne 0) { throw 'Could not archive the approved installer.' }
    $installerHash = (Get-FileHash -Algorithm SHA256 $installerArchive).Hash.ToLowerInvariant()
}
$uploadDirectory = (ssh $SshHost 'umask 077; mktemp -d /tmp/buh-review-upload.XXXXXXXXXX').Trim()
if ($LASTEXITCODE -ne 0 -or $uploadDirectory -notmatch '^/tmp/buh-review-upload\.[A-Za-z0-9]+$') {
    throw 'Could not create a private upload directory.'
}
foreach ($item in @(@($sourceArchive, 'source.tar'), @($ReviewPath, 'review.json'), @($HistoricalLogPath, 'historical-log-details.json'))) {
    scp $item[0] "${SshHost}:${uploadDirectory}/$($item[1])"
    if ($LASTEXITCODE -ne 0) { throw 'Recovery evidence upload failed.' }
}
if ($Operation -eq 'InstallAndVerify') {
    scp $installerArchive "${SshHost}:${uploadDirectory}/installer.tar"
    if ($LASTEXITCODE -ne 0) { throw 'Approved installer upload failed.' }
}
$encodedConfirmation = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Confirmation))
if (-not $encodedConfirmation) { $encodedConfirmation = '-' }
$rootScript = @'
import base64
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
    upload, source_hash, commit, runtime, review_hash, operation, encoded, installer_hash, installer_commit = sys.argv[1:]
    confirmation = "" if encoded == "-" else base64.b64decode(encoded, validate=True).decode("utf-8")
    required = {
        "Verify": "",
        "InstallAndVerify": "INSTALL OWNER EXHAUSTION CHECK " + runtime,
        "Complete": "COMPLETE REVIEWED ROLLBACK gh-34713182347-1 " + review_hash,
    }
    if operation not in required or confirmation != required[operation]:
        raise SystemExit("Operation approval differs from the reviewed action")
    os.umask(0o077)

    def uploaded(name, limit, digest):
        with (Path(upload) / name).open("rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit or hashlib.sha256(raw).hexdigest() != digest:
            raise SystemExit("Reviewed upload changed or exceeded its bound")
        return raw

    archive = uploaded("source.tar", 128 * 1024 * 1024, source_hash)
    review_raw = uploaded("review.json", 1024 * 1024, review_hash)
    review = json.loads(review_raw)
    if review["receiver_sha256"] != runtime:
        raise SystemExit("Reviewed receiver payload differs")
    historical = uploaded("historical-log-details.json", 1024 * 1024, review["historical_log_evidence_sha256"])
    stage = Path(tempfile.mkdtemp(prefix="buh-reviewed-recovery-" + commit[:8] + "-", dir="/root"))
    def extract_source(raw, destination):
        destination.mkdir(mode=0o700)
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
            members = tar.getmembers()
            if len(members) > 10000 or any(
                not (m.isfile() or m.isdir()) or PurePosixPath(m.name).is_absolute()
                or ".." in PurePosixPath(m.name).parts for m in members
            ):
                raise SystemExit("Source archive contains unsafe entries")
            tar.extractall(destination, filter="data")

    source = stage / "source"
    extract_source(archive, source)
    if hashlib.sha256((source / "ops/deploy/docker_host.py").read_bytes()).hexdigest() != runtime:
        raise SystemExit("The commit does not contain the approved receiver bytes")
    installer = None
    if operation == "InstallAndVerify":
        if installer_commit != review.get("installer_commit"):
            raise SystemExit("Installer differs from the separately approved source")
        installer = stage / "approved-installer"
        extract_source(uploaded("installer.tar", 128 * 1024 * 1024, installer_hash), installer)
        if hashlib.sha256((installer / "ops/deploy/docker_host.py").read_bytes()).hexdigest() != runtime:
            raise SystemExit("Approved installer receiver payload differs")
    (stage / "review.json").write_bytes(review_raw)
    (stage / "historical-log-details.json").write_bytes(historical)
    expected_archive = "0d1dec44c94a5cad8c6568b195a206954d3f8b660c304af418de4f6e12273ad0"
    verification_archive = None
    for pattern in (
        "buh-classifier-repair-*/v062-verification-only.tar.gz",
        "buh-log-repair-*/v062-verification-only.tar.gz",
        "buh-worker-repair-*/v062-verification-only.tar.gz",
    ):
        candidates = list(Path("/root").glob(pattern))
        if len(candidates) > 32:
            raise SystemExit("Unexpected retained verification archive inventory")
        for candidate in candidates:
            if candidate.is_symlink() or candidate.stat().st_size > 8 * 1024 * 1024:
                continue
            if hashlib.sha256(candidate.read_bytes()).hexdigest() == expected_archive:
                verification_archive = candidate
                break
        if verification_archive is not None:
            break
    if verification_archive is None:
        raise SystemExit("Pinned verification archive is unavailable; preserve resources")
    def run(module, arguments, report_name=None, *, source_root=source):
        env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "PYTHONPATH": str(source_root)}
        result = subprocess.run(
            ["/usr/bin/python3", "-B", "-P", "-m", module, *arguments],
            cwd=source_root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=1800,
        )
        if not result.stdout.strip():
            raise SystemExit("Operation returned no structured report; preserve resources")
        print(result.stdout, end="", flush=True)
        (stage / ((report_name or module.rsplit(".", 1)[-1]) + "-report.json")).write_text(result.stdout, encoding="utf-8")
        if result.returncode:
            raise SystemExit(result.returncode)
        return json.loads(result.stdout)

    print(json.dumps({"staging_directory": str(stage), "reviewed_commit": commit,
                      "installer_commit": installer_commit, "operation": operation}), flush=True)
    arguments = [
        "complete" if operation == "Complete" else "verify", "--runtime-sha256", runtime,
        "--archive", str(verification_archive), "--review", str(stage / "review.json"),
        "--review-sha256", review_hash,
    ]
    if operation == "InstallAndVerify":
        # Exercise every recovery check with the staged correction before writing
        # a receiver file. A remaining health/log blocker prevents installation.
        run("ops.deploy.reviewed_recovery", arguments + ["--before-install"], "before-install")
        receipt = run("ops.deploy.worker_recovery", [
            "install-exhaustion", "--runtime-sha256", runtime, "--confirm", confirmation,
        ], source_root=installer)
        if receipt.get("result") != "receiver-owner-exhaustion-repaired" or receipt.get("sha256") != runtime:
            raise SystemExit("Unexpected installation receipt; verification not run")
    if operation == "Complete":
        arguments += ["--confirm", confirmation]
    run("ops.deploy.reviewed_recovery", arguments)
except SystemExit as error:
    if isinstance(error.code, str):
        print(json.dumps({"result": "blocked", "preserve_resources": True, "error": error.code[:200]}), flush=True)
        raise SystemExit(1)
    raise
except Exception as error:
    print(json.dumps({"result": "blocked", "preserve_resources": True,
                      "error_type": type(error).__name__, "error": "Recovery staging or execution failed"}), flush=True)
    raise SystemExit(1)
'@
$reportPath = Join-Path $taskDirectory 'recovery-report.txt'
$rootScript | ssh $SshHost "sudo -n timeout 3700s /usr/bin/python3 -B - $uploadDirectory $sourceHash $ReviewedCommit $ReceiverSha256 $ReviewSha256 $Operation $encodedConfirmation $installerHash $installerCommit" |
    Tee-Object -FilePath $reportPath -OutVariable buhRecoveryOutput
$recoveryExitCode = $LASTEXITCODE
$buhRecoveryOutput | Set-Clipboard
Write-Host "Results copied. Local report: $reportPath"
if ($recoveryExitCode -ne 0) {
    Write-Host 'Stopped. Preserve all recovery resources and paste the copied output into Work.'
} else {
    Write-Host 'The requested operation finished. Paste the copied output into Work.'
}
