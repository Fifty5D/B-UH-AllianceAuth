# Current worker-identity recovery — September 12, 2026

This supersedes earlier deployment/receiver-maintenance sequences, not their
evidence. PRs #52 and #60 are merged; main is
`efdeebf9ff86a97cef18463605273a032403c4f0`. No code merge is a new production
approval. The one-use `CONTINUE APPROVED V0.6.2` dispatch is **consumed**.

## Established state and remaining evidence

- Deploy Production `34713182347/1` reached the server and state `migrated`.
  Both candidate health and restored-production verification rejected correct
  Celery replies. The attempt is failed, rollback is failed, and verification
  and cleanup are incomplete. Preserve that original journal, not a rewritten
  success record.
- Artifact `10304410685`, SHA-256
  `9420e3a1d9f0083e0d06c121f5c0c1cbd2510717f42db47c97cdaf70568d8842`,
  contains the failed attempt. Observer artifact `10304648161` from
  `34715589893/1`, SHA-256
  `d61c4027b6fe69af32b1c4aeaea700dc8ac7d5e263e13e94de787d12da2c5e9f`,
  shows six responding old workers, zero reported restarts, and retained slots
  `buh-web-candidate-gh-34713182347-1-1` and
  `buh-web-previous-gh-34713182347-1-1`. The candidate image was
  `sha256:46bbe055aa71f67e4f2c23d4f9aa89c352a5bfc115da8302d60f954bd7558a2d`.
  These observations and Work's login HTTP 200 are not complete rollback proof.
- Anthony's subsequent root-only inventory confirms the attempt-specific backup
  `/var/backups/buh-platform-v2/gh-34713182347-1`, a 27,930,704-byte database dump,
  saved static manifest/assets, and identical active/backup recovery plans at
  `359eba2816cb0af9c7bda07739f64f14d74be115b0b418db33d0d6297e81afc8`.
  Migration/static collection/traffic protection started; worker and Gunicorn
  replacement and marker publication did not. The collector originally rendered
  the phase as `invalid` because its display filter omitted digits; that display
  is not the plan's phase or evidence of corruption. The actual plan is parsed
  and hash-verified by the continuation, never reconstructed from that display.
- Installed receiver source is `fc0229b71c50c1bcb15d37f3625189fd9a7cb495`;
  `docker_host.py` SHA-256 is
  `0aa449968b98038fd68aca1b093640bea76d3889c185dce298775943881f20fe`.
  Original `INSTALL.json` is
  `d7b5c208f223720bfaf33525aff3bb3f116054b654015cbc7298e648fc959379`;
  original failed attempt journal is
  `fb0db30cb18da821ab4cb7b4c3ab89f9848707eb7bbf7dc0a739452953eec50b`.
  The live upstream differs from the saved upstream, as expected for retained
  safety routing; its exact hash is pinned, not silently treated as cleanup.

One bounded owner-only **read-only** collection, from the reviewed checkout:

```powershell
Get-Content -Raw .\ops\deploy\collect_worker_recovery.py |
  ssh b-uh 'sudo -n timeout 60s /usr/bin/python3 -B -'
```

Return only its JSON. It reports hashes and selected plan metadata, never
configuration contents, SQL, `.env`, logs, keys or task payloads. It neither
imports the installed receiver nor calls recovery. An incomplete report is a
blocker, not permission to retry or clean up.

## Corrected code and payload ownership

Celery 5.6.3's worker CLI applies `host_format(default_nodename(value))`:
`worker_%n` becomes `celery@worker_<short-hostname>`. The receiver now preserves
that normalization order, explicit `name@host`, and supported host/process
placeholders. The full worker set, unique identities, queues and tasks remain
mandatory. Bounded failure messages report expected, observed, missing and
unexpected worker names; they never include reply bodies. The same check runs
before preparation/migrations, candidate traffic, after worker replacement,
and during rollback verification.

