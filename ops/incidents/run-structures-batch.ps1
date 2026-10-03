param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ReviewedCommit,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$PilotSha256,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$HostSha256,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$RecoverySha256,
    [Parameter(Mandatory = $true)][ValidatePattern('^gh-[0-9]+-[0-9]+$')][string]$AttemptId,
    [Parameter(Mandatory = $true)][ValidateLength(1,100)][string]$PilotCharacterName,
    [Parameter(Mandatory = $true)][ValidateCount(2,11)][string[]]$CharacterNames,
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]*$')][string]$SshHost = 'b-uh'
)
# Production data mutation. Uses the existing single-owner pilot in bounded
# groups 2,3,3,3. Never deploys, cleans recovery resources or changes Member Audit.
$ErrorActionPreference = 'Stop'

function Get-StructuresBatchStopReason($Report, $ExpectedCommit, $ExpectedCharacter) {
    if ($Report.scan_complete -ne $true -or $Report.pilot_source_commit -cne $ExpectedCommit) {
        return 'host_or_source_evidence_incomplete'
    }
    if ($Report.deployment_attempted -ne $false -or $Report.retained_recovery_changed -ne $false) {
        return 'retained_deployment_changed'
    }
    foreach ($side in @($Report.before_host, $Report.after_host)) {
        if ($side.public_smoke_passed -ne $true -or $side.traffic_only_verified_previous_image -ne $true) {
            return 'production_health_not_proven'
        }
    }
    if ($Report.before_host.hold_sha256 -cne $Report.after_host.hold_sha256 -or
        $Report.before_host.active_upstream_sha256 -cne $Report.after_host.active_upstream_sha256 -or
        $Report.before_host.runtime_image_id -cne $Report.after_host.runtime_image_id) {
        return 'host_identity_changed'
    }
    if ($Report.observed_disk_growth_bytes -gt 1GB) { return 'disk_growth_exceeds_batch_bound' }
    $value = $Report.result
    if ($value.recovered -ne $true -or $value.category -cne 'recovered_existing_token' -or
        $value.refresh_succeeded -ne $true -or $value.fresh_identity_and_scopes_verified -ne $true -or
        $value.current_corporation_verified -ne $true -or $value.sync_transaction_committed -ne $true) {
        return 'existing_token_or_sync_not_recovered'
    }
    if ($value.same_token_pk -ne $true -or $value.same_auth_link_pk -ne $true -or
        $value.same_token_inventory -ne $true -or $value.tokens_deleted -ne $false -or
        $value.links_removed -ne $false -or $value.historical_records_deleted -ne $false -or
        $value.memberaudit_changes -ne $false) {
        return 'preservation_not_proven'
    }
    if ($value.after.character_name -cne $ExpectedCharacter -or $value.after.enabled -ne $true -or
        $value.after.disabled_for_no_valid_token -ne $false -or @($value.after.required_missing_scopes).Count) {
        return 'selected_character_state_not_recovered'
    }
    $expectedSteps = @('update_structures_esi', 'update_asset_esi', 'fetch_notifications_esi')
    if (@($value.steps).Count -ne 3) { return 'native_sync_steps_incomplete' }
    for ($index = 0; $index -lt 3; $index++) {
        $step = $value.steps[$index]
        if ($step.step -cne $expectedSteps[$index] -or $step.completed -ne $true -or $step.committed -ne $true) {
            return 'native_sync_steps_incomplete'
        }
    }
    if ($value.outage_snapshot.read_only -ne $true -or @($value.outage_snapshot.owners).Count -lt 1) {
        return 'outage_health_snapshot_incomplete'
    }
    foreach ($owner in $value.outage_snapshot.owners) {
        if (-not $owner.state -or $owner.same_auth_link_pk -ne $true -or
            $owner.same_owner_pk -ne $true -or $owner.same_character_id -ne $true) {
            return 'outage_roster_changed'
        }
    }
    return $null
}

