[CmdletBinding(DefaultParameterSetName = "Upgrade")]
param(
    [Parameter(Mandatory = $true)]
    [string]$ReviewedCommit,
    [Parameter(ParameterSetName = "Upgrade")]
    [string]$ConfigPath = "/etc/buh-platform-v2/receiver.json",
    [Parameter(ParameterSetName = "Upgrade")]
    [string]$LegacyReceiver = "/usr/local/sbin/buh-moon-tax-platform-remote",
    [Parameter(Mandatory = $true, ParameterSetName = "Upgrade")]
    [string]$PreflightRequest,
    [Parameter(Mandatory = $true, ParameterSetName = "Recover")]
    [string]$RecoveryBackup
)

$ErrorActionPreference = "Stop"
$SshTarget = "b-uh"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$CanonicalConfigPath = "/etc/buh-platform-v2/receiver.json"
$CanonicalLegacyReceiver = "/usr/local/sbin/buh-moon-tax-platform-remote"

function Assert-NativeSuccess([string]$Description) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed."
    }
}

foreach ($Command in @("git", "ssh", "scp")) {
    if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) {
        throw "Required command '$Command' is missing."
    }
}
$SshExecutable = (Get-Command ssh -ErrorAction Stop).Source
if ($ReviewedCommit -cnotmatch '^[0-9a-f]{40}$') {
    throw "ReviewedCommit must be exactly 40 lowercase hexadecimal characters."
}
$Operation = $PSCmdlet.ParameterSetName.ToLowerInvariant()
if (
    $Operation -eq "upgrade" -and
    ($ConfigPath -cne $CanonicalConfigPath -or $LegacyReceiver -cne $CanonicalLegacyReceiver)
) {
    throw "Receiver upgrade paths must match the canonical production configuration and legacy forced command."
}
$RemotePaths = if ($Operation -eq "recover") {
    @($RecoveryBackup)
}
else {
    @($ConfigPath, $LegacyReceiver)
}
foreach ($RemotePath in $RemotePaths) {
    if ($RemotePath -notmatch '^/[A-Za-z0-9._/-]+$' -or $RemotePath.Contains('/../')) {
        throw "Every remote path must be a safe Linux absolute path without spaces."
    }
}
if (
    $Operation -eq "recover" -and
    $RecoveryBackup -cnotmatch '^/var/backups/buh-receiver-upgrade/buh-receiver-[0-9a-f]{12}-[0-9a-f]{16}$'
) {
    throw "RecoveryBackup must be one exact receiver backup path printed by the upgrade."
}
$ResolvedPreflightRequest = $null
if ($Operation -eq "upgrade") {
    $ResolvedPreflightRequest = [IO.Path]::GetFullPath($PreflightRequest)
    if (-not (Test-Path -LiteralPath $ResolvedPreflightRequest -PathType Leaf)) {
        throw "PreflightRequest must name the locally prepared request archive."
    }
    $RequestSource = Get-Item -LiteralPath $ResolvedPreflightRequest -Force
    if (
        ($RequestSource.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
        $RequestSource.Length -le 0 -or
        $RequestSource.Length -gt 536870912
    ) {
        throw "The locally prepared preflight request is unsafe."
    }
}

$TopLevel = (& git -C $RepoRoot rev-parse --show-toplevel 2>$null | Out-String).Trim()
Assert-NativeSuccess "Git repository discovery"
if ([IO.Path]::GetFullPath($TopLevel) -ne [IO.Path]::GetFullPath($RepoRoot)) {
    throw "Run this helper from its original repository checkout."
}
$HeadCommit = (& git -C $RepoRoot rev-parse HEAD 2>$null | Out-String).Trim()
Assert-NativeSuccess "Git commit verification"
if ($HeadCommit -cne $ReviewedCommit) {
    throw "The local checkout does not match the reviewed commit."
}
$Dirty = (& git -C $RepoRoot status --porcelain=v1 --untracked-files=all --ignored=matching |
    Out-String).Trim()
Assert-NativeSuccess "Git cleanliness verification"
if ($Dirty) {
    throw "The local checkout contains tracked, untracked, or ignored files."
}

$TrackedPaths = @(& git -C $RepoRoot ls-tree -r --name-only --full-tree $ReviewedCommit)
Assert-NativeSuccess "Reviewed path inventory"
foreach ($TrackedPath in $TrackedPaths) {
    $Leaf = $TrackedPath -replace '^.*/', ''
    if (
        $Leaf -match '^(?:\.env(?:\..*)?|authorized_keys|id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?|.+\.(?:pem|key|p12|pfx))$'
    ) {
        throw "The reviewed commit contains a forbidden credential-like path."
    }
}

$RequiredSources = @(
    "ops/__init__.py",
    "ops/buh-github-observe-entry",
    "ops/buh-github-observe-root",
    "ops/buh-redact-diagnostics.py",
    "ops/deploy/__init__.py",
    "ops/deploy/buh-deploy-dispatch",
    "ops/deploy/buh-platform-v2-receiver",
    "ops/deploy/contracts.py",
    "ops/deploy/coordinated-recovery.json",
    "ops/deploy/deployment-request.schema.json",
    "ops/deploy/docker_host.py",
    "ops/deploy/engine.py",
    "ops/deploy/install-receiver.sh",
    "ops/deploy/receiver-config-v1.schema.json",
    "ops/deploy/receiver-config.example.json",
    "ops/deploy/receiver-config.schema.json",
    "ops/deploy/receiver.py",
    "ops/deploy/request_archive.py",
    "ops/deploy/upgrade-receiver.sh",
    "ops/deploy/receiver_upgrade.py",
    "ops/release/__init__.py",
    "ops/release/buh_release.py",
    "ops/release/recovery_policy.py",
    "platform/baselines/legacy-baseline.schema.json",
    "releases/platform/v0.5.6/RELEASE.json",
    "releases/platform/v0.6.0/RELEASE.json",
    "releases/platform/v0.6.1/INSTALL_PLAN.json",
    "releases/platform/v0.6.1/RELEASE.json",
    "setup/Upgrade-BUH-PlatformV2Receiver.ps1",
    "tests/__init__.py",
    "tests/deploy/__init__.py",
    "tests/deploy/fixtures/discord-owner-50013-mainprocess.log",
    "tests/deploy/rehearse_snapshot_backup.py",
    "tests/deploy/test_contracts.py",
    "tests/deploy/test_docker_host.py",
    "tests/deploy/test_engine.py",
    "tests/deploy/test_receiver.py",
    "tests/deploy/test_schemas.py",
    "tests/deploy/test_upgrade_receiver.py"
)
foreach ($RelativePath in $RequiredSources) {
    & git -C $RepoRoot ls-files --error-unmatch -- $RelativePath *> $null
    Assert-NativeSuccess "Tracked receiver source verification"
    $LocalPath = Join-Path $RepoRoot ($RelativePath -replace '/', [IO.Path]::DirectorySeparatorChar)
    if (-not (Test-Path -LiteralPath $LocalPath -PathType Leaf)) {
        throw "A required receiver source is missing."
    }
}

$AttemptId = [Guid]::NewGuid().ToString("N")
$LocalStage = Join-Path ([IO.Path]::GetTempPath()) "buh-receiver-upgrade-$AttemptId"
$TreeArchive = Join-Path $LocalStage "receiver-tree.tar"
$Inventory = Join-Path $LocalStage "TRACKED"
$Checksums = Join-Path $LocalStage "SHA256SUMS"
$RequestUpload = Join-Path $LocalStage "preflight-request.tar.gz"
$RemoteStage = "/tmp/buh-receiver-upgrade-$AttemptId"

New-Item -ItemType Directory -Path $LocalStage -ErrorAction Stop | Out-Null
try {
    # A Git tar archive contains only the exact reviewed tree; unlike a bundle it
    # cannot carry deleted files or any other reachable repository history.
    $InventoryLines = @(& git -C $RepoRoot ls-tree -r --full-tree $ReviewedCommit -- $RequiredSources)
    Assert-NativeSuccess "Reviewed tree inventory"
    if ($InventoryLines.Count -eq 0) {
        throw "The reviewed tree inventory is empty."
    }
    foreach ($InventoryLine in $InventoryLines) {
        if ($InventoryLine -cnotmatch '^(100644|100755) blob ([0-9a-f]{40})\t([A-Za-z0-9._/-]+)$') {
            throw "The reviewed tree contains an unsupported entry or path."
        }
    }
    [IO.File]::WriteAllText(
        $Inventory,
        ([String]::Join("`n", $InventoryLines) + "`n"),
        [Text.ASCIIEncoding]::new()
    )
    & git -C $RepoRoot archive --format=tar --output=$TreeArchive $ReviewedCommit -- $RequiredSources
    Assert-NativeSuccess "Exact reviewed tree export"

    $ArchiveHash = (Get-FileHash -LiteralPath $TreeArchive -Algorithm SHA256).Hash.ToLowerInvariant()
    $InventoryHash = (Get-FileHash -LiteralPath $Inventory -Algorithm SHA256).Hash.ToLowerInvariant()
    $ArchiveSize = (Get-Item -LiteralPath $TreeArchive).Length
    $InventorySize = (Get-Item -LiteralPath $Inventory).Length
    $ChecksumText = "$ArchiveHash  receiver-tree.tar`n$InventoryHash  TRACKED`n"
    $RequestHash = "0"
    $RequestSize = 0
    if ($Operation -eq "upgrade") {
        Copy-Item -LiteralPath $ResolvedPreflightRequest -Destination $RequestUpload -ErrorAction Stop
        $RequestHash = (Get-FileHash -LiteralPath $RequestUpload -Algorithm SHA256).Hash.ToLowerInvariant()
        $RequestSize = (Get-Item -LiteralPath $RequestUpload).Length
        if ($RequestSize -le 0 -or $RequestSize -gt 536870912) {
            throw "The pinned preflight request has an unsafe size."
        }
        $ChecksumText += "$RequestHash  preflight-request.tar.gz`n"
    }
    [IO.File]::WriteAllText(
        $Checksums,
        $ChecksumText,
        [Text.ASCIIEncoding]::new()
    )

    Write-Host "Transferring the exact reviewed receiver package to $SshTarget..." -ForegroundColor Cyan
    & ssh -T $SshTarget "umask 077 && mkdir '$RemoteStage'"
    Assert-NativeSuccess "Remote staging creation"
    $TransferFiles = @($TreeArchive, $Inventory, $Checksums)
    if ($Operation -eq "upgrade") { $TransferFiles += $RequestUpload }
    & scp -- @TransferFiles "${SshTarget}:${RemoteStage}/"
    Assert-NativeSuccess "Receiver package transfer"

    & ssh -T $SshTarget "cd '$RemoteStage' && test -f receiver-tree.tar && test ! -L receiver-tree.tar && test -f TRACKED && test ! -L TRACKED && sha256sum -c SHA256SUMS >/dev/null && test `"`$(git get-tar-commit-id < receiver-tree.tar)`" = '$ReviewedCommit'"
    Assert-NativeSuccess "Unprivileged transfer verification"

    # Root opens each upload without following links, pins its descriptor, bounds
    # and hashes the bytes into a private directory, then verifies every extracted
    # file against the exact reviewed tree inventory. No Git history is uploaded.
    $RootBootstrap = @'
set -Eeuo pipefail
umask 077
unset BASH_ENV ENV PYTHONHOME PYTHONPATH GIT_DIR GIT_WORK_TREE
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
archive_upload="$1"
archive_hash="$2"
archive_size="$3"
inventory_upload="$4"
inventory_hash="$5"
inventory_size="$6"
reviewed_commit="$7"
operation="$8"
subject="$9"
config="${10}"
legacy="${11}"
subject_size="${12}"
subject_hash="${13}"
canonical_config=/etc/buh-platform-v2/receiver.json
canonical_legacy=/usr/local/sbin/buh-moon-tax-platform-remote
root_stage="$(mktemp -d /root/buh-reviewed-receiver.XXXXXXXX)"
cleanup() { rm -rf -- "$root_stage"; }
trap cleanup EXIT
install -d -m 0700 "$root_stage/home" "$root_stage/source" "$root_stage/inputs"
cd -- "$root_stage"
if ! /usr/bin/python3 -P - "$archive_upload" "$root_stage/receiver-tree.tar" \
  "$archive_size" "$archive_hash" 536870912 \
  "$inventory_upload" "$root_stage/TRACKED" \
  "$inventory_size" "$inventory_hash" 8388608 2>/dev/null <<'PY'
import hashlib
import os
import stat
import sys

def copy_verified(source, destination, expected_size, expected_hash, maximum):
    size = int(expected_size)
    limit = int(maximum)
    if size <= 0 or size > limit:
        raise SystemExit(1)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
    flags |= getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source, flags)
    try:
        details = os.fstat(source_fd)
        if not stat.S_ISREG(details.st_mode) or details.st_size != size:
            raise SystemExit(1)
        digest = hashlib.sha256()
        output_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
        )
        try:
            copied = 0
            with os.fdopen(os.dup(source_fd), "rb", closefd=True) as incoming:
                with os.fdopen(output_fd, "wb", closefd=True) as outgoing:
                    while True:
                        block = incoming.read(1024 * 1024)
                        if not block:
                            break
                        copied += len(block)
                        if copied > size:
                            raise SystemExit(1)
                        digest.update(block)
                        outgoing.write(block)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
        except BaseException:
            try:
                os.close(output_fd)
            except OSError:
                pass
            raise
        if copied != size or digest.hexdigest() != expected_hash:
            raise SystemExit(1)
    finally:
        os.close(source_fd)