**Main or a release archive alone cannot activate this correction.** The
forced-command wrapper imports `ops.deploy.receiver` from the installed
`/usr/local/lib/buh-platform-v2` library; that receiver imports `DockerHost`.
The immutable archive builder packages only `REQUEST.json`, exact `release/`
files and lineage manifests. It does not ship or execute `docker_host.py`.
Checking out v0.6.2 on the runner therefore cannot repair the installed checker.

The smallest release-preserving path is an explicitly approved **receiver-only
correction**, with the reviewed `docker_host.py` and truthful installation
provenance, followed by verified rollback completion. Application release bytes
remain v0.6.2 at `6074b965cbd2e6ab2630cd539ee455b8d419aef6`, source
`4f98e7cb559ee1a9b269ea1f94938678d2dac4df`, manifest SHA-256
`c9cf79d34c8b3f90556e405f338ee4c1e7818d9c686f0462eec7dca201b784f4`.
No runner archive override, release rebuild/ref move, or application payload
substitution is authorized. The original receiver upgrade's historical
baseline/preflight command is not an activation shortcut for this changed host.

## Work prerequisites and stopping conditions

1. Review the exact qualified feature head and the real Celery/Redis rehearsal.
   Verify the read-only owner inventory, including installed hashes and the
   actual retained plan flags. The receiver correction must be pinned to those
   installed bytes and separately approved before activation.
2. Keep the failed journal, database backup, recovery plan, static backups,
   image identities, and both safety slots. Verify migration compatibility
   against the old running application. Never reverse migrations or restore
   the database to make the health check pass.
3. Do not send a “no-change preflight” merely to probe the fix: the installed
   receiver invokes `recover_incomplete_plan()` **before reading a new archive**.
   With an active plan this can perform rollback/traffic operations. Such an
   invocation needs explicit recovery approval and independently validated plan
   context, not the consumed deployment authorization.
4. Rollback completion must prove exact previous images/replicas, one beat,
   all six real worker identities/queues/tasks, Django/database/Redis health,
   compatible migration state, static hashes and Nginx route configuration,
   every public/internal route, and unchanged restart/log restrictions. Verify
   that retained-plan rehydration supplies its required health baselines; a
   live ping alone does not establish them. Failure preserves all resources.
5. Only after full verified recovery may a separately approved cleanup restore
   the final active-only route, verify it, and retire safety slots/recovery
   resources. Keep the database backup and historical failure journal. Cleanup
   failure must leave protective resources available and be reported separately.
6. A corrected receiver changes deployment infrastructure. Obtain a fresh
   exact-v0.6.2 preflight with its receiver identity, reverify retained evidence,
   and request a **new explicit production approval** and reviewed continuation
   before another deployment. Never rerun `34713182347`, `34679176355`, or the
   consumed continuation; this PR does not reopen them.

## Exact next Work action and payload preparation

First review and merge **only this qualified repair PR**, preserving the v0.6.3
hold. Do not dispatch deployment or a preflight. Ask Anthony for separate approval
to install the one-file receiver correction; identify the reviewed commit and
the SHA-256 of its Git blob `ops/deploy/docker_host.py`. Main/release bytes are not
the receiver's activation identity. Do not repeat old receiver/Nginx maintenance.

Prepare a fresh, clean LF checkout of the exact reviewed feature head (not the
moving main ref). Run the required tests there. Export **only tracked files at
that commit**, with `git archive`, and record/verify the archive SHA-256. Generate
the following **verification-only** request archive in that checkout; this does
not publish, rebuild or dispatch a release:

```bash
python3 ops/deploy/request_archive.py --root . \
  --release-dir releases/platform/v0.6.2 \
  --repository Fifty5D/B-UH-AllianceAuth \
  --release-commit 6074b965cbd2e6ab2630cd539ee455b8d419aef6 \
  --mode deploy --workflow-run-id 34713182347 --workflow-run-attempt 1 \
  --output /absolute/new/local/output/v062-verification-only.tar.gz
sha256sum /absolute/new/local/output/v062-verification-only.tar.gz
git show "$REVIEWED_SHA:ops/deploy/docker_host.py" | sha256sum
```

