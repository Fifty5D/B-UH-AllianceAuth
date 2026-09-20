# Reviewed recovery after successful data syncs

This is the continuation for the migrated attempt `gh-34713182347-1`. The
complete diagnostic and private log inventory must be reviewed first. It does
not replay that deployment, alter immutable v0.6.2 or retire the release hold.

The retained application is Alliance Auth 5.2 with Structures 4.0.3 and Moon
Mining 3.1.0.post1. Its last nickname attempt emits both a retry warning and an
exhausted ERROR. The receiver now recognizes the complete pair only for the
already-reviewed guild owner on individually verified old workers. Every logger
family still needs its own same-second 403/code 50013 denial and retry; the
exhausted record additionally needs the exact owner URL and a bounded traceback.
Role changes, other members, unrelated errors and new workers remain strict.
The application nickname guard is already in source; this receiver correction
does not install that application update.

## Evidence and review

Keep private data out of this repository. Work prepares `review.json` containing:

- `schema_version: 1`, the exact `attempt_id`, `previous_runtime_sha256` (the
  installed PR #63 hash), and `receiver_sha256` (this reviewed source file).
- `historical_log_evidence_sha256`: SHA-256 of the original complete private
  `historical-log-details.json`, without reformatting its bytes.
- `historical_assessment: "api-errors-followed-by-successful-syncs"` and
  `historical_esi_root_cause: "not-recorded"`. The old ESI logs did not record
  their HTTP status; successful later syncs do not retroactively invent a cause.
- `current_evidence_policy: "scheduled-sync-and-retained-errors-v1"`: explicit
  acceptance of the narrowly scoped current-evidence policy below. An older
  assessment cannot silently enable it.
- `asset_recoveries`: bounded mappings from `previous_character_pk` to the stable
  `eve_character_id` verified by Work. These private identities are never committed.
- `installer_commit`: the separately approved original installer commit. The
  launcher fetches it independently of the new verification source and verifies
  that both contain exactly the approved receiver-file SHA-256.
- `sync_evidence_at`: the timestamp of the subsequently reviewed successful
  database snapshot; `structures_owners`, `moonmining_owners` and `refineries`:
  the exact nonempty lists of internal primary keys covered by that review.

The tool accounts for every ERROR record across six complete, untruncated worker
streams. It only accepts the four reviewed ESI task names, the exact owner
nickname failures and the reviewed transient Discord responses. Extra errors,
unknown exception categories or changed worker identities block the operation.

The original historical interval stays attached to the result as **not clean**.
Every byte after its fixed `until` is still scanned; the boundary is not moved
forward. Only the staged `ReviewedDockerHost` adapter uses the following policy,
only on the same six individually verified retained workers during rollback.
Ordinary deployment, candidate, infrastructure, and new-worker checks stay strict.

- The four reviewed ESI task failures need a complete HTTP-only traceback and
  successful, fresh corresponding updates **after** the error for every reviewed
  owner. The historical HTTP cause remains unknown.
- Member Audit asset failures need successful, fresh assets status for every
  active character. An explicit stable EVE identity binds a retired internal
  primary key to its current registration. A missing or disabled replacement,
  unsuccessful status, token error, or missing status row blocks recovery.
- Complete historical Discord 429/503 response records remain dependency warnings;
  this does not establish Discord recovery. Permission errors, other operations,
  malformed responses and errors during the current verification remain blocking.
  The separately reviewed exact-owner 50013 policy is unchanged.
- A structurally valid decoded ESI identity record at DEBUG severity is metadata;
  a word such as `ERROR` inside its data is not a fatal log level. Unrecognized
  fatal metadata is blocked with its payload withheld from the report.

Each accepted category retains source, count, first/last timestamps and a digest
of the complete records. Unknown failures and incomplete log reads still block.
This is evidence of subsequent successful updates, not a claim of gap-free history.

Each invocation queries current database status without queuing tasks or calling
an external API. Required owner/refinery populations and success flags are checked.
Timestamps must follow the original reviewed boundary and be fresh:

| Data | Maximum age |
| --- | --- |
| Structures / corporation assets | 2 hours |
| Notifications / forwarding / Moon Mining owners | 30 minutes |
| Refinery ledgers | Configured Moon Tax audit interval + enabled dispatch interval + 15 minutes |
| Member Audit assets | Effective assets section interval + enabled dispatch interval + 15 minutes |

The installed Moon Tax dispatcher wakes hourly but refreshes sources only when
its configured four-hour audit is due. That gives a **5 hour 15 minute** ledger
bound, not the old two-hour bound. A recent COMPLETE audit, chronological source
refresh, fresh scheduler heartbeat and ledger successes after that audit's source
request are all required. Missing, disabled, ambiguous or unsupported schedules
never silently use a default. Member Audit likewise uses its actual section
configuration instead of treating its 15-minute dispatcher as the assets cadence.
Completion re-reads sync evidence before the final rollback health checks.

Failed reports retain the current snapshot and identify each failing model,
record, field, observed age and applicable limit, alongside all independent health
and log findings. Historical evidence does not expire into another collection loop.

## Owner operations

Work first reviews the exact qualified PR head and prepares the private
assessment hash. Use `run-reviewed-recovery.ps1` from that commit with these
arguments (Work supplies concrete values and local paths):

```powershell
.\ops\deploy\run-reviewed-recovery.ps1 `
  -ReviewedCommit <reviewed-commit> -ReceiverSha256 <receiver-sha256> `
  -ReviewSha256 <assessment-sha256> -RepositoryPath <local-repository> `
  -ReviewPath <private-review.json> -HistoricalLogPath <original-log-report.json>
```

The default `Verify` operation assumes the reviewed receiver is already installed.
It stages and checks source/evidence, finds the existing immutable verification
archive by hash, checks all receipt/backup identities and collects all independent
health results. It never installs, cleans up or creates a completion receipt.
The report is saved and copied to the clipboard on success **and failure**.

1. **Install and verify**, after separate approval of the exact receiver payload:
   add `-Operation InstallAndVerify -Confirmation 'INSTALL OWNER EXHAUSTION CHECK <receiver-sha256>'`.
   First it exercises **all** recovery checks using staged code against the
   unchanged installed receiver. Any remaining blocker stops before installation.
   This invokes `worker_recovery install-exhaustion` from the separately approved
   original installer commit against the installed PR #63 state. It backs up and replaces only `docker_host.py`, retaining the original
   INSTALL and all prior additive receipts. It then runs reviewed verification.
   No services, database schema, application code or traffic are changed.
2. **Complete recovery**, only after Work reviews successful verification and
   the owner separately approves the exact assessment: add
   `-Operation Complete -Confirmation 'COMPLETE REVIEWED ROLLBACK gh-34713182347-1 <assessment-sha256>'`.
   It repeats verification, retains the exact historical files, then calls the
   normal traffic-first rollback completion. Pinned flags prohibit service
   replacement; health must pass again before safety slots and image pins are
   removed. The database backup, previous receipts and historical evidence stay.
3. Work verifies the completion receipt and resulting host state, then prepares
   retirement of the hold and the next immutable release. Those source/release
   steps are separate from these commands. A fresh production approval is still
   required before any deployment.

If installation succeeded but verification failed, use `Verify` for any approved
follow-up. Do not replay this or any older installer. Failures retain the recovery
resources and expose all independent check results; they never authorize cleanup.
