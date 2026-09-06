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
- A legacy host may start from a newer published Platform v2 release only when
  the release explicitly opts into skipping uninstalled v2 predecessors and
  still names the exact reviewed legacy baseline. Once Platform v2 is live,
  every release must name the verified live release as its direct predecessor.
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

## Schema-v2 managed web-switch prerequisite

Schema v2 is a deliberate host transition, not an application-release side
effect. A production `deploy platform-v2` request using a schema-v1 receiver
configuration is rejected before Compose, migrations, or traffic can change.
The new receiver can still run one no-change preflight against an archived
schema-v1 configuration during its own upgrade: it synthesizes the five minimum
health routes and skips the not-yet-provisioned managed upstream. That
compatibility cannot fall back to the former force-recreate deployment path.

Before the first schema-v2 preflight, provision the following from the exact
reviewed checkout in a separately approved maintenance window:

1. Back up the active Nginx configuration, the literal `.env` `COMPOSE_FILE`
   value and every named Compose file, and the receiver configuration. Keep all
   existing overlays in their existing order.
2. Create `conf/buh-platform-v2/nginx/upstream.conf` below the AllianceAuth
   application directory with these initial reviewed bytes, which still route
   only to the current live service:

   ```nginx
   # Managed by the B-UH Platform v2 receiver.
   upstream buh_platform_v2_active {
       least_conn;
       server allianceauth_gunicorn:8000 max_fails=1 fail_timeout=5s;
   }
   ```

3. Add a dedicated Compose overlay that mounts the containing directory into
   the existing Nginx service, then append that overlay to the literal
   `COMPOSE_FILE` value. Mount the directory, not the individual file: the
   receiver replaces the file atomically and a single-file bind mount would
   keep the old inode. The relevant overlay contract is:

   ```yaml
   services:
     nginx:
       volumes:
         - ./conf/buh-platform-v2/nginx:/etc/nginx/buh-platform-v2:ro
   ```

4. Inside the Nginx `http` context, include
   `/etc/nginx/buh-platform-v2/upstream.conf` exactly once. Change the existing
   AllianceAuth proxy location to contain exactly one
   `proxy_pass http://buh_platform_v2_active;`; no other `proxy_pass` may occur
   in an `auth.b-uh.com` server block. Validate the complete explicit
   Compose file set, confirm the host/container upstream SHA-256 values match,
   run `nginx -t`, reload Nginx, and verify `/`, `/account/login/`, `/moon-tax/`,
   `/structure-operations/`, and `/mining-analytics/`. Do not restart Nginx; if any
   check fails, restore the backed-up configuration while the old service stays
   live.
5. Derive a canonical schema-v2 receiver configuration from
   `receiver-config.example.json`. Keep `fatal_log_allowlist` empty unless a
   narrowly reviewed fingerprint has its own regression test. Validate this
   configuration from the exact reviewed source before using the receiver
   upgrade helper.

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

## Production bootstrap (one time, not automatic)

Do this only after the pull request and hosted checks pass. It changes the SSH
forced-command boundary and therefore requires a separately reviewed maintenance
window.

1. Update the existing observer bridge from the exact reviewed `main` checkout,
   then comment `/fingerprint platform-v2` in diagnostics issue #1. The guarded
   command reports only the Compose service names, current `AA_DOCKER_TAG`, its
   resolved repository digest, and Docker versions. It never emits other
   environment values. Confirm the Compose filename, service names, existing
   legacy receiver path, and runtime result without copying environment contents
   or credentials into GitHub, chat, or diagnostics.
2. Resolve the current production image to its immutable repository digest.
   Add that exact `name:tag@sha256:...` as
   `production_runtime.base_image` in `platform/compatibility.toml`, run all
   source checks, and publish a new immutable Platform release. A tag-only image
   is rejected.
3. Complete the schema-v2 managed web-switch prerequisite above while its
   initial upstream still selects the current live Gunicorn service. Copy
   `receiver-config.example.json` to a root-only configuration, adjust only the
   verified path/service values, serialize it as canonical JSON, and validate it
   with `ReceiverConfig.load` from the exact reviewed checkout.
4. Run the reviewed Windows PowerShell receiver-upgrade helper documented below.
   It transfers only the exact commit, runs the receiver/deployment tests before
   activation, takes a root-only backup, installs atomically, runs the supplied
   no-change preflight, and restores the prior receiver if installation or
   preflight fails. It preserves the legacy receiver and does not alter either
   identity's public-key material.
5. Review the existing deploy user's forced-command entry locally. Retain its
   existing public-key material and restrictions, changing only its fixed command
   to invoke:

   ```text
   /usr/local/sbin/buh-deploy-dispatch /usr/local/sbin/buh-moon-tax-platform-remote
   ```

   The dispatcher routes the old `deploy moon-tax` command unchanged and adds
   only the two Platform v2 commands. Never send the key material anywhere.
6. Confirm the helper's exact-release no-change preflight and sanitized observer
   diagnostics passed. Then run **Deploy Platform v2** in `preflight` mode for
   the exact release intended for production with confirmation
   `PREFLIGHT PLATFORM V2`. This validates the upgraded receiver; it does not
   authorize or perform a deployment.
7. Stop the infrastructure window without deploying. Routine application
   releases proceed through **Prepare Release**, its exact feature/readiness and
   no-change preflight evidence, Anthony's single ChatGPT approval on the release
   synchronization PR, and **Deploy Production**. Direct `deploy` mode in
   **Deploy Platform v2** is emergency-only: Anthony must first record an explicit
   emergency approval for the exact immutable release and its already-passing
   validation/preflight evidence. It must never be used to bypass a failed,
   missing, stale, or expired normal-flow gate.

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

The configuration and preflight archive must be root-owned mode `0600` beneath
root-owned directories with no group/world write access. The legacy receiver and
all its parent directories must be root-owned and not group/world writable; the
receiver itself must remain executable. Root pins configuration, request, and
legacy bytes before running tests and verifies their SHA-256 identities again
before staging and preflight. The transaction takes and verifies a root-only
backup of every managed receiver target, retains exact recovery code and config,
installs through verified sibling paths, and restores the backup after any
catchable installation or preflight failure.

From a clean Windows checkout at the exact reviewed commit, the one-time command
is below. Replace the SHA and root-readable VPS request path only after review;
the request path is not request content, and PowerShell history must not contain
a token or key. The helper uses the existing `b-uh` SSH host alias:

```powershell
.\setup\Upgrade-BUH-PlatformV2Receiver.ps1 `
  -ReviewedCommit '<REVIEWED_40_HEX_SHA>' `
  -ConfigPath '/etc/buh-platform-v2/receiver.json' `
  -LegacyReceiver '/usr/local/sbin/buh-moon-tax-platform-remote' `
  -PreflightRequest '/root/platform-v2-preflight.tar.gz'
```

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
