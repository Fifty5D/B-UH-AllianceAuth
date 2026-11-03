param(
    [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ReviewedCommit,
    [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{64}$')][string]$DockerSha256,
    [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{64}$')][string]$HelperSha256,
    [Parameter(Mandatory=$true)][ValidatePattern('^[A-Za-z0-9+/=]+$')][string]$AssessmentBase64,
    [string]$RetainedLogReportPath,
    [string]$SemanticEvidencePath,
    [string]$ReviewedSignalsPath,
    [switch]$RecoverExistingLocation,
    [string]$ReportPath,
    [switch]$PassThru,
    [ValidateSet('verify','install-and-recover')][string]$Operation='verify',
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]*$')][string]$SshHost='b-uh'
)
# verify: read-only database/host checks, with private evidence staging only.
# install-and-recover: after all read-only gates pass, replace only the receiver's
# docker_host.py with an exact backup and additive receipt, then invoke the
# existing rollback/recovery verification and safety-slot/hold completion.
# Optional separately authorized existing LOCATION recovery refreshes its one
# existing token and runs that native section once. No token deletion/replacement,
# Auth relink, other section update, or application deployment is performed here.
$ErrorActionPreference='Stop'
$assessmentBytes=[Convert]::FromBase64String($AssessmentBase64)
if ($assessmentBytes.Length -gt 65536) { throw 'Private assessment exceeds its bound.' }
$assessmentText=[Text.Encoding]::UTF8.GetString($assessmentBytes)
$assessment=$assessmentText | ConvertFrom-Json
if ($assessment.attempt_id -notmatch '^gh-[0-9]+-[0-9]+$') { throw 'Assessment attempt is invalid.' }
$localStage=Join-Path $env:TEMP ('buh-verifier-repair-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $localStage | Out-Null
$assessmentPath=Join-Path $localStage 'assessment.json'
[IO.File]::WriteAllBytes($assessmentPath,$assessmentBytes)
$assessmentSha=(Get-FileHash -Algorithm SHA256 $assessmentPath).Hash.ToLowerInvariant()
$retainedSha='-'
$semanticSha='-'
$signalsSha='-'
$uploadNames=@('docker_host.py','verifier_repair.py','assessment.json')
if ($assessment.PSObject.Properties.Name -contains 'semantic_recovery') {
    if (-not $SemanticEvidencePath -or -not $ReviewedSignalsPath) { throw 'Complete semantic evidence and reviewed signals are required.' }
    if ($RetainedLogReportPath) { throw 'Complete semantic evidence cannot use a legacy finding waiver.' }
    $semanticInfo=Get-Item -LiteralPath $SemanticEvidencePath
    $signalInfo=Get-Item -LiteralPath $ReviewedSignalsPath
    if ($semanticInfo.PSIsContainer -or $signalInfo.PSIsContainer -or $semanticInfo.Length -gt 16777216 -or
        $signalInfo.Length -gt 67108864 -or ($semanticInfo.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        ($signalInfo.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Complete evidence must use bounded regular files.' }
    $semanticSha=(Get-FileHash -Algorithm SHA256 -LiteralPath $SemanticEvidencePath).Hash.ToLowerInvariant()
    if ($semanticSha -cne $assessment.semantic_recovery.report_sha256) { throw 'Complete semantic evidence checksum differs.' }
    $semantic=[IO.File]::ReadAllText($semanticInfo.FullName) | ConvertFrom-Json
    $signalsSha=(Get-FileHash -Algorithm SHA256 -LiteralPath $ReviewedSignalsPath).Hash.ToLowerInvariant()
    if ($signalsSha -cne $semantic.signal_archive.sha256 -or $signalInfo.Length -ne $semantic.signal_archive.stored_bytes -or
        $semantic.signal_archive.filename -cne $signalInfo.Name) { throw 'Complete reviewed signal archive identity differs.' }
    Copy-Item -LiteralPath $SemanticEvidencePath -Destination (Join-Path $localStage 'semantic-recovery.json')
    Copy-Item -LiteralPath $ReviewedSignalsPath -Destination (Join-Path $localStage $signalInfo.Name)
    $uploadNames+=@('semantic-recovery.json',$signalInfo.Name)
} elseif ($SemanticEvidencePath -or $ReviewedSignalsPath -or $RecoverExistingLocation) {
    throw 'Complete semantic recovery is not authorized by this assessment.'
}
if ($RecoverExistingLocation -and $Operation -ne 'install-and-recover') { throw 'Existing LOCATION mutation requires the authorized recovery operation.' }
if ($assessment.PSObject.Properties.Name -contains 'retained_log_findings') {
    if (-not $RetainedLogReportPath) { throw 'The reviewed historical log report path is required.' }
    $reportInfo=Get-Item -LiteralPath $RetainedLogReportPath
    if ($reportInfo.PSIsContainer -or $reportInfo.Length -gt 2097152 -or
        ($reportInfo.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Historical log evidence must be a bounded normal file.'
    }
    $retainedSha=(Get-FileHash -Algorithm SHA256 -LiteralPath $RetainedLogReportPath).Hash.ToLowerInvariant()
    if ($retainedSha -notmatch '^[0-9a-f]{64}$' -or $retainedSha -cne $assessment.retained_log_findings.report_sha256) {
        throw 'Historical log report checksum mismatch; stopping before any remote operation.'
    }
    Copy-Item -LiteralPath $RetainedLogReportPath -Destination (Join-Path $localStage 'retained-log-findings.json')
    $uploadNames+=@('retained-log-findings.json')
} elseif ($RetainedLogReportPath) {
    throw 'Historical log evidence is not authorized by this assessment.'
}
foreach ($entry in @(
    @{Name='docker_host.py';Hash=$DockerSha256},
    @{Name='verifier_repair.py';Hash=$HelperSha256}
)) {
    $path=Join-Path $localStage $entry.Name
    Invoke-WebRequest -UseBasicParsing -TimeoutSec 30 -Uri (
        "https://raw.githubusercontent.com/Fifty5D/B-UH-AllianceAuth/$ReviewedCommit/ops/deploy/" + $entry.Name
    ) -OutFile $path
    if ((Get-FileHash -Algorithm SHA256 $path).Hash.ToLowerInvariant() -cne $entry.Hash) {
        throw ('Reviewed source checksum mismatch: ' + $entry.Name)
    }
}
$uploadOutput=@(ssh -o BatchMode=yes -o ConnectTimeout=20 $SshHost 'umask 077; mktemp -d /tmp/buh-verifier-upload.XXXXXXXXXX')
if ($LASTEXITCODE -ne 0 -or $uploadOutput.Count -ne 1) { throw 'Could not stage verifier source.' }
$upload=$uploadOutput[0].Trim()
if ($upload -notmatch '^/tmp/buh-verifier-upload\.[A-Za-z0-9]+$') { throw 'Unexpected upload directory.' }
foreach ($name in $uploadNames) {
    scp -o BatchMode=yes -o ConnectTimeout=20 (Join-Path $localStage $name) ($SshHost + ':' + $upload + '/' + $name)
    if ($LASTEXITCODE -ne 0) { throw 'Verifier source upload failed.' }
}
$rootScript=@'
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
operation = "unknown"
phase = "arguments"
try:
    upload, commit, docker_digest, helper_digest, assessment_digest, operation, retained_digest, semantic_digest, signals_digest, recover_location = sys.argv[1:]
    if (not re.fullmatch(r"/tmp/buh-verifier-upload\.[A-Za-z0-9]+", upload)
            or not re.fullmatch(r"[0-9a-f]{40}", commit)
            or any(not re.fullmatch(r"[0-9a-f]{64}", item)
                   for item in (docker_digest, helper_digest, assessment_digest))
            or operation not in {"verify", "install-and-recover"}
            or any(item != "-" and not re.fullmatch(r"[0-9a-f]{64}", item) for item in (retained_digest,semantic_digest,signals_digest))
            or recover_location not in {"0","1"} or recover_location == "1" and operation != "install-and-recover"):
        raise ValueError()
    os.umask(0o077)
    phase = "source_staging"
    stage = Path(tempfile.mkdtemp(prefix="buh-verifier-repair-" + commit[:8] + "-", dir="/root"))
    inputs = [
        ("docker_host.py", docker_digest, 512*1024),
        ("verifier_repair.py", helper_digest, 64*1024),
        ("assessment.json", assessment_digest, 64*1024),
    ]
    if retained_digest != "-":
        inputs.append(("retained-log-findings.json", retained_digest, 2*1024*1024))
    if semantic_digest != "-":
        inputs.append(("semantic-recovery.json",semantic_digest,16*1024*1024))
    for name, expected, limit in inputs:
        fd = os.open(Path(upload) / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ValueError()
            raw = stream.read(limit+1)
        if len(raw) != info.st_size or hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError()
        path = stage / name
        path.write_bytes(raw)
        path.chmod(0o600)
    assessment = json.loads((stage / "assessment.json").read_bytes())
    if semantic_digest != "-":
        semantic=json.loads((stage / "semantic-recovery.json").read_bytes())
        name=semantic["signal_archive"]["filename"]
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,150}\.ndjson\.gz",name)
                or assessment["semantic_recovery"]["report_sha256"] != semantic_digest
                or semantic["signal_archive"]["sha256"] != signals_digest):
            raise ValueError()
        descriptor=os.open(Path(upload)/name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(descriptor,"rb") as source:
            info=os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size>64*1024*1024 or info.st_size!=semantic["signal_archive"]["stored_bytes"]:
                raise ValueError()
            raw=source.read(64*1024*1024+1)
        if len(raw)!=info.st_size or hashlib.sha256(raw).hexdigest()!=signals_digest:
            raise ValueError()
        (stage/name).write_bytes(raw)
        (stage/name).chmod(0o600)
        (stage/"assessment-reviewed.json").write_bytes((stage/"assessment.json").read_bytes())
        assessment.pop("retained_log_findings",None)
        assessment["semantic_recovery"]["report_path"]=str(stage/"semantic-recovery.json")
        selected=json.dumps(assessment,sort_keys=True).encode()
        if len(selected)>64*1024:
            raise ValueError()
        (stage/"assessment.json").write_bytes(selected)
        assessment_digest=hashlib.sha256(selected).hexdigest()
    elif "semantic_recovery" in assessment or signals_digest!="-" or recover_location=="1":
        raise ValueError()
    if "retained_log_findings" in assessment:
        if assessment["retained_log_findings"]["report_sha256"] != retained_digest:
            raise ValueError()
        # Preserve the reviewed selection; amend only its private staging path.
        (stage / "assessment-reviewed.json").write_bytes((stage / "assessment.json").read_bytes())
        assessment["retained_log_findings"]["report_path"] = str(stage / "retained-log-findings.json")
        selected = json.dumps(assessment, sort_keys=True).encode()
        if len(selected) > 64*1024:
            raise ValueError()
        (stage / "assessment.json").write_bytes(selected)
        assessment_digest = hashlib.sha256(selected).hexdigest()
    elif retained_digest != "-":
        raise ValueError()
    if (not re.fullmatch(r"/root/buh-retained-recovery-0a3f8c09-[A-Za-z0-9_-]+/reconcile_structures_attempt.py",
                         assessment["bridge_source"])
            or not re.fullmatch(r"/root/buh-structures-pilot-badc74fd-[A-Za-z0-9_-]+", assessment["pilot_directory"])
            or not re.fullmatch(r"/root/buh-memberaudit-recovery-c6ba1ead-[A-Za-z0-9_-]+", assessment["memberaudit_directory"])):
        raise ValueError()
    phase = "reviewed_verifier_operation"
    started = True
    command = [
        "/usr/bin/python3", "-B", "-P", str(stage / "verifier_repair.py"),
        "--candidate", str(stage / "docker_host.py"), "--candidate-sha256", docker_digest,
        "--assessment", str(stage / "assessment.json"), "--assessment-sha256", assessment_digest,
        "--commit", commit, "--bridge", assessment["bridge_source"],
        "--pilot-directory", assessment["pilot_directory"], "--ma-directory", assessment["memberaudit_directory"],
        "--operation", operation,
    ]
    if recover_location == "1":
        command.append("--recover-existing-location")
    result = subprocess.run(command, env={"PATH":"/usr/sbin:/usr/bin:/sbin:/bin","PYTHONPATH":"/usr/local/lib/buh-platform-v2"},
       cwd="/", stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1500)
    phase = "validate_result"
    if not result.stdout or len(result.stdout) > 512*1024:
        raise ValueError()
    report = json.loads(result.stdout)
    if report.get("schema_version") != 1 or type(report.get("scan_complete")) is not bool:
        raise ValueError()
    report["process_exit_code"] = result.returncode
    report["staged_report_path"] = str(stage / "verifier-recovery-report.json")
    report["launcher_output"] = {"stdout_bytes":len(result.stdout), "stderr_bytes":len(result.stderr),
                                 "limit_bytes":512*1024}
    text = json.dumps(report, indent=2, sort_keys=True)
    (stage / "verifier-recovery-report.json").write_text(text+"\n", encoding="utf-8")
    print(text, flush=True)
    raise SystemExit(result.returncode)
except SystemExit:
    raise
except Exception as error:
    report = {"schema_version":1,"scan_complete":False,"failure_phase":phase,
              "error_type":type(error).__name__,"read_only":operation=="verify",
              "deployment_attempted":False,"memberaudit_mutation_attempted":False,
              "mutation_result":"unknown_requires_report_review" if started and operation!="verify" else "not_attempted",
              "preserve_recovery_resources":True}
    if stage is not None:
        report["staged_report_path"] = str(stage / "verifier-recovery-failure.json")
        (stage / "verifier-recovery-failure.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps(report,indent=2),flush=True)
    raise SystemExit(1)
'@
$remoteCommand="sudo -n timeout 1560s /usr/bin/python3 -B - $upload $ReviewedCommit $DockerSha256 $HelperSha256 $assessmentSha $Operation $retainedSha $semanticSha $signalsSha $([int][bool]$RecoverExistingLocation)"
$start=New-Object System.Diagnostics.ProcessStartInfo
$start.FileName=(Get-Command ssh).Source
$start.Arguments="-o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=15 -o ServerAliveCountMax=4 $SshHost `"$remoteCommand`""
$start.UseShellExecute=$false
$start.CreateNoWindow=$true
$start.RedirectStandardInput=$true
$start.RedirectStandardOutput=$true
$start.RedirectStandardError=$true
$process=New-Object System.Diagnostics.Process
$process.StartInfo=$start
if (-not $process.Start()) { throw 'The bounded recovery transport could not start.' }
$stdout=$process.StandardOutput.ReadToEndAsync()
$stderr=$process.StandardError.ReadToEndAsync()
$process.StandardInput.Write($rootScript)
$process.StandardInput.Close()
$clock=[Diagnostics.Stopwatch]::StartNew()
while (-not $process.WaitForExit(15000)) {
    Write-Host ('Bounded retained operation is running: ' + [int]$clock.Elapsed.TotalSeconds + ' seconds. Do not repeat it.')
    if ($clock.Elapsed.TotalSeconds -gt 1620) {
        $process.Kill()
        throw 'Transport deadline reached. Remote mutation outcome requires retained report review; do not repeat the operation.'
    }
}
$exitCode=$process.ExitCode
$text=$stdout.GetAwaiter().GetResult()
$errorText=$stderr.GetAwaiter().GetResult()
if ([Text.Encoding]::UTF8.GetByteCount($text) -gt 524288) { throw 'Structured report exceeds its transport bound; do not repeat the operation.' }
try { $report=$text | ConvertFrom-Json } catch {
    throw 'No structured report returned. Do not repeat activation/recovery or start deployment.'
}
$desktop=[Environment]::GetFolderPath('Desktop')
if (-not $desktop) { $desktop=(Get-Location).Path }
if (-not $ReportPath) { $ReportPath=Join-Path $desktop ('BUH-Verifier-Recovery-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.json') }
[IO.File]::WriteAllText($ReportPath,$text+[Environment]::NewLine,(New-Object Text.UTF8Encoding($false)))
if (-not $PassThru) { Write-Host "Saved single report: $ReportPath" }
if ($exitCode -ne 0 -or $report.scan_complete -ne $true) {
    Write-Host 'A gate failed. Preserve recovery resources and do not start another deployment.'
} elseif ($Operation -eq 'install-and-recover') {
    Write-Host 'The reviewed receiver correction and supported retained recovery completed. Application deployment was not started.'
} else {
    Write-Host 'Read-only verification passed. No installed tooling, application state or recovery resources changed.'
}
if ($PassThru) { return $report }