arguments = sys.argv[1:]
if len(arguments) != 10:
    raise SystemExit(1)
copy_verified(*arguments[:5])
copy_verified(*arguments[5:])
PY
then
  printf '%s\n' 'Receiver package copy verification failed.' >&2
  exit 65
fi
test "$(git get-tar-commit-id < "$root_stage/receiver-tree.tar")" = "$reviewed_commit"
tar --extract --no-same-owner --file="$root_stage/receiver-tree.tar" \
  --directory="$root_stage/source"
if ! /usr/bin/python3 -P - "$root_stage/TRACKED" "$root_stage/source" 2>/dev/null <<'PY'
import hashlib
import os
import re
import stat
import sys
from pathlib import Path

inventory = Path(sys.argv[1])
source = Path(sys.argv[2])
pattern = re.compile(
    r"^(100644|100755) blob ([0-9a-f]{40})\t([A-Za-z0-9._/-]+)$"
)
forbidden = re.compile(
    r"(?i)^(?:\.env(?:\..*)?|authorized_keys|id_(?:rsa|dsa|ecdsa|ed25519)"
    r"(?:\.pub)?|.+\.(?:pem|key|p12|pfx))$"
)
expected = {}
for line in inventory.read_text(encoding="ascii").splitlines():
    match = pattern.fullmatch(line)
    if match is None or forbidden.fullmatch(match.group(3).rsplit("/", 1)[-1]):
        raise SystemExit(1)
    mode, object_id, relative = match.groups()
    if relative in expected:
        raise SystemExit(1)
    expected[relative] = (mode == "100755", object_id)
