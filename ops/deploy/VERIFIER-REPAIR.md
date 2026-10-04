# Scoped retained-recovery verifier repair

This correction changes receiver verification only; it does not install an
application, build or alter an immutable release, refresh tokens, update Member
Audit, or initiate a new deployment.

`collect_retained_log_findings.py` is a separate, read-only diagnostic for the
remaining selected asset-name 404 and handled callback traceback. It uses the
already-staged, digest-pinned verifier and existing deployment lock. It reads
the selected character's current asset status, token/link metadata and asset relationships;
independently checks at most 16 retained asset payloads; and collects complete
selected traceback records, session-hash-correlated login continuation and
HTTP statuses. It checks preservation of the completed 87/348 recovery,
excluded 12/61 state, Structures and retained recovery resources. Database reads
use `SET TRANSACTION READ ONLY`. Token refresh and update/recovery/installation
operations are absent. Report or evidence bounds cannot grant recovery
eligibility. The diagnostic does not classify either finding as recovered.

New durable plans retain restart counts and their exact service/container
mapping. Old plans fail closed unless a root-private `VERIFIER-REVIEW.json`
reconstructs that mapping from Work's reviewed retained attempt and host evidence.
The review binds the original plan digest, attempt, full container IDs, images,
counts and start times. It never samples current counts as replacement evidence.
The live inspector compares all those fields again; an unexpected restart,
replacement, service swap, unhealthy state, OOM or image change still fails.

After the installed memory healthcheck and actual recycle causes have been
reviewed, schema 2 adds `worker_memory_recycles` to this private review. It binds
the original reviewed evidence digest, boot identity, Docker/containerd process
start identities, diagnostic store identity, exact general-worker IDs/names and
the independently verified script digest. Original counts and start times are
retained. Non-worker services, replacement workers and normal deployments retain
strict restart rules.

For every additional worker restart, the verifier reads the complete retained
Docker journal and requires consecutive restart counts from that original
baseline. Each event independently needs four installed memory-check executions,
the third failure's unhealthy transition, the next check followed immediately by
Celery warm shutdown, exit 0, automatic restart with `manualRestart=false`, the
same container/image and a later healthy transition. It reads complete bounded
application and host journal windows as well as indexed diagnostic Docker events.
Missing events, capture lag/gaps, a changed script/configuration/daemon/boot,
manual control, host-agent actions, crashes, OOM, fatal application errors and
unexplained restarts block recovery. A subsequent independently proven recycle
can pass without changing the original reviewed baseline. Every accepted event
keeps its count, timestamps and source hashes in the private recovery report and
`WORKER-RECYCLES.json`; it is not erased or put on a general log allowlist.

Docker health metadata is optional on containers without a Docker healthcheck.
The Go templates use `index .State "Health"` instead of dereferencing a missing
map key. Absence remains `none`/`null`; a present unhealthy or starting state
remains distinct. The read-only worker-restart collector retains normal runtime,
identity, image, count, OOM, exit and time evidence and rejects malformed metadata.
Hosted regressions reproduce production's strict Go missing-key failure, then
verify absent, healthy and unhealthy cases through the actual templates against
real Docker containers. Docker client versions with permissive map-key handling
are also supported; absence remains distinct from a healthy result.

`collect_worker_restart_evidence.py` reads the existing retained plan and private
diagnostic store without refreshing tokens, running healthcheck scripts or
changing application, recovery or deployment state. It captures the installed
memory-check script identity and bounded causal windows for post-baseline worker
restarts. It reports evidence for review, never replaces a baseline or grants
recovery eligibility. Unknown identity, unhealthy state, new changes during the
read, missing data, oversized windows and non-worker restarts remain blocking.

The retained `candidate-slot-start-1` plan may already have
`traffic_switch_started = true`: `_protect_static_collection_traffic` routes
through previous-version static safety slots before candidate startup. This is
admitted only while both replacement flags are false and the private reviewed
assessment includes independent `previous_slot_routing` evidence. The original
flag and plan bytes remain intact so the supported traffic-first rollback still
runs. A flag, slot name or readiness label alone cannot establish this case.

That record has exactly these fields:

- `schema_version`: integer `1`.
- `attempt_id`, `plan_sha256`, `host_evidence_sha256`: the retained attempt,
  original `hold_sha256`, and independently reviewed host inventory digest from
  the assessment. Both active and backed-up plan bytes must match that digest.
- `slots`: the complete previous-slot inventory, in retained plan order. Each row
  has exactly `name`, `container_id` (full ID), `image_id`, `restart_count` (zero),
  and `started_at`. Use reviewed evidence; never reset a baseline from the live
  inspector or reuse another attempt's slot inventory.
- `targets`, `backup_targets`: exact primary and fallback endpoints from the
  independent upstream evidence. Only the existing previous-slot/original
  Gunicorn safety route, its restored Gunicorn/previous-slot route, or its final
  original Gunicorn route are supported. Candidate, mixed and unknown routes
  fail, including candidate fallback endpoints.
- `upstream_sha256`: the SHA-256 of those exact managed upstream bytes.

The helper independently re-reads full slot identities, previous images,
restart counts and start times; all retained live service identities and runtime
state; the previous static snapshot; host and container-visible upstream bytes;
and the parsed production Nginx route and its complete upstream endpoint set.
Missing, failed or changed reads block the review. It checks the retained plan
again after these probes and retains the routing-proof digest in its private
report. This does not authorize an operation or change Discord classification.

The reviewed Discord exception is one complete nickname PATCH request, its
specific mirrored rate-limit errors, the same member's correlated one-second
retry, HTTP 204 and nickname completion within a five-second evidence window.
The verifier re-reads that window on the same retained runtime and retains a
recovered warning with hashes of both original ERROR lines. Only those two exact
lines may be accepted once in the complete retained scan. Repeated, unmatched,
permission or unknown failures remain fatal. Ordinary deployments have no review
and retain their existing strict log policy.

`run-verifier-repair.ps1` defaults to `verify`. Work supplies the exact tested
commit, launcher/helper/runtime hashes and private assessment. Verification checks
the existing lock, receiver inventory, v0.8.2 identity/routing, retained backup,
all 12 Structures owners, completed 87/348 Member Audit state and unchanged 12/61
excluded sections, then all independent restoration checks. Failed or incomplete
reads stop before activation.

The authorized `install-and-recover` operation uses the existing additive
single-file repair pattern: preserve the original runtime and INSTALL record,
atomically replace only `docker_host.py`, retain a separate receipt, and restore
the old file if activation validation fails. It invokes the supported retained
rollback API once. Fresh Structures and read-only Member Audit preservation gates
run inside its final verification before safety slots or the active hold may be
removed. The database backup and historical incident reports are retained.

The recycle reader shares the existing health-log rule for the exact DEBUG ESI
`Error` schema-model listing, including the worker's MainProcess mirror. It
retains the listing count, bounded samples and full evidence digest in each
accepted recycle. This is a schema name, not a runtime failure. Different
severity, logger, source line, process or message, and any adjacent real failure
remain blocking. Schema records never substitute for warm-shutdown evidence.
An early routing/restart rejection reports the specific blocked worker and any
already-proven recycles before stopping without activation or cleanup.

A failure produces one bounded JSON report and stops; do not replay an installer
or begin another deployment without reviewing its result. Successful recovery
permits a separate fresh preflight and the already-authorized immutable release
deployment through the existing guarded workflow.
