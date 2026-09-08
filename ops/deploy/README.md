# Platform v2 production receiver

This directory contains the separately gated Platform v2 deployment path. It
does not replace or modify the legacy Moon Tax receiver during repository tests,
release building, or installation. Production activation is a distinct,
reviewed host operation. Platform v2 is the current production deployment
generation; the legacy path remains only for rollback continuity until the
required production deployment and rollback drill have both succeeded.

## Consistent backup evidence

The receiver holds a MariaDB global read lock while it takes the database dump
and reads the evidence-table counts. Background capture, retention and user writes
wait during this short phase. It releases the lock before restoring the dump in
the isolated verification container; the restored table set and row counts must
still match exactly. This avoids comparing a transaction snapshot with later
live counts.

Lock acquisition waits at most 15 seconds. An independent watchdog inside the
database container limits the client lifetime to 120 seconds, with a five-second
forced-termination grace period. A lost connection or expired lock rejects the
backup. The client cannot silently reconnect, and every normal/error path closes
the session. MariaDB documents the lock and release behavior in
[FLUSH](https://mariadb.com/docs/server/reference/sql-statements/administrative-sql-statements/flush-commands/flush)
and [UNLOCK TABLES](https://mariadb.com/docs/server/reference/sql-statements/transactions/transactions-unlock-tables).

The upgrade CI lane rehearses concurrent writes, independent restoration, a real
restore mismatch, capture errors and watchdog expiry using synthetic records.
Receiver fixes require the existing root-only `install-receiver.sh` operation;
merging application source or publishing a release does not update the host
receiver. A failed production attempt is not automatically retried.

## What the receiver guarantees

- Only the exact forced commands `preflight platform-v2` and
  `deploy platform-v2` are accepted.
- One nonblocking host `flock` serializes preflights and deployments.
- Request, receiver, release, install-plan, compatibility, and artifact fields
  are parsed fail closed; an unknown security-sensitive field is an error.
- The gzip/USTAR payload is bounded and extracted without following links or
  accepting traversal, PAX metadata, Unicode aliases, nested paths, or special
  files.
- The release must match its exact manifest SHA-256 and the configured GitHub
  repository.
- The release must name one digest-pinned production base image and the host's
  `AA_DOCKER_TAG` must equal it exactly.
- Every Compose file declared by the host's literal `.env` `COMPOSE_FILE` value
  is validated as a unique, regular in-tree file and passed explicitly to every
  preflight, deployment, health, and rollback command. The configured base file
  must remain present, so application overlays and their bind mounts cannot be
  silently dropped during container replacement.
- Every immutable release continues to name the immediately preceding ledger
  release. An established v2 host may cross the single reviewed v0.5.6 release
  gap only when the target, request archive, receiver, host baseline, retained
  evidence, and approval all carry the exact coordinated-recovery identity.
  Every ordinary deployment remains a strict direct transition.
- Existing and third-party wheels are force-reinstalled from verified bytes in
  stable Docker layers before changed owned wheels. Unchanged layers remain
  cached, while same-version corrections cannot silently retain old bytes.
- Every configured Auth service must resolve through Compose to the application
  directory and the reviewed `custom.dockerfile`; a missing, inline, parent, or
  alternate build is rejected. Each resulting image must differ from its exact
  prior image and carry release-generated labels for the platform version,
  source commit, release commit, manifest SHA-256, and production base digest.
- Preflight and deployment both build the real candidate and run package-version,
  Django, and migration-plan checks. Before building, the receiver pins every
  exact live image under an attempt-scoped rollback tag. Preflight restores and
  verifies the prior Compose references without restarting live Auth containers,
  does not rewrite the shared static manifest or current-release marker when
  their mutation stages never began, then removes temporary tags on a
  best-effort basis.
- Candidate preparation preserves the owner and mode of `custom.dockerfile`.
  The root-only backup of mounted `conf/local.py` is used only to verify its
  bytes; it is never copied over the live settings file, whose owner and mode
  must also remain unchanged throughout the attempt.
- Every running replica of each configured Auth service is discovered. Replicas
  of one logical service must share an image and Compose reference, while
  different services may have distinct image IDs from the same anchored build
  definition. Exact per-service images and replica counts are pinned during
  replacement and rollback. Restart counts for the retained Gunicorn, MariaDB,
  Redis, and Nginx containers are captured before preparation and may not
  increase; every new slot and replacement must remain at zero restarts.
- The application database is verified from its Django migration history, even
  on legacy MariaDB containers that do not expose a database-name environment
  variable. Discovery fails closed if more than one Django database is present.
  A pre-migration database dump is then restored into an isolated, memory-backed
  MariaDB container using the exact running database image. Accounting-table and
  migration row counts must match before migration begins.
- Candidate Gunicorn slots are checked before traffic and retain the old live
  Gunicorn service as an Nginx `backup` server. Nginx configuration is tested and
  atomically reloaded only after the proxy container reports the exact SHA-256
  of the newly rendered host bytes at every traffic boundary. This fails closed
  on a stale inode from an incorrect single-file bind mount. The proxy container
  is never restarted during a normal deployment. The parsed `nginx -T` tree must
  show exactly one managed upstream and one managed `proxy_pass`, with that pass
  directly inside the catch-all location of a `server_name auth.b-uh.com` virtual
  host. A decoy virtual host cannot satisfy the production-route contract.
  Previous-image slots remain
  available while the active service is replaced, preventing a candidate
  failure from exposing a raw 502.
- Celery workers are replaced at their captured replica counts. Beat is stopped
  before its replacement is started and is always scaled to exactly one. The
  health gate parses Celery's structured JSON and requires every expected worker,
  queue, and registered task rather than accepting substring matches.
- Before a candidate build or `collectstatic` can change the shared static
  manifest, the receiver snapshots its complete mapping and starts the exact
  previous-image Gunicorn slots with that manifest bind-mounted read-only. It
  primes every required route, fingerprints the full manifest, and records a
  bounded canonical aggregate of every referenced stored path, byte length, and
  exact file digest. Later checks require the same aggregate, so an overwritten
  hash-named file fails even when the path still exists. The read-only container
  copy accepts only
  Django's public `paths`/`version`/`hash` manifest shape; its root-only host
  directory prevents host access while mode `0444` lets non-root Gunicorn read
  the file mount. Before collection, an atomic Nginx reload moves
  production traffic to those static-isolated old slots, with the original live
  Gunicorn service as a backup, and public routes are checked. Static collection
  uses content-hashed manifest storage without `--clear`, so the old hashes
  remain; the complete mapping is rechecked after collection and throughout
  stabilization. Rollback stays on the isolated old slots while a root-scoped,
  same-filesystem atomic rename restores the old shared manifest with its exact
  original owner and mode, then returns traffic to restored Gunicorn. Candidate
  and restored health also resolve a known collected asset through
  `stored_name()`.
- Production traffic is exercised for the configured stabilization period
  (300 seconds by default, every 15 seconds). Each iteration verifies exact
  versions and images, replica and restart state, Django, migrations, MariaDB,
  Redis, Celery, static assets, candidate and active HTTP, all five required
  public routes, application-specific read-only checks, and new fatal logs.
- The fatal-log allowlist is empty by default. Any exception must be a structured
  `component=...|exception=...|message=...` fingerprint with a specific message;
  broad `403`, permission, `Error`, or `Exception` suppressions are rejected.
  The one known Discord permission form can be reviewed only as the full exact
  `403 Forbidden (error code: 50013): Missing Permissions` message paired with a
  concrete `Forbidden` exception and application component. The component and
  exception must occur as bounded tokens in that order and the exact message
  must end the log line. Expected findings remain visible in `HEALTH.json` and
  the sanitized observer report.
- Preflight and deployment attempts are journaled atomically with bounded,
  redacted failure text, `HEALTH.json` verification metadata, warnings, and an
  explicit rollback pass/fail result. Both the receiver lineage `current.json`
  and application-facing `CURRENT.json` are captured exactly and restored (or
  newly created pointers removed) if final publication is interrupted.
  `current.json` changes only after every stage passes. Candidate and previous
  slots are removed only after that success is durable; cleanup is best-effort,
  recorded separately, and never converts a verified release into a rollback.
  Cleanup failure retains rollback image pins and any remaining slots for an
  operator.
- Code/configuration rollback switches traffic back first, retags the pinned
  pre-attempt images, restores the exact replica topology, and verifies the
  restored site and services before removing failed or previous web slots. The
  live service remains an Nginx backup while previous slots are primary; after
  restoration, previous slots remain backups until a final active-only reload
  passes. A failed restoration check deliberately retains those slots and the
  verified backup for diagnosis. Database migrations are never reversed or
  restored automatically, and a failed deployment is never retried
  automatically.
- Catchable `SIGHUP`, `SIGINT`, and `SIGTERM` signals become deployment errors
  and enter the same guarded rollback path. Later signals are deferred while
  recovery runs. `SIGKILL` cannot be caught, so the atomic journal and manual
  recovery procedure remain authoritative after a forced host termination.

## Bounded coordinated recovery identity

`ops/deploy/coordinated-recovery.json` is the only authorization for the
installed-versus-published gap. It binds the read-only host report captured at
`2026-09-07T22:17:17.791522+00:00` (SHA-256
`df9865e121e068cc65c3e6b05cb287f1b87f05772e54ecd4c7d07fa61137fc38`),
the exact production v0.5.6 markers and running provenance, the installed
receiver source/configuration, service images and replicas, Discord owner
association, Nginx/front-proxy identities, and both sides of the one-time Nginx
mount activation. It authorizes no other host or later retry after live state
changes.

| Version | Release commit | Source commit | Manifest SHA-256 |
|---|---|---|---|
| 0.5.6 | `ce6bcf0aaa21338623aaf6d89d6bfc4d12ac51ba` | `72e900c85791869e456931c9350bef794f8f9b9b` | `db22873f967c039d12ccf82e7330767d8c6b82f91b5654ca3f95854a8520821a` |
| 0.6.0 | `ba99d9d82aff319ba073256b4841732aaf3998d2` | `2ed0188255833c99e8acf9aa72f4d584533909ac` | `26f30066f2dd50a7bf4b650c48a6e2218e3f23f9cccf05fb018cf7ae083dee1d` |
| 0.6.1 | `43234a8c0b6371fdfc62b59fa75a7980924ac3a5` | `b97955bf8998b690e8ce4fc34086f5458487ab60` | `80623060f7eb70144f89ef2b0edddc7bcdfa40ae59144f2de9178c72ea51bb33` |

The temporary receiver-upgrade request is an exact v0.6.1 **preflight** with
purpose `receiver-upgrade-preflight`. It accepts only the confirmed pre-mount
Compose order (`docker-compose.yml`, then
`docker-compose.buh-vps-health.yml`) and can use the archived schema-v1 receiver
configuration. Its result is receiver-upgrade evidence only: it cannot deploy,
publish a production-readiness marker, or change either current-release marker.

The eventual next immutable release remains a normal ledger child of v0.6.1,
but its single-use `deployment_recovery` attestation allows production to move
from verified v0.5.6 through the exact immutable chain. Its schema-v2 preflight
and deployment require the activated ordered Compose set with
`docker-compose.buh-platform-v2.yml` last. All release refs, release-parent
commits, manifest bytes, compatibility hashes, live images/replicas, and
evidence digests are reverified. A later release is direct from that installed
release and carries no recovery authorization.

The receiver upgrade itself additionally requires the exact installed source
`afd2d37e09856c34be1d18f76383898d87a384a7`, schema 1 configuration SHA-256
`b57e6db83bccbedcf944985469f4f621e0b34b4eb34274b68c2dff210444bb04`,
and reviewed installed-file hashes from the host report. Any drift stops before
the verified backup or live-path activation.

The owner setting staged in `conf/local.py` must be exactly:

```python
BUH_DISCORD_GUILD_OWNER_ID = 318985508913020930
```

Before staging, confirm that Alliance Auth username `Fifty5D` is still linked to
Discord ID `318985508913020930` in guild `1521272563626672198`. Preserve the
file's existing uid 0, gid 61000, mode `0640`, all unrelated settings, Discord
role synchronization, and the five-minute nickname schedule. During only
candidate health, worker cutover, and rollback for this verified recovery, the
health scanner reads each old worker container separately and records rather
than rejects one bounded Alliance Auth Celery record containing the exact owner
nickname retry and Discord 403/code 50013. The record must come from a named
ForkPoolWorker process whose container, restart count, exact old image, replica
count, configured guild, linked Discord ID, and Alliance Auth username still
match the confirmed baseline. Long tracebacks remain associated with their one
Celery record; duplicate, interleaved, unpaired, role, and other-member errors
fail closed. The bounded finding retains its phase, container, process, and
guild in `HEALTH.json`. Candidate output and all new-worker output remain
strict, so the exception ends as soon as the old workers are replaced; it is
available again only while verifying exact restored old workers during
rollback.

## Schema-v2 managed web-switch prerequisite

Schema v2 is a deliberate host transition, not an application-release side
effect. A production `deploy platform-v2` request using a schema-v1 receiver
configuration is rejected before Compose, migrations, or traffic can change.
The new receiver can still run one no-change preflight against an archived
schema-v1 configuration during its own upgrade: it synthesizes the five minimum
health routes and skips the not-yet-provisioned managed upstream. That
compatibility cannot fall back to the former force-recreate deployment path.

Before the first schema-v2 preflight, stage and validate the following from the
exact reviewed checkout. Staging does not authorize any live production
container change.

### Stage and validate the complete configuration

1. From `/opt/aa-docker`, reconcile the complete current Compose file set in its
   effective order. Start with `/opt/aa-docker/docker-compose.yml`, read the
   literal `.env` `COMPOSE_FILE` value, and inspect the current service or
   operator invocation for additional `-f` overlays. The running container's
   `com.docker.compose.project.config_files` label is only a creation-time
   record; do not treat it as authoritative because overlays may have been
   added later. Verify every reconciled file is a regular in-tree file, render
   the explicit full set with `docker compose ... config`, and prepare that same
   ordered set for the reviewed literal `COMPOSE_FILE` value without installing
   it yet.
2. Record `docker inspect aa-docker-nginx-1`, including its exact `.Image` ID,
   restart count, and complete mount list. Back up the active
   `conf/nginx.conf`, `.env`, every reconciled Compose file and overlay, and the
   receiver configuration while preserving owner and mode. The proposed
   configuration must retain every existing overlay and mount. In particular,
   it must retain the host `conf/nginx.conf` bind at
   `/etc/nginx/nginx.conf` and the existing static volume at
   `/var/www/myauth/static`.
3. Create `conf/buh-platform-v2/nginx/upstream.conf` below the AllianceAuth
   application directory with these initial reviewed bytes, which still route
   only to the current live service:

   ```nginx
   # Managed by the B-UH Platform v2 receiver.
   upstream buh_platform_v2_active {
       least_conn;
       server allianceauth_gunicorn:8000 max_fails=1 fail_timeout=5s;
   }
   ```

4. Add a dedicated Compose overlay that mounts the containing directory into
   the existing Nginx service, then prepare a literal `COMPOSE_FILE` value that
   appends the overlay. Do not install that value during staging. Mount the
   directory, not the individual file: the
   receiver replaces the file atomically and a single-file bind mount would
   keep the old inode. The relevant overlay contract is:

   ```yaml
   services:
     nginx:
       volumes:
         - ./conf/buh-platform-v2/nginx:/etc/nginx/buh-platform-v2:ro
   ```

5. Stage, but do not yet install, a replacement for `conf/nginx.conf`. Inside
   its `http` context, include
   `/etc/nginx/buh-platform-v2/upstream.conf` exactly once. Change the existing
   AllianceAuth proxy location to contain exactly one
   `proxy_pass http://buh_platform_v2_active;`; no other `proxy_pass` may occur
   in an `auth.b-uh.com` server block.
6. Render the proposed complete Compose set with the new mount overlay and a
   root-only temporary validation overlay that mounts the staged Nginx file at
   `/etc/nginx/nginx.conf`. Confirm the resolved Nginx image reference maps to
   the exact image ID recorded from `aa-docker-nginx-1`; do not build or pull an
   image. Use that proposed set to run a disposable, non-published Nginx
   container with `--no-deps` and execute both `nginx -t` and the reviewed
   `nginx -T` assertions. Confirm the resolved mounts still include every
   original mount plus `/etc/nginx/buh-platform-v2`. A validation failure leaves
   the running container and live configuration unchanged.
7. Derive a canonical schema-v2 receiver configuration from
   `receiver-config.example.json`. Keep `fatal_log_allowlist` empty unless a
   narrowly reviewed fingerprint has its own regression test. Validate this
   configuration from the exact reviewed source before using the receiver
   upgrade helper.

### Activate the mount once, with separate approval

The directory bind does not exist in `aa-docker-nginx-1` today. An Nginx reload
cannot add a container mount, so activating it requires one separately approved
maintenance operation after staging passes. This is not a normal Platform v2
deployment.

1. Reconfirm the backup, complete ordered Compose set, exact running Nginx image
   ID, unchanged restart count, and staged validation result. Atomically install
   the staged `conf/nginx.conf` with its original owner and mode, and activate
   the reviewed `COMPOSE_FILE` value containing the new overlay.
2. Re-render the complete explicit file set. Reconfirm that its Nginx image
   resolves locally to the recorded image ID and that all original overlays and
   mounts remain present. With an additional explicit approval for this host
   change, run the equivalent of:

   ```text
   docker compose <every verified -f file in order> up -d --no-deps --no-build --pull never --force-recreate nginx
   ```

   This command must target only the `nginx` service. It must not recreate,
   restart, build, pull, or scale any other service.
3. Require the replacement container's `.Image` to equal the recorded image ID
   and inspect its mounts. Verify the preserved `/etc/nginx/nginx.conf` and
   `/var/www/myauth/static` mounts, every other original mount, and the new
   read-only `/etc/nginx/buh-platform-v2` directory. Run `nginx -t`, confirm the
   host/container upstream SHA-256 values match, and repeat the reviewed
   `nginx -T` assertions. Verify `/`, `/account/login/`, `/moon-tax/`,
   `/structure-operations/`, and `/mining-analytics/` before declaring the
   activation successful.
4. If recreation, mount inspection, Nginx validation, or any route check fails,
   restore the backed-up `.env`, Compose files, overlays, and `conf/nginx.conf`.
   Recreate only `nginx` from the original complete file set with `--no-deps`,
   `--no-build`, and `--pull never`, using the exact recorded image. Reverify
   its image ID, original mounts, `nginx -t`, and all five routes. Do not restart
   any application, worker, database, or Redis service; retain the failure and
   restoration evidence for review.

After this one-time mount activation succeeds, routine Platform v2 deployments
must not recreate or restart Nginx. They only atomically replace the already
mounted `upstream.conf`, verify its exact bytes and parsed configuration, run
`nginx -t`, and reload Nginx at the guarded traffic-switch boundaries described
above.

The receiver then proves the managed file is a regular file, its bytes are
visible inside Nginx, and the parsed `nginx -T` scopes bind the sole named
upstream and `proxy_pass` to the production Auth catch-all route rather than an
unused decoy server. It also proves every Auth service's resolved Compose build
uses the reviewed application context and Dockerfile. Every original Compose
overlay must still be present. A v2
preflight must pass after provisioning and before any production deployment is
eligible.

## Repository validation

Run the small host-independent suite with:

```bash
python -m unittest discover -s tests/deploy -p 'test_*.py'
```

The required source workflow also reconstructs the immutable legacy v0.3.3
schema in MariaDB, seeds synthetic accounting evidence, dumps it, upgrades it,
restores the dump into a second database, upgrades that copy, and verifies the
evidence again.

## Ordered coordinated-recovery runbook

This is a reviewed sequence, not authority to merge, change the host, or deploy.
Each host-maintenance or production action requires its stated approval. Stop on
the first mismatch; never rewrite a release ref, current marker, retained backup,
or approval record to advance the sequence.

1. **Qualify the repair without merging it.** Review the coordinated repair PR,
   its final-head Validate PR/Preview UI/readiness evidence, the complete
   synthetic rehearsal, and the host-report identity above. Confirm PR #50 still
   has exact head `43234a8c0b6371fdfc62b59fa75a7980924ac3a5` and is only the
   already-published v0.6.1 history synchronization. Do not merge either PR yet.
2. **Prepare every host input offline.** From a clean Windows checkout of the
   repair's exact reviewed commit, generate the v0.6.1 bootstrap archive with the
   preparation command below. Separately stage backups of `conf/local.py`,
   `conf/nginx.conf`, `.env`, the receiver config, and every reconciled Compose
   file. Stage the exact owner ID, `bootstrap/upstream.conf`,
   `bootstrap/docker-compose.buh-platform-v2.yml`, the real Auth server's
   `auth.b-uh.com` name, include, and sole catch-all managed `proxy_pass`.
   Reconcile the actual service/manual `-f` invocation with `.env`; the confirmed
   two-file creation label is not proof that no later overlay exists.
3. **Validate the Nginx proposal without touching the running service.** Render
   the full ordered Compose set plus a temporary staged-config overlay. Require
   the Nginx service to resolve to local image
   `sha256:46ccc48fbb1f5a43167f2ee2c279c122b96eec5d976e7f4e1e0780f59a51b4d6`.
   With no published ports or dependency starts, run the equivalent of
   `docker compose <verified files> run --rm --no-deps --no-build --pull never nginx nginx -t`
   and the reviewed `nginx -T` assertions. Compare the full resolved mount set,
   proxy headers, default-server behavior, front-proxy service, Auth replicas,
   and singleton beat to the backups. Record exact restoration commands using
   the original full file set before requesting live approval.
4. **With separate receiver-maintenance approval, install and prove the reviewed
   receiver.** Keep the canonical schema-v1 config in place and the initial
   upstream on the live Gunicorn service. Stage the owner setting without
   restarting the old workers. Run the upgrade command below with the locally
   prepared archive. The helper rechecks the exact installed receiver baseline,
   transfers only the reviewed tree and archive, runs deployment tests, retains
   a verified root-only backup, installs atomically, and runs that exact
   no-change bootstrap preflight. Failure restores the prior receiver and ends
   the attempt. Success leaves v0.5.6 current markers unchanged and is not
   production readiness. Preserve both SSH keys and the legacy receiver; in the
   same separately reviewed window, change only the deploy key's forced command
   to `/usr/local/sbin/buh-deploy-dispatch /usr/local/sbin/buh-moon-tax-platform-remote`.
5. **With separate Nginx-only approval, activate the mount and schema v2.** Follow
   “Activate the mount once” above using the exact reconciled file list,
   `--no-deps --no-build --pull never --force-recreate nginx`, and no other
   service. Verify the exact image, old and new mounts, visible upstream hash,
   actual `auth.b-uh.com` server/location, `nginx -t`, and all five routes. On any
   failure, restore the saved Nginx/Compose/settings bytes and recreate only
   Nginx from the original full file set. After success, atomically install the
   validated canonical schema-v2 receiver config as root mode `0600`. A normal
   deployment from this point reloads Nginx; it never recreates it.
6. **Synchronize history before the repair merge.** With the exact separately
   reviewed history-only authorization, merge PR #50 directly onto its tested
   source. It must have no production approval marker; verify authorization is
   rejected and no deployment starts. Rebase or update the coordinated repair
   PR on that main tip, then rerun its exact-head validation, preview, rehearsal,
   and readiness publication before Work merges it. This preserves v0.6.1's
   exact commit, immutable files, ancestry, and consumed fragments.
7. **Use one fresh automatic release lifecycle.** The repair merge should plan
   the next patch release (v0.6.2 while the reviewed fragment remains the only
   change), with immediate ledger predecessor v0.6.1 and the bounded recovery
   attestation. Do not retry run 34084467513 or substitute a standalone
   preflight. Follow the new automatic run through immutable publication, main
   and sync validation, an exact schema-v2 no-change preflight, artifact
   retention, and approval-readiness publication. Keep the release sync PR on
   its tested source and do not merge another feature while approval is pending.
8. **Ask once, then monitor or stop.** Work presents the exact release/source
   commits, manifest, recovery identity, CI, preview, receiver, host-bootstrap,
   and preflight evidence to Anthony for one production approval. Only the
   existing approved merge path may record it. The deployment must retain the
   verified database backup, switch traffic before worker replacement, keep one
   beat, stabilize for at least five minutes, and publish current markers last.
   Any failure switches traffic back first, restores exact images/replicas and
   static mapping, verifies the restored site, retains the database backup, does
   not reverse migrations, and ends without an automatic retry.

Legacy v1 remains available until a complete Platform v2 production cycle and a
separately approved rollback drill have succeeded.

## Reviewed one-time receiver upgrade

Receiver changes do not ride an application release. After this PR is reviewed,
Anthony must use a clean local checkout of its exact commit and the PowerShell
helper below; never run the root upgrade script from a VPS checkout. The helper
uploads a SHA-256-verified tar snapshot and blob inventory of only the current
reviewed tree. It never uploads a Git object database, bundle, parent commit, or
deleted historical file, and it rejects credential-like tracked paths. Root
copies the upload through bounded, no-follow file descriptors, verifies the tar's
embedded commit, and verifies every extracted file's mode and Git blob ID before
running code. The root upgrade refuses every invocation outside that private
bootstrap.

The canonical VPS configuration must be root-owned mode `0600` beneath
root-owned directories with no group/world write access. The legacy receiver and
all its parent directories must be root-owned and not group/world writable; the
receiver itself must remain executable. The preparation helper creates the
request locally outside the clean checkout and proves two builds are byte-for-byte
identical. The upgrade helper uploads that exact local file into its private,
attempt-scoped transfer directory. Root copies it through a bounded no-follow
descriptor into mode `0600` storage, then pins configuration, request, and legacy
bytes before tests and verifies their SHA-256 identities again before staging and
preflight. The transaction takes and verifies a root-only backup of every managed
receiver target, retains exact recovery code and config, installs through
verified sibling paths, and restores the backup after any catchable installation
or preflight failure.

From a clean Windows checkout at the exact reviewed commit, use the commands
below. Replace only the reviewed SHA after final review; the output must not
already exist. Neither command accepts a token or key, and the upgrade helper
uses the existing `b-uh` SSH host alias:

```powershell
$ReviewedCommit = '<REVIEWED_40_HEX_SHA>'
$BootstrapArchive = Join-Path `
  ([IO.Path]::GetTempPath()) `
  "buh-platform-v061-bootstrap-$($ReviewedCommit.Substring(0, 12)).tar.gz"

.\setup\Prepare-BUH-PlatformV2Preflight.ps1 `
  -ReviewedCommit $ReviewedCommit `
  -OutputPath $BootstrapArchive

.\setup\Upgrade-BUH-PlatformV2Receiver.ps1 `
  -ReviewedCommit $ReviewedCommit `
  -ConfigPath '/etc/buh-platform-v2/receiver.json' `
  -LegacyReceiver '/usr/local/sbin/buh-moon-tax-platform-remote' `
  -PreflightRequest $BootstrapArchive
```

Retain the printed archive SHA-256, adjacent `.metadata.json`, receiver backup
path, and sanitized preflight result for review. The metadata must identify
v0.6.1 and the exact three-release recovery chain. The upgrade helper transfers
the archive itself; no pre-existing `/root/platform-v2-preflight.tar.gz` or other
VPS request path is assumed.

The helper must never be run by CI and must never print `.env`, request payloads,
credentials, database data, or raw logs. This operation upgrades receiver
infrastructure only; it does not deploy an application release. If the host must
temporarily use its archived schema-v1 configuration for upgrade viability, that
no-change preflight is permitted, but production deployment stays locked until
the managed Nginx/Compose prerequisite, canonical schema-v2 configuration, and a
new schema-v2 preflight have all passed.

### Interrupted receiver-upgrade recovery

`SIGHUP`, `SIGINT`, and `SIGTERM` trigger protected automatic rollback, and
repeat catchable signals are deferred until exact restoration finishes.
`SIGKILL`, host loss, or power loss cannot be caught, so every verified backup
retains a root-only `transaction.json`. The upgrade prints its exact backup path
immediately after that verified transaction is durable and before any live path
can change. `activation-in-progress` or `rollback-failed` with
`recovery_required: true` means the installed receiver may be mixed. Do not run
the upgrade again, change forced commands, deploy, or restore a database.

Use the exact backup path printed for that attempt—never "latest" selection or a
glob—and the same clean Windows checkout at the reviewed commit. Run the same
helper in its recovery parameter set:

```powershell
.\setup\Upgrade-BUH-PlatformV2Receiver.ps1 `
  -ReviewedCommit '<REVIEWED_40_HEX_SHA>' `
  -RecoveryBackup '/var/backups/buh-receiver-upgrade/buh-receiver-<12_SHA>-<16_HEX_ATTEMPT_ID>'
```

The helper uploads only its strict commit-bound allowlist and independently
verifies that clean reviewed source before importing the recovery module. It
never imports code from the backup. The clean verifier then validates the
transaction, backup manifest, retained recovery-source manifest and every
retained source hash before acquiring the normal production deployment lock and
changing a live path. While holding that lock, it atomically restores exact prior
receiver present/absent paths, owners, groups, modes, and bytes and verifies the
durable result. The still-loaded reviewed code then consumes and verifies any
retained application-deployment recovery plan before it records `rolled-back`.
An unresolved application rollback keeps the receiver transaction at
`rollback-failed` so this external recovery path remains available.

If this command fails, stop: keep the backup and `rollback-failed` marker for
investigation and do not retry production automatically. After it passes, verify
the restored forced command, receiver version, observer diagnostics, and a
read-only/no-change application health check before scheduling a fresh upgrade
attempt. Database migrations or backups are never reversed by this procedure.