if not expected:
    raise SystemExit(1)

actual = {}
for directory, names, files in os.walk(source, followlinks=False):
    base = Path(directory)
    for name in names:
        directory_path = base / name
        details = directory_path.lstat()
        if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
            raise SystemExit(1)
        directory_path.chmod(0o755)
    for name in files:
        path = base / name
        details = path.lstat()
        if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
            raise SystemExit(1)
        relative = path.relative_to(source).as_posix()
        data = path.read_bytes()
        header = f"blob {len(data)}\0".encode("ascii")
        executable = bool(stat.S_IMODE(details.st_mode) & 0o111)
        actual[relative] = (executable, hashlib.sha1(header + data).hexdigest())
        path.chmod(0o755 if executable else 0o644)
if actual != expected:
    raise SystemExit(1)
PY
then
  printf '%s\n' 'Receiver tree inventory verification failed.' >&2
  exit 65
fi

if [[ "$operation" == recover ]]; then
  [[ "$subject" =~ ^/var/backups/buh-receiver-upgrade/buh-receiver-[0-9a-f]{12}-[0-9a-f]{16}$ ]] || {
    printf '%s\n' 'Receiver recovery backup path is invalid.' >&2
    exit 65
  }
  [[ "$(basename -- "$subject")" == "buh-receiver-${reviewed_commit:0:12}-"* ]] || {
    printf '%s\n' 'Receiver recovery backup does not match the reviewed commit.' >&2
    exit 65
  }
  # Import only the clean, commit-bound source verified above. The retained
  # backup code is evidence and is hash-checked by this trusted implementation;
  # it is never imported before its identity has been independently verified.
  env -i \
    HOME=/root LANG=C.UTF-8 PATH=/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONPATH="$root_stage/source" PYTHONDONTWRITEBYTECODE=1 \
    /usr/bin/python3 -P -m ops.deploy.receiver_upgrade recover \
    "$reviewed_commit" "$subject"
  exit 0