The original attempt identity is intentional: the helper validates its unchanged
release, lineage and recovery digest, but **never passes it to `receiver.receive`,
the SSH forced command, or DeploymentEngine**. Do not send it as a deployment.
Work must compare both hashes with the reviewed local payload before approving
owner execution. Stage these files, after approval, in a fresh root-owned `0700`
directory **under `/root`**, with no symlinks or writable ancestors; source files
must be root-owned. Verify the transferred archive hashes before extracting the
tracked source. Keep all existing maintenance journals and receiver backups.

From that exact root-owned source directory, with `$RUNTIME_SHA256` and
`$ARCHIVE_SHA256` set to the hashes just approved and `$ARCHIVE` the staged request:

```bash
# Only after explicit receiver-file installation approval:
env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin PYTHONPATH="$PWD" \
  /usr/bin/python3 -B -P -m ops.deploy.worker_recovery install \
  --runtime-sha256 "$RUNTIME_SHA256" \
  --confirm "INSTALL WORKER CHECK $RUNTIME_SHA256"

# Health verification; retains plans, backups, routes and safety slots:
env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin PYTHONPATH="$PWD" \
  /usr/bin/python3 -B -P -m ops.deploy.worker_recovery verify \
  --runtime-sha256 "$RUNTIME_SHA256" --archive "$ARCHIVE" \
  --archive-sha256 "$ARCHIVE_SHA256"
```

The installer changes only the installed `docker_host.py`, atomically. It uses
the production lock, verifies the confirmed original hashes, retains the original
file and `INSTALL.json` in a new `worker-check-<hash-prefix>` receiver backup, and
restores the old file on activation/verification failure. It does not claim a full
receiver upgrade: original `INSTALL.json` stays unchanged, with an additive
`/etc/buh-platform-v2/WORKER-REPAIR.json` describing the exact single-file override.
No wrapper, forced command, credentials, config, engine, release or service changes.

Verification loads the actual retained plan and original immutable archive,
checks all original image/topology identities and requires **zero**, not newly
forgiven, restart counts. It revalidates Compose overlays, Nginx/proxy identities,
owner association, restored files, hashed static assets, and shared functional
health. The narrow existing owner-transition exception stays visible; unrelated
errors remain fatal. This accounts for already-run migrations by checking that
the original application still works with the resulting schema, never reversing
migrations. Verification failure is a stop, not permission to repair live state.

After successful verification, Work reports the evidence and obtains **separate
rollback-completion/cleanup approval**. Only then may Anthony run:

```bash
env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin PYTHONPATH="$PWD" \
  /usr/bin/python3 -B -P -m ops.deploy.worker_recovery complete \
  --runtime-sha256 "$RUNTIME_SHA256" --archive "$ARCHIVE" \
  --archive-sha256 "$ARCHIVE_SHA256" \
  --confirm "COMPLETE ROLLBACK gh-34713182347-1"
```

Completion repeats verification while holding the lock, uses traffic-first
rollback with the pinned **no-replacement** flags, restores and verifies the
active route, and only then removes safety slots/temporary image pins and disarms
the active plan. Exact previous image IDs remain authoritative even if the first
rollback already removed temporary tags. The database backup and backup recovery
plan, original failed journal, and maintenance journals remain. A **new** local
`worker-recovery-gh-34713182347-1.json` records completion; the old failure is never
rewritten. Any error stops; inspect the retained evidence before further action.
Do not repeat completion when its receipt exists or the active plan is absent.

After verified completion, obtain a fresh no-change preflight for exact v0.6.2
and the corrected installed receiver, then obtain a **new production approval**.
The consumed one-use production continuation remains closed; Work must authorize
a fresh reviewed deployment continuation with that new evidence/approval. This
PR does not dispatch or reopen it, and is not itself production approval.

The v0.6.3 hold remains across this repair merge; unconsumed fragments and all
immutable releases are preserved. Draft #58 is separate. Broader release and
generic crash-recovery restructuring remain deferred. No further owner evidence
is needed to review this pinned path; changed live pins require a new bounded
read-only review, not a relaxed comparison.