function Invoke-StructuresBatchSequence($Names, $ExpectedCommit, [scriptblock]$Runner) {
    $attempts = New-Object System.Collections.Generic.List[object]
    $position = 0
    $representativePassed = $false
    foreach ($bound in @(2,3,3,3)) {
        $stage = if ($position -eq 0) { 'representative' } else { 'recovery' }
        for ($offset = 0; $offset -lt $bound -and $position -lt $Names.Count; $offset++) {
            $name = $Names[$position]
            $position++
            try {
                $outcome = & $Runner $name 'apply'
                $attempts.Add([pscustomobject]@{
                    stage = $stage; character_name = $name
                    report = $outcome.report; report_path = $outcome.report_path
                    exit_code = $outcome.exit_code
                })
                $reason = Get-StructuresBatchStopReason $outcome.report $ExpectedCommit $name
                if ($outcome.exit_code -ne 0 -and -not $reason) { $reason = 'pilot_process_failed' }
            } catch {
                $reason = 'pilot_invocation_failed_mutation_result_unknown'
                $attempts.Add([pscustomobject]@{
                    stage = $stage; character_name = $name
                    invocation_error_type = $_.Exception.GetType().Name
                })
            }
            if ($reason) {
                return [pscustomobject]@{
                    attempts = $attempts.ToArray(); stop_reason = $reason
                    representative_passed = $representativePassed
                    unattempted_count = $Names.Count - $position
                }
            }
        }
        if ($stage -eq 'representative') { $representativePassed = $true }
        if ($position -ge $Names.Count) { break }
    }
    [pscustomobject]@{
        attempts = $attempts.ToArray(); stop_reason = $null
        representative_passed = $representativePassed; unattempted_count = 0
    }
}

if (@($CharacterNames | Sort-Object -Unique).Count -ne $CharacterNames.Count -or
    $CharacterNames -contains $PilotCharacterName) { throw 'The bounded roster must be unique and exclude the recovered pilot.' }
