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
- `sync_evidence_at`: the timestamp of the subsequently reviewed successful
  database snapshot; `structures_owners`, `moonmining_owners` and `refineries`:
  the exact nonempty lists of internal primary keys covered by that review.

The tool accounts for every ERROR record across six complete, untruncated worker
streams. It only accepts the four reviewed ESI task names, the exact owner
nickname failures and the reviewed transient Discord responses. Extra errors,
unknown exception categories or changed worker identities block the operation.

The historical interval stays attached to the result as **not clean**. Its fixed
`until` becomes the explicit reviewed boundary. Every later log is scanned under
the ordinary strict policy. This is an incident-specific review, not an ESI
exception allowlist, an empty tail sample, or a claim of complete data history.

Each invocation also queries live database status. The same owner/refinery
population must be present and enabled, and every applicable success flag must
be true. Timestamps must follow the reviewed historical boundary and be fresh:
Structures/assets and refinery ledgers within two hours; notifications,
forwarding and Moon Mining owners within 30 minutes. A timestamp without its
success flag cannot pass. Historical evidence itself does not expire and force
recollection; live status and the complete newer log interval are always checked.

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
   This invokes `worker_recovery install-exhaustion` from the installed PR #63
   state. It backs up and replaces only `docker_host.py`, retaining the original
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
