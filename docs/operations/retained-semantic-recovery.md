# Complete retained recovery semantics

The schema-4 verifier consumes a digest-bound private evidence package containing
the complete reviewed operation records, capture identities, source text, native
identity inventory and evidence provenance. It replays every record, then scans
every retained application, infrastructure and safety-slot container through one
fixed current cutoff. Current read-only native database, installed-source,
retained asset-byte and correct-guild Discord membership reads supply the final
state. Production evidence and identities stay outside the repository.

Finding totals are independent of the example limit. Reports expose
`complete_scan`, `analysis_complete`, `total_candidate_findings`,
`recovered_count`, `unresolved_count`, `unknown_count`,
`counts_by_semantic_family` and `examples_truncated`. An incomplete read, missing
terminal cause, unfamiliar exception, current failure or unproven identity stops
verification. Authentication, permission, scope and revoked-credential failures
remain blocking even when an older success exists.

The contracts recognize:

- Same-operation transient failures with retained task operands and later native
  persisted success for the same owner, credential and subsystem.
- Concurrent ESI server failures across independent workers and native task
  families, with the installed server-error branch proved from source and later
  full native success across the unchanged complete affected owner inventory.
  Task operands are never inferred from timing or inventory.
- Member Audit incomplete-response refresh incidents for the same existing
  token, character and section, followed by later refresh and native section
  success with the sticky flag clear and current required scopes intact.
- Asset-name Invalid IDs responses whose installed client-error path, complete
  causal traceback, real retained payload bytes, relationships and current
  native assets section are proved.
- Discord nickname rate-limit or provider retries with the same guild/member,
  native backoff, distinct request identities, later 204/native completion and
  current membership. Unknown Member cleanup instead requires same-target
  successful deletion, absent binding and current native 404/10007.
- DEBUG callback lookup exceptions caught by the installed native fallback,
  with same-session token/view continuation and a successful normal HTTP return.

Worker memory recycle thresholds, restart baselines, OOM rejection, daemon/boot
identity and graceful drain requirements are unchanged. Scoped auxiliary
diagnostics cannot waive a failure in an application stream. Historical capture
copies count in the complete evidence report but cannot establish independent
provider failures.

## Authorized existing LOCATION repair

`run-verifier-repair.ps1 -RecoverExistingLocation -Operation install-and-recover`
requires separately authorized, digest-bound identity and exact stale-status
evidence. All current verification checks run before the first application
mutation. Only that known pending section may remain unresolved; any other
failure stops the operation.

The operation validates the existing Auth link, singleton token inventory and
required scopes, performs one bounded native token refresh with validated JWT
identity/scope claims, and invokes the installed native LOCATION section update
once with `force_update=True`. It forbids token deletion/replacement, Auth-link
writes, other token saves and other section saves in that process. Actual
location data, persisted success, cleared token-error state and advanced native
run/update timestamps are required. Other completed Member Audit recovery,
excluded older state, Structures recovery and the retained database backup are
checked again. No asynchronous retry loop is started.

The final complete semantic report and preservation checks must pass before the
supported single-file receiver activation and retained recovery can retire the
hold or clean safety resources. `SEMANTIC-RECOVERY.json` is written to the
retained backup before cleanup. A failed or unknown mutation outcome requires
report review and must never be replayed automatically.

## Post-incident task observability follow-up

Add structured start, failure and persisted-completion events to the registered
Moon Mining and Structures owner tasks in a separately tested application
release. Each event must retain the task UUID, registered task name, numeric
owner PK, corporation ID, EVE character ID, Auth-link PK, token PK, native
operation ID, provider status/exception class and native completion timestamp.
Failure events must preserve these operands independently of optional Celery
result-backend availability. Never emit credentials, session keys or OAuth
responses. Test that concurrent owners remain distinct, retries keep their
operation identity, and completion is emitted only after native persistence.

This requires application packaging and is intentionally a precise follow-up;
the existing immutable release is deployed unchanged. The current systemic
contract covers a conclusively recovered provider event without inventing its
missing historical owner operands.
