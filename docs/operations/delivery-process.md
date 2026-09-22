# Delivery process and current recovery state

Feature requests can be implemented, tested and merged now. Making a feature
available on the live Auth site still requires verified recovery and a separately
approved deployment. A GitHub merge, published release, installed receiver fix,
and healthy production installation are four different states.

## State established on September 20, 2026

| Area | Established state | Remaining action |
| --- | --- | --- |
| Source | PRs #60–#67 merged; PR #67 merge was `fd09e816917e263997ef8c0a8933fdf9de30c0b9` | History collection and the owner nickname guard are in source; they are not installed application updates |
| Release | Immutable v0.6.2 exists at `6074b965cbd2e6ab2630cd539ee455b8d419aef6` | Preserve its bytes; do not infer installation from publication |
| Server receiver | Owner reported PR #63 installation succeeded; SHA-256 `5891086d9656114f2245bed9eea0870f0e8c07c0cd54a8afeaeaef95e702c38c` | PR #67 staged verification failed before installation; recheck pins during the next reviewed operation |
| Production recovery | Attempt `gh-34713182347-1` reached migrations. Full rollback verification and cleanup remain unresolved | Complete diagnosis, resolve findings, then obtain the applicable recovery approval |
| Diagnostic health | PR #67 staged run passed 21 checks; current-sync timing and retained logs blocked it | Recheck live health during reviewed recovery |
| Data syncs | September 20 snapshots report 12/12 Structures owners healthy, 12/12 Moon Mining owners successful, 10/10 refinery ledgers successful, and 133/133 active Member Audit asset attempts successful | Preserve that evidence and require fresh success flags and timestamps |
| Remaining receiver case | The installed guard is absent; the final owner nickname retry emits an additional ERROR | Use the approved exhausted-owner payload with corrected staged checks and the explicit historical assessment in [reviewed recovery](../../ops/deploy/REVIEWED-RECOVERY.md) |
| New publication/deployment | Active recovery hold; the previous continuation was consumed | Keep production held until recovery is verified and its hold is retired through review |

The September 20 review confirmed two additional causes of misleading failures:
Moon Tax refreshes ledgers every four hours, while PR #67 required two-hour
freshness; and the character from the historical assets 404 was re-registered
under a new internal primary key. Its stable EVE identity now has a successful
assets update. Five consecutive four-hour Moon Tax audits were COMPLETE. These
observations support the corrected staged checks; they are not a production
verification or cleanup receipt. No more broad diagnostic collection is needed
unless a named check returns new evidence.

The complete historical inventory accounts for 26 ESI task errors whose HTTP
status was not recorded, owner nickname 403/code 50013 responses and exhausted
retries, and transient Discord 429/503 responses. The later successful data syncs
establish subsequent recovery; they do not prove that history has no gaps or
that the old logs were clean. Old containers and safety slots
were retained; migrations had already run. The current installed application
version must come from verified host markers, image identities and the journal.

`python ops/release/delivery_state.py` reports repository state and the next
action. **Prepare Release** retains that status for held main commits. It never
claims a production version from GitHub alone.

## One path for ordinary work

### Finish the retained recovery and deliver history collection

The merged history changes still need installation. Complete the existing
reviewed recovery first and verify its host completion receipt. Then apply the
bounded [history receiver configuration update](../../ops/deploy/HISTORY-CONFIG-MAINTENANCE.md).
It adds the missing archive setup command without changing existing arguments,
permissions, service state, or the original installation receipt.

Before merging the reviewed removal of both active recovery hold files, pause
the **Prepare Release** workflow using GitHub's workflow controls. This prevents
automatic publication from racing the required intermediate installation.
Preserve the archived historical contract and all host recovery evidence.
Use the existing **Deploy Platform v2** workflow from current, validated `main`
for a new no-change preflight of immutable v0.6.2, then one owner deployment
bound to that exact preflight's run and artifact. Do not retry the consumed
attempt or use the old continuation workflow.

After v0.6.2 is verified on the host, resume **Prepare Release** and qualify the
pending history release through the ordinary flow below. Verify the archive
mount, migrations, enabled collection schedules, successful new observations,
and advancing retry/backfill progress. A successful release installation does
not mean every historical dataset has finished downloading.

### Subsequent updates

1. Start one branch/PR from current main. Implement the feature, add focused
   regressions and the required app/platform change fragment.
2. Run the fast checks once the change is ready, then use the hosted source
   suite. Fix concrete failures on the same PR. A proven prose-only diff skips
   the runtime lanes; code, mixed, missing-base and uncertain diffs run all lanes.
   The release ledger and required aggregate check still run for prose changes.
3. Let Preview UI determine relevance. A server-only change does not need an
   extra screenshot run unless there is a specific visual risk. UI and preview
   policy changes retain their existing full preview requirements.
4. Request `ready-for-work` once the final head is green. Wait for the trusted
   record to publish and re-add the label. Work checks that same head and current
   review evidence, then merges under Anthony's standing non-production approval.