fi
[[ "$operation" == upgrade ]] || {
  printf '%s\n' 'Receiver helper operation is invalid.' >&2
  exit 65
}
request="$subject"
[[ "$config" == "$canonical_config" && "$legacy" == "$canonical_legacy" ]] || {
  printf '%s\n' 'Receiver upgrade paths are not canonical.' >&2
  exit 65
}

if ! /usr/bin/python3 -P - "$request" "$root_stage/request-upload.tar" \
  "$subject_size" "$subject_hash" 536870912 2>/dev/null <<'PY'
import hashlib
import os
import stat
import sys

source, destination, expected_size, expected_hash, maximum = sys.argv[1:]
size = int(expected_size)
if size <= 0 or size > int(maximum):
    raise SystemExit(1)
flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
flags |= getattr(os, "O_NOFOLLOW", 0)
source_fd = os.open(source, flags)
try:
    details = os.fstat(source_fd)
    if not stat.S_ISREG(details.st_mode) or details.st_size != size:
        raise SystemExit(1)
    output_fd = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
        0o600,
    )
    digest = hashlib.sha256()
    copied = 0
    with os.fdopen(os.dup(source_fd), "rb", closefd=True) as incoming:
        with os.fdopen(output_fd, "wb", closefd=True) as outgoing:
            for block in iter(lambda: incoming.read(1024 * 1024), b""):
                copied += len(block)
                if copied > size:
                    raise SystemExit(1)
                digest.update(block)
                outgoing.write(block)
            outgoing.flush()
            os.fsync(outgoing.fileno())
    if copied != size or digest.hexdigest() != expected_hash:
        raise SystemExit(1)
