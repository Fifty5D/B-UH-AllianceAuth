# Current retained-log recovery — September 12, 2026

This supersedes earlier deployment/receiver-maintenance sequences, not their
evidence. PRs #52, #60 and #61 are merged; the follow-up base is
`4f02900aed9e62c3f7fcb7cce4bcdde50325b5bc`. No code merge is a new production
approval. The one-use `CONTINUE APPROVED V0.6.2` dispatch is **consumed**.

Anthony has **already installed** PR #61's one-file worker repair, with result
`receiver-file-repaired`. Do not replay its `worker_recovery install` command,
the full receiver installer, or any receiver/Nginx maintenance. Its exact runtime
is `c33fe29ed735e18f0c62bbbb31aecd7a6f3ee49a175a8c1c2ad8f33b90134ef8`;
preserve `/etc/buh-platform-v2/WORKER-REPAIR.json` and its backup
`/var/backups/buh-receiver-upgrade/worker-check-c33fe29ed735` unchanged.

The next **verify**, not deploy, passed the corrected worker check but failed:
`Retained owner-transition log scan for allianceauth_worker_services exceeded the bounded output limit`.
Cleanup remains **not established**, with `preserve_resources: true`. This is
not a new failed deployment or permission to remove safety resources.

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
- Base receiver source remains `fc0229b71c50c1bcb15d37f3625189fd9a7cb495`.
  Its original `docker_host.py` SHA-256 was
  `0aa449968b98038fd68aca1b093640bea76d3889c185dce298775943881f20fe`;
  that file is retained in PR #61's backup, not currently installed. The installed
  single-file override is the `c33fe29…` hash above.
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

The new log reader preserves the entire interval starting at
`2026-09-12T19:09:32+00:00`. It streams Docker's complete output with a two-chunk
queue, 64 KiB lines/batches and the existing per-command deadline (including
classification); it neither tails logs nor changes the global 4 MiB command cap.
Only possible owner records are temporarily spooled under a private `0700`
directory, with a bounded SQLite cache. All records for the same source/second
remain together, including out-of-order duplicated Celery/Alliance Auth output.
No historical incident is forgotten when a read or batch boundary is crossed.
The existing exact owner/guild/container/phase classifier remains authoritative.
Individual owner records are bounded to 128 lines/64 KiB and one incident to
512 KiB; ambiguity, overlong records, unfinished lines, read errors, nonzero exit,
spool failure or deadline expiry fail verification and preserve resources.
Temporary log data is removed on success/failure, never uploaded. Fatal-error
detection and the narrow allowlist are unchanged. Linux pipe tests, late duplicate
tests and the immutable-v0.6.2 cold-plan rehearsal cover these boundaries.

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

The smallest release-preserving path is a **second, separately approved one-file
receiver correction**, using `worker_recovery install-logs`, never PR #61's
`install`. The helper runs from a new exact reviewed root-owned source export;
verification requires that staged `DockerHost` to match the newly installed file.
Only `docker_host.py` is installed; `worker_recovery.py` is the reviewed staged
continuation, not a receiver-library override. No runner/receiver-engine/archive
substitution is involved. Application release bytes
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

First review **only this qualified repair PR** and its trusted exact-head evidence,
then Work may merge it with the v0.6.3 hold intact. Codex does not merge. Do not
dispatch deployment or a preflight. Ask Anthony for a **new** approval to replace
only installed `docker_host.py`, from `c33fe29…` to the reviewed log-reader hash.
Approval must identify the qualified feature commit and this new Git-blob SHA-256:

`d75575649e7d6a8bbae51adee8bbc8beb44d7b9765106220f53e4eb73ada74a9`

The helper independently verifies the original `INSTALL.json`, old repair receipt
and backup, original plan/journal/backup-manifest/config/upstream/engine/receiver
pins, and installed `c33fe29…` bytes before activation. Any drift is a blocker,
not permission to refresh pins, replay maintenance or discard a receipt.

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
# STOP unless Work/Anthony approved this exact new payload and source export.
# Run under the same root-only, production-locked helper; do not use "install".
set -euo pipefail
RUNTIME_SHA256=d75575649e7d6a8bbae51adee8bbc8beb44d7b9765106220f53e4eb73ada74a9
test "$(sha256sum ops/deploy/docker_host.py | cut -d' ' -f1)" = "$RUNTIME_SHA256"
test "$(sha256sum "$ARCHIVE" | cut -d' ' -f1)" = "$ARCHIVE_SHA256"
env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin PYTHONPATH="$PWD" \
  /usr/bin/python3 -B -P -m ops.deploy.worker_recovery install-logs \
  --runtime-sha256 "$RUNTIME_SHA256" \
  --confirm "INSTALL RETAINED LOG READER $RUNTIME_SHA256"
```

Require `receiver-log-reader-repaired`, matching old/new hashes and the additive
receipt below. If installation fails, stop and inspect retained evidence; do not
retry or delete its backup/receipt. After Work reviews successful activation,
run the separately staged verification command (no preflight/cleanup/deploy):

```bash
env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin PYTHONPATH="$PWD" \
  /usr/bin/python3 -B -P -m ops.deploy.worker_recovery verify \
  --runtime-sha256 "$RUNTIME_SHA256" --archive "$ARCHIVE" \
  --archive-sha256 "$ARCHIVE_SHA256"
```

The installer changes only installed `docker_host.py`, atomically. It uses the
production lock, retains the installed **c33fe29…** file, original `INSTALL.json`
and existing `WORKER-REPAIR.json` in a new
`/var/backups/buh-receiver-upgrade/retained-logs-d75575649e7d` backup, and restores
c33fe29… on activation failure. It never restores the older pre-PR-61 checker.
Original `INSTALL.json`, PR #61's receipt and its backup are unchanged. A **new**
`/etc/buh-platform-v2/RETAINED-LOG-REPAIR.json` binds both hashes, the new backup
and the previous receipt's hash. Verify/complete require that intact receipt
chain and both backups. A later health-verification failure leaves the approved
log reader installed and preserves every recovery resource; it is not automatic
permission to revert infrastructure or clean up.
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
