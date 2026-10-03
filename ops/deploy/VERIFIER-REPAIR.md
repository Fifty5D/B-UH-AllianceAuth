# Scoped retained-recovery verifier repair

This correction changes receiver verification only; it does not install an
application, build or alter an immutable release, refresh tokens, update Member
Audit, or initiate a new deployment.

New durable plans retain restart counts and their exact service/container
mapping. Old plans fail closed unless a root-private `VERIFIER-REVIEW.json`
reconstructs that mapping from Work's reviewed retained attempt and host evidence.
The review binds the original plan digest, attempt, full container IDs, images,
counts and start times. It never samples current counts as replacement evidence.
The live inspector compares all those fields again; an unexpected restart,
replacement, service swap, unhealthy state, OOM or image change still fails.

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

A failure produces one bounded JSON report and stops; do not replay an installer
or begin another deployment without reviewing its result. Successful recovery
permits a separate fresh preflight and the already-authorized immutable release
deployment through the existing guarded workflow.