finally:
    os.close(source_fd)
PY
then
  printf '%s\n' 'Receiver preflight-request copy verification failed.' >&2
  exit 65
fi
request="$root_stage/request-upload.tar"

if ! /usr/bin/python3 -P - "$config" "$legacy" "$request" "$root_stage/inputs" \
  2>/dev/null <<'PY'
import hashlib
import os
import stat
import sys
from pathlib import Path

def validate_parents(path):
    current = Path("/")
    for part in path.parts[1:-1]:
        current /= part
        details = current.lstat()
        if (
            not stat.S_ISDIR(details.st_mode)
            or stat.S_ISLNK(details.st_mode)
            or details.st_uid != 0
            or stat.S_IMODE(details.st_mode) & 0o022
        ):
            raise SystemExit(1)

def pin(source, destination, maximum, private, executable=False):
    path = Path(source)
    if not path.is_absolute() or ".." in path.parts:
        raise SystemExit(1)
    validate_parents(path)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        details = os.fstat(descriptor)
        mode = stat.S_IMODE(details.st_mode)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != 0
            or details.st_size <= 0
            or details.st_size > maximum
            or (private and mode != 0o600)
            or (not private and mode & 0o022)
            or (executable and not mode & 0o111)
        ):
            raise SystemExit(1)
        digest = hashlib.sha256()
        output = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o500 if executable else 0o600,
        )
        copied = 0
        with os.fdopen(os.dup(descriptor), "rb", closefd=True) as incoming:
            with os.fdopen(output, "wb", closefd=True) as outgoing:
                while True:
                    block = incoming.read(1024 * 1024)
                    if not block:
                        break
                    copied += len(block)
                    if copied > details.st_size:
                        raise SystemExit(1)
                    digest.update(block)
                    outgoing.write(block)
                outgoing.flush()
                os.fsync(outgoing.fileno())
        if copied != details.st_size:
            raise SystemExit(1)
        return digest.hexdigest()
    finally:
        os.close(descriptor)

root = Path(sys.argv[4])
hashes = {
    "config": pin(sys.argv[1], root / "config.json", 65536, True),
    "legacy": pin(sys.argv[2], root / "legacy", 16777216, False, True),
    "request": pin(sys.argv[3], root / "request.tar", 536870912, True),
}
(root / "SHA256").write_text(
    "".join(f"{hashes[name]}  {name}\n" for name in sorted(hashes)),
    encoding="ascii",
)
os.chmod(root / "SHA256", 0o600)
PY
then
  printf '%s\n' 'Receiver input pinning failed.' >&2
  exit 65
