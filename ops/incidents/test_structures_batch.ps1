$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $PSScriptRoot 'run-structures-batch.ps1'), [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Batch script parse failed.' }
$functions = @($ast.FindAll({
    param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst]
}, $false))
if ($functions.Count -ne 2) { throw 'Unexpected batch policy functions.' }
foreach ($function in $functions) { . ([scriptblock]::Create($function.Extent.Text)) }
$testCommit = 'a' * 40
function New-TestReport($name) {
    [ordered]@{
        scan_complete = $true; pilot_source_commit = $testCommit
        deployment_attempted = $false; retained_recovery_changed = $false; observed_disk_growth_bytes = 4096
        before_host = @{ public_smoke_passed = $true; traffic_only_verified_previous_image = $true
            hold_sha256 = 'synthetic-hold'; active_upstream_sha256 = 'synthetic-route'; runtime_image_id = 'synthetic-image' }
        after_host = @{ public_smoke_passed = $true; traffic_only_verified_previous_image = $true
            hold_sha256 = 'synthetic-hold'; active_upstream_sha256 = 'synthetic-route'; runtime_image_id = 'synthetic-image' }
        result = @{
            recovered = $true; category = 'recovered_existing_token'; refresh_succeeded = $true
            fresh_identity_and_scopes_verified = $true; current_corporation_verified = $true
            sync_transaction_committed = $true; same_token_pk = $true; same_auth_link_pk = $true; same_token_inventory = $true
            tokens_deleted = $false; links_removed = $false; historical_records_deleted = $false; memberaudit_changes = $false
            after = @{ character_name = $name; enabled = $true; disabled_for_no_valid_token = $false; required_missing_scopes = @() }
            steps = @(
                @{step='update_structures_esi';completed=$true;committed=$true}
                @{step='update_asset_esi';completed=$true;committed=$true}
                @{step='fetch_notifications_esi';completed=$true;committed=$true}
            )
            outage_snapshot = @{read_only=$true;owners=@(@{
                state=@{owner_active=$true};same_auth_link_pk=$true;same_owner_pk=$true;same_character_id=$true
            })}
        }
    } | ConvertTo-Json -Depth 20 | ConvertFrom-Json
}
$report = New-TestReport 'Synthetic 1'
if (Get-StructuresBatchStopReason $report $testCommit 'Synthetic 1') { throw 'Valid evidence rejected.' }
foreach ($flag in @('same_token_pk','same_auth_link_pk','same_token_inventory','refresh_succeeded',
                    'sync_transaction_committed','fresh_identity_and_scopes_verified','current_corporation_verified')) {
    $report = New-TestReport 'Synthetic 1'
    $report.result.$flag = $false
    if (-not (Get-StructuresBatchStopReason $report $testCommit 'Synthetic 1')) { throw "Unsafe flag accepted: $flag" }
}
foreach ($flag in @('tokens_deleted','links_removed','historical_records_deleted','memberaudit_changes')) {
    $report = New-TestReport 'Synthetic 1'
    $report.result.$flag = $true
    if (-not (Get-StructuresBatchStopReason $report $testCommit 'Synthetic 1')) { throw "Unsafe change accepted: $flag" }
}
$report = New-TestReport 'Synthetic 1'
$report.after_host.hold_sha256 = 'changed'
if (-not (Get-StructuresBatchStopReason $report $testCommit 'Synthetic 1')) { throw 'Changed retained hold accepted.' }
$report = New-TestReport 'Synthetic 1'
$report.result.steps[1].committed = $false
if (-not (Get-StructuresBatchStopReason $report $testCommit 'Synthetic 1')) { throw 'Rolled-back step accepted.' }
$report = New-TestReport 'Synthetic 1'
$report.result.outage_snapshot = $null
if (-not (Get-StructuresBatchStopReason $report $testCommit 'Synthetic 1')) { throw 'Missing snapshot accepted.' }

$names = @(1..11 | ForEach-Object { 'Synthetic ' + $_ })
$script:seen = New-Object System.Collections.Generic.List[string]
$healthyRunner = {
    param($name, $mode)
    if ($mode -ne 'apply') { throw 'Unexpected policy mode.' }
    $script:seen.Add($name)
    [pscustomobject]@{report=(New-TestReport $name);exit_code=0;report_path='synthetic-report'}
}
$result = Invoke-StructuresBatchSequence $names $testCommit $healthyRunner
if ($result.stop_reason -or $result.attempts.Count -ne 11 -or -not $result.representative_passed -or $result.unattempted_count) {
    throw 'Healthy bounded sequence failed.'
}
if (@($result.attempts | Where-Object stage -eq 'representative').Count -ne 2) { throw 'Representative bound failed.' }

$script:seen.Clear()
$failedRunner = {
    param($name, $mode)
    $script:seen.Add($name)
    $report = New-TestReport $name
    if ($script:seen.Count -eq 2) { $report.result.recovered = $false; $report.result.category = 'permanent_invalid_grant' }
    [pscustomobject]@{report=$report;exit_code=0;report_path='synthetic-report'}
}
$result = Invoke-StructuresBatchSequence $names $testCommit $failedRunner
if ($script:seen.Count -ne 2 -or $result.unattempted_count -ne 9 -or $result.representative_passed -or -not $result.stop_reason) {
    throw 'Failed representative batch did not stop.'
}

$script:seen.Clear()
$unknownRunner = {
    param($name, $mode)
    $script:seen.Add($name)
    throw 'synthetic-exception-secret'
}
$result = Invoke-StructuresBatchSequence $names $testCommit $unknownRunner
if ($script:seen.Count -ne 1 -or $result.unattempted_count -ne 10 -or -not $result.stop_reason) {
    throw 'Unknown mutation outcome did not stop.'
}
if (($result | ConvertTo-Json -Depth 20) -match 'synthetic-exception-secret') { throw 'Exception message leaked.' }
Write-Host 'PASS: valid evidence, preservation gates, committed steps, bounded sequence, representative failure, unknown-outcome stop.'
