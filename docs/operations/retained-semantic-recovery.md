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
- DEBUG callback lookup exceptions remain unresolved: recognizing the native
  caught fallback does not prove completion of its operation or continuation.

## October 14 callback proof correction (F02, R1 and R2)

The October 13 implementation was rejected by independent review. It treated
same-session annotations and a nearby normal HTTP 200 as callback completion.
Neither establishes that the response completed the failed callback's actual
operation. Matching user, client IP, user agent, session, timing or path cannot
supply that missing causal relationship. No path allowlist grants clearance.

Bounded inspection of the pinned `django-esi` 9.6.0 producer establishes that
native callback messages emit only `request.session.session_key[:5]`. The token
decorator logs "Returning to view" before calling the view. Callback receipt and
redirect messages also precede completion of the destination operation. Combined
proxy access records supply no server-owned operation or continuation identifier
linking a completed response to the original failure. There is consequently no
supported positive callback-completion example in the current record formats.

Both fresh and retained adapters preserve hashes of unambiguous native prefix
fields as `session_prefix_sha256` diagnostics only. A five-character prefix, or
its hash, is never a complete session identity. Marker-like text in client
headers, URLs and other payloads, pre-hashed text and legacy `session_sha256`
annotations cannot become authoritative correlation. Normalization clears the
legacy identity field, preserves original row digests and retains the distinction
between a partial diagnostic and proof. Unsupported markers on otherwise harmless
records do not themselves create an application failure.
Previously retained partial diagnostics can only narrow what the visible text
supports, preserving a refusal when redaction has removed a conflicting payload
marker. Supplied metadata cannot add attribution or grant identity authority.

Callback failures remain unresolved even when all nearby observations look
successful. The analyzer does not synthesize success, authentication or current
health from them; direct contract flags cannot waive the missing producer proof.
Other supported recovery contracts and their positive controls remain in force.
Exact physical copies are counted separately from independent records;
contradictory copies block analysis. Both adapters use the same bounded traceback
framing, and incomplete causal tails never become complete.
The native caught-failure header remains unresolved even when its entire expected
traceback is absent; losing all exception rows cannot erase the finding.

A future callback-completion contract would require a separately versioned,
server-owned operation/continuation identifier and a completion event emitted
after the actual operation succeeds, with an independently bound response. It
must distinguish concurrent operations and sessions without exposing session
keys, cookies, OAuth state or tokens. That prospective logging work is separate;
this correction adds or installs no logging system.

This is source qualification only. Receiver installation, retained application
recovery, historical closure and deployment readiness require their own evidence
and authorizations. Immutable v0.8.3, the existing incident hold, exclusions,
exception bindings and attempt limits remain in force. The independently reported
Prepare Release/host-hold mismatch remains a separate gate before any future merge.

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