fi
export HOME="$root_stage/home"
export GIT_CONFIG_NOSYSTEM=1
export GIT_CONFIG_GLOBAL=/dev/null
export BUH_REVIEWED_COMMIT="$reviewed_commit"
export BUH_REVIEWED_INVENTORY="$root_stage/TRACKED"
export BUH_REVIEWED_INVENTORY_SHA256="$inventory_hash"
export BUH_RECEIVER_CONFIG_PATH="$config"
export BUH_PINNED_CONFIG_SHA256="$(awk '$2 == "config" {print $1}' "$root_stage/inputs/SHA256")"
export BUH_PINNED_LEGACY_SHA256="$(awk '$2 == "legacy" {print $1}' "$root_stage/inputs/SHA256")"
export BUH_PINNED_REQUEST_SHA256="$(awk '$2 == "request" {print $1}' "$root_stage/inputs/SHA256")"
export BUH_LEGACY_RECEIVER_PATH="$legacy"
"$root_stage/source/ops/deploy/upgrade-receiver.sh" "$reviewed_commit" \
  "$root_stage/inputs/config.json" "$root_stage/inputs/legacy" \
  "$root_stage/inputs/request.tar"
'@
    if ($Operation -eq "recover") {
        $OperationTarget = $RecoveryBackup
        $RootConfig = "/unused"
        $RootLegacy = "/unused"
        $OperationTargetSize = 0
        $OperationTargetHash = "0"
    }
    else {
        $OperationTarget = "$RemoteStage/preflight-request.tar.gz"
        $RootConfig = $ConfigPath
        $RootLegacy = $LegacyReceiver
        $OperationTargetSize = $RequestSize
        $OperationTargetHash = $RequestHash
    }
    $RootCommand = "/usr/bin/sudo -- /usr/bin/env -i HOME=/root LANG=C.UTF-8 PATH=/usr/sbin:/usr/bin:/sbin:/bin /bin/bash -seu -- '$RemoteStage/receiver-tree.tar' '$ArchiveHash' '$ArchiveSize' '$RemoteStage/TRACKED' '$InventoryHash' '$InventorySize' '$ReviewedCommit' '$Operation' '$OperationTarget' '$RootConfig' '$RootLegacy' '$OperationTargetSize' '$OperationTargetHash'"
    $RootProcessInfo = [Diagnostics.ProcessStartInfo]::new()
    $RootProcessInfo.FileName = $SshExecutable
    if ($null -ne $RootProcessInfo.PSObject.Properties["ArgumentList"]) {
        $RootProcessInfo.ArgumentList.Add("-T")
        $RootProcessInfo.ArgumentList.Add($SshTarget)
        $RootProcessInfo.ArgumentList.Add($RootCommand)
    }
    else {
        # Windows PowerShell 5.1 lacks ArgumentList. Every interpolated value was
        # restricted above to an ASCII token/path without quotes, so one quoted
        # OpenSSH remote-command argument is unambiguous here.
        $RootProcessInfo.Arguments = "-T $SshTarget `"$RootCommand`""
    }
    $RootProcessInfo.UseShellExecute = $false
    $RootProcessInfo.RedirectStandardInput = $true
    if ($null -ne $RootProcessInfo.PSObject.Properties["StandardInputEncoding"]) {
        $RootProcessInfo.StandardInputEncoding = [Text.UTF8Encoding]::new($false)
    }
    $RootProcess = [Diagnostics.Process]::new()
    $RootProcess.StartInfo = $RootProcessInfo
    if (-not $RootProcess.Start()) { throw "Could not start the reviewed receiver upgrade." }
    $LfBootstrap = $RootBootstrap.Replace("`r`n", "`n").Replace("`r", "`n").TrimEnd("`n") + "`n"
    $RootProcess.StandardInput.Write($LfBootstrap)
    $RootProcess.StandardInput.Close()
    $RootProcess.WaitForExit()
    if ($RootProcess.ExitCode -ne 0) { throw "Reviewed receiver $Operation failed." }
    if ($Operation -eq "recover") {
        Write-Host "Receiver recovery completed from independently verified reviewed source." -ForegroundColor Green
        Write-Host "The exact VPS backup was retained; no application release was deployed." -ForegroundColor Green
    }
    else {
        Write-Host "Receiver upgrade and no-change preflight completed." -ForegroundColor Green
        Write-Host "The verified VPS backup was retained; no application release was deployed." -ForegroundColor Green
    }
}
finally {
    # The generated path is attempt-scoped and contains only this transfer package.
    & ssh -T $SshTarget "rm -rf -- '$RemoteStage'" 2>$null | Out-Null
    Remove-Item -LiteralPath $LocalStage -Recurse -Force -ErrorAction SilentlyContinue
}