foreach ($name in $CharacterNames) {
    if (-not $name -or $name.Length -gt 100 -or $name -match '[\x00-\x1f]') { throw 'Invalid character selection.' }
}
$taskDirectory = Join-Path $env:TEMP ('buh-structures-batch-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $taskDirectory | Out-Null
$pilotLauncher = Join-Path $taskDirectory 'run-structures-pilot.ps1'
$download = @{
    UseBasicParsing = $true; TimeoutSec = 30
    Uri = "https://raw.githubusercontent.com/Fifty5D/B-UH-AllianceAuth/$ReviewedCommit/ops/incidents/run-structures-pilot.ps1"
    OutFile = $pilotLauncher
}
Invoke-WebRequest @download
if ((Get-FileHash -Algorithm SHA256 $pilotLauncher).Hash.ToLowerInvariant() -cne $PilotSha256) {
    throw 'Pilot launcher checksum mismatch.'
}
$runner = {
    param($name, $mode)
    $arguments = @{
        ReviewedCommit = $ReviewedCommit; HostSha256 = $HostSha256; RecoverySha256 = $RecoverySha256
        AttemptId = $AttemptId; CharacterName = $name; Mode = $mode; SshHost = $SshHost; PassThru = $true
    }
    & $pilotLauncher @arguments
}
$started = [DateTime]::UtcNow.ToString('o')
$initial = $null
$sequence = $null
try {
    # Read-only current pilot/roster check, not another refresh of the recovered grant.
    $initial = & $runner $PilotCharacterName 'report'
    if ($initial.exit_code -ne 0 -or $initial.report.scan_complete -ne $true -or
        $initial.report.result.after.enabled -ne $true -or
        $initial.report.result.after.disabled_for_no_valid_token -ne $false -or
        $initial.report.result.outage_snapshot.read_only -ne $true) {
        $sequence = [pscustomobject]@{
            attempts = @(); stop_reason = 'recovered_pilot_or_roster_not_currently_proven'
            representative_passed = $false; unattempted_count = $CharacterNames.Count
        }
    } else {
        $sequence = Invoke-StructuresBatchSequence $CharacterNames $ReviewedCommit $runner
    }
} catch {
    $sequence = [pscustomobject]@{
        attempts = @(); stop_reason = 'initial_probe_failed'; representative_passed = $false
        unattempted_count = $CharacterNames.Count; error_type = $_.Exception.GetType().Name
    }
}
$attempts = @($sequence.attempts)
$latest = $initial.report
foreach ($attempt in $attempts) { if ($attempt.report) { $latest = $attempt.report } }
$snapshot = $latest.result.outage_snapshot
$report = [ordered]@{
    schema_version = 1; source_commit = $ReviewedCommit; read_only = $false
    started_at = $started; finished_at = [DateTime]::UtcNow.ToString('o')
    deployment_attempted = $false; retained_recovery_cleanup_attempted = $false; memberaudit_mutation_attempted = $false
    requested_characters = $CharacterNames; representative_passed = $sequence.representative_passed
    stop_reason = $sequence.stop_reason; unattempted_count = $sequence.unattempted_count
    recovered_in_this_batch = @($attempts | Where-Object { $_.report.result.recovered -eq $true }).Count
    initial_pilot_report = $initial.report; attempts = $attempts; latest_outage_snapshot = $snapshot
    latest_host_evidence = $latest.after_host
}
$desktop = [Environment]::GetFolderPath('Desktop')
if (-not $desktop) { $desktop = (Get-Location).Path }
$reportPath = Join-Path $desktop ('BUH-Structures-Batch-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.json')
[IO.File]::WriteAllText($reportPath, ($report | ConvertTo-Json -Depth 40), (New-Object Text.UTF8Encoding($false)))
$compact = [ordered]@{
    source_commit = $ReviewedCommit; representative_passed = $sequence.representative_passed
    stop_reason = $sequence.stop_reason; unattempted_count = $sequence.unattempted_count
    recovered_in_this_batch = $report.recovered_in_this_batch
    memberaudit_sticky_characters = $snapshot.memberaudit_sticky_characters
    memberaudit_sticky_sections = $snapshot.memberaudit_sticky_sections
    attempts = @($attempts | ForEach-Object {
        $value = $_.report.result
        [ordered]@{
            name = $_.character_name; stage = $_.stage; owner_pk = $value.after.owner_pk
            token_pk = $value.after.token_pk; category = $value.category; recovered = $value.recovered
            refresh_succeeded = $value.refresh_succeeded; same_token_pk = $value.same_token_pk
            same_auth_link_pk = $value.same_auth_link_pk; same_token_inventory = $value.same_token_inventory
            sync_committed = $value.sync_transaction_committed
            before_notification_count = $value.before.notification_count
            after_notification_count = $value.after.notification_count
            failure_phase = $value.failure_phase; exception_types = $value.exception_types
            oauth_codes = $value.oauth_codes; http_statuses = $value.http_statuses
            host_guard_reason = $_.report.guard_reason; invocation_error_type = $_.invocation_error_type
        }
    })
    owners = @($snapshot.owners | ForEach-Object {
        $state = $_.state
        [ordered]@{
            owner_pk = $_.owner_pk; name = $state.owner_name; character = $state.character_name
            active = $state.owner_active; enabled = $state.enabled; up = $state.owner_is_up
            freshness = $state.freshness; timestamps = $state.owner_timestamps
            forwarding_at = $state.forwarding_last_update_at
            token_pk = $_.token_pk; category = $_.category
        }
    })
    host = if ($latest.after_host) {
        [ordered]@{
            platform_version = $latest.after_host.platform_version
            hold_sha256 = $latest.after_host.hold_sha256; hold_phase = $latest.after_host.hold_phase
            journal_state = $latest.after_host.journal_state
            traffic_only_verified_previous_image = $latest.after_host.traffic_only_verified_previous_image
            public_smoke_passed = $latest.after_host.public_smoke_passed
            disk = $latest.after_host.disk; cpu_count = $latest.after_host.cpu_count
            load_average = $latest.after_host.host_load_average
            memory_available_bytes = $latest.after_host.memory_available_bytes
        }
    } else { $null }
}
$compactText = $compact | ConvertTo-Json -Depth 20 -Compress
if ($compactText.Length -le 20000) { Set-Clipboard -Value $compactText; Write-Host 'Combined compact result copied. Paste it into ChatGPT.' }
Write-Host "Saved combined report: $reportPath"
if ($sequence.stop_reason) {
    Write-Host "Stopped: $($sequence.stop_reason). No further apply operations were started."
} else {
    Write-Host 'All requested existing-token syncs completed. Forwarding/status freshness remains separately reported.'
}