5. Main validation runs. During recovery, source changes and fragments accumulate
   while the explicit hold reports success without generating another release.
   The hold is independent of which feature commits are added to main.
6. Once recovery is cleared through review, release automation follows the
   guarded candidate/publication/preflight flow. Work presents one concrete
   immutable candidate and requests the exact production approval.
7. Deployment remains serialized with the host lock, journal, backup, functional
   checks and rollback handling. Report the installed result and any unresolved
   recovery state from the returned evidence.

Future source, preview, readiness and v2 release/deployment artifacts use a
30-day retention window. This reduces expiry-driven rebuilding; it does not
restore already-expired artifacts or extend an authorization past its verified
evidence. Digest, source identity and live review checks still apply.

## Diagnose the remaining incident once

The combined audit, complete log inventory and subsequent sync snapshot have
already been collected for this incident. Continue with the reviewed recovery
runbook above; do not request these broad diagnostics again unless concrete new
evidence changes the assessment. The following launcher remains available for
diagnosis against the original installed PR #63 receiver.

Run `ops/deploy/run-recovery-audit.ps1` from the reviewed diagnostic commit:

```powershell
.\ops\deploy\run-recovery-audit.ps1 -ReviewedCommit <reviewed-40-character-commit> -RepositoryPath <local-repository-path>
```

Work supplies the concrete commit and local path in the handoff. The launcher
fetches and stages exactly that source, verifies its archive hash, locates the
already-retained immutable verification archive by its pinned hash, and runs
`python -B -P -m ops.deploy.recovery_audit`. The source is temporary diagnostic
tooling; it does not replace the installed receiver. It uses the owner's existing
`b-uh` SSH identity. No additional credentials or manual file-hash copying are
needed. Failed diagnostic output is also copied to the clipboard.

The audit checks installed receipts/hashes and retained recovery context, then
collects independent restoration and functional probes: saved files, static
assets, safety slots, Nginx/upstream, container images/restarts, Django/migration
state, Redis, Celery queues/tasks, internal/public HTTP and application health.
It reports retained log failures together across batches and services. A failed
probe does not prevent later independent probes from running. Missing identity
evidence blocks the dependent checks and disables historical owner exceptions.

The report separates current functional results from the original retained log
interval. That interval still starts at `2026-09-12T19:09:32+00:00`; the audit does
not silently forget an earlier failure by moving the start time. Fatal patterns
and narrow existing exceptions remain unchanged. Reports contain at most 32
distinct bounded failure samples, group repeated timestamps/task IDs, declare
overflow, and mark incomplete reads. Exception type and an emitted HTTP status
are retained without traceback frames, response bodies or exception URLs.
An incomplete or overflowed report requires the affected source to be examined;
it cannot establish a clean interval.

Even when every diagnostic probe passes, the report is **diagnostic-only** and
grants no installation, cleanup or deployment permission. Root-owned scratch
staging and the existing exclusive lock are used; receiver files, receipts,
journals, backups, safety slots and production configuration are preserved.
The launcher retains its private report and stages for review. Only sanitized
output should leave the host; do not put raw logs or task payloads in public PRs.

After reviewing the combined report, fix confirmed causes together where they
share a coherent implementation. Reproduce relevant failures in synthetic
tests before preparing an exact installation or recovery-completion request.
Do not repeatedly reinstall earlier repairs or classify the unexplained
`HTTPError()` as harmless simply to make verification green.

## What the audit changed and what remains conditional

| Cause of repeated work | Change |
| --- | --- |
| Fail-fast diagnosis exposes one problem per attempt | Aggregate independent health probes and complete-interval log findings |
| Diagnostic improvements require another server receiver install | Reviewed staged diagnostic entry point and a reusable owner launcher |
| Every feature merge must fit a historical repair-commit allowlist to keep releases held | Explicit active recovery hold used before new publication and receiver access |
| Bookkeeping labels cancel a required preview already running | Ignored label events receive their own concurrency group |
| Repeated merge permissions and forced previews | Standing Work merge authority documented; automatic preview relevance |
| Three-day preview/readiness artifacts expire during recovery | Consistent 30-day evidence retention for future v2 runs |
| Prose edits rerun database/browser infrastructure | Conservative docs-only CI scope with an always-required aggregate gate |
| Handoffs confuse source, release, receiver and production | Repository delivery status and this single current-state entry point |

The release synchronization PR and historical authorization code still bind
existing immutable evidence. Removing them in the middle of the unresolved
transaction would create another migration of the deployment protocol. After
verified recovery, consolidate that path into one immutable candidate and a
host-owned installed-state record, rehearse both successful deployment and
rollback, then retire the consumed recovery entry points together. Do not add
another PR-specific exception chain as a substitute for that migration.

Review future efficiency using full source runs per final head, forced previews
on non-UI PRs, evidence-expiry rebuilds, receiver installations per incident and
owner commands per diagnosis. This change establishes the mechanisms; it does
not claim measured production-time savings before they are used.
