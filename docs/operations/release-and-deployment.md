# Release and deployment operations

## Deployment generations

There are two deliberately separate generations:

1. **Legacy v1:** the production-tested Moon Tax `v0.3.x` bundles, guarded GitHub
   workflow, and forced-command VPS receiver.
2. **Platform v2 (current production generation):** source-built multi-application releases described
   by `RELEASE.json` and `INSTALL_PLAN.json`.

Platform v2 is the current routine production deployment generation. Legacy v1
artifacts and its forced-command route remain immutable recovery assets until a
successful Platform v2 deployment and separately approved rollback drill are
recorded. Do not edit, regenerate, or silently replace an existing `v0.3.x`
directory.

## Routine delivery and responsibility boundary

The operator-facing Actions stages are **Validate PR**, **Preview UI**, **Prepare
Release**, **Deploy Production**, and **Operations and Recovery**. The Validate
PR check `Source test suite / Required source checks` remains the single required
status for `main`, preserving the existing protected context while presenting
the consolidated workflow name. Its fan-out includes the complete fast,
integration, upgrade, restore, browser, migration, permission, accounting,
release-plan, and immutable legacy-artifact lanes. Every lane checks out the
exact PR head rather than GitHub's synthetic merge ref.

Codex opens or updates one feature PR. UI paths (templates, static assets,
JavaScript/CSS, themes, and browser tests) automatically run the synthetic
preview; `ui-preview` remains a manual override. Every preview artifact is
retained for three days and its JSON manifest binds the repository, PR, head,
workflow run and attempt, artifact name, screenshot inventory, and Playwright
report. The base-branch-only readiness workflow never checks out PR code with its
write token, never writes to forks, and never qualifies a PR without the existing
`codex` label or an exact `main` base; it may only remove an ineligible readiness
label from another same-repository PR. A head, base retarget, preview-policy,
check-run, review, review-comment, or PR-conversation-comment change removes `ready-for-work`, adds
`needs-codex`, and never restores readiness on its own; a requested or in-progress
rerun invalidates readiness before it can finish.
After both exact-head workflows are green, Codex applies `ready-for-work`. The
trusted workflow immediately removes that request label, re-fetches the current
required check, review state, workflow run, non-expired artifact IDs and digests,
and bounded manifest, and publishes a three-day readiness record. The record
contains a deterministic digest and metadata-only snapshot of latest reviews,
review threads, and ordinary PR conversation comments (IDs, authors, states,
resolution, and update times; never comment bodies). A conversation comment with
a serious marker remains blocking until its author edits away the finding or
deletes the obsolete comment and readiness is requested again. The reserved
GitHub-Actions-owned readiness-pointer comment is excluded from that snapshot.
One bot-owned PR comment is the sole authoritative pointer
to the current record and artifact digest, explicitly superseding older artifacts.
Only after that pointer exists does the workflow remove `needs-codex` and re-add
`ready-for-work` as its final external signal. Its own label event is ignored, so
it cannot loop or publish a duplicate. ChatGPT Work must require the current head,
absence of `needs-codex`, and the qualified pointer before performing the final
risk/review assessment or merging this non-production PR. Neither the label nor
that merge authorizes production.

Configure a ChatGPT Work GitHub-event task for feature PRs when
`ready-for-work` is added. Use this operating prompt (with the repository and
status names unchanged):

> Review the current head of this same-repository pull request targeting `main`.
> Require the exact `Source test suite / Required source checks` success for that
> head, no pending or failed checks, no `needs-codex` label, and the current
> GitHub-Actions-owned `buh-readiness-record:v2` pointer. Inspect every applicable
> three-day preview manifest, desktop/mobile screenshot, browser result, artifact
> ID and digest. Review all current review findings, threads, and PR conversation comments, migrations and
> backward compatibility, authentication and permission boundaries, Moon Tax
> accounting integrity, and deployment risk. Re-read the PR head and evidence
> immediately before acting. If anything is missing, stale, expired, unresolved,
> or unsafe, do not merge: remove `ready-for-work`, add `needs-codex`, report the
> exact evidence to Anthony, and give him one exact prompt to paste into Codex to
> fix this same PR. If everything passes, merge only this exact non-production
> feature head into `main` with a two-parent merge commit. Never deploy, approve a
> production release, alter release refs, rerun Actions, or bypass a failed gate.

After merge, Prepare Release builds one immutable release and performs the
no-change receiver preflight. Work presents the bound readiness record and asks
Anthony once for production approval. Deploy Production accepts only the exact
approval-marked release evidence. Receiver upgrades, emergency rollback, and
diagnostics are not routine application releases: use the separately reviewed
receiver procedure below or Operations and Recovery, and never automatically
retry a production failure.

## Required repository settings (one-time audit)

Configure a `main` ruleset requiring pull requests and the exact status
`Source test suite / Required source checks` from Validate PR; block force
pushes and branch deletion, require branches to be up to date, and permit only
merge commits. Give the ChatGPT Work identity **Contents: write**, **Pull
requests: read/write**, and **Actions: read** so it can inspect evidence and merge
an unchanged, validated non-production head. Give **Issues: read/write** only when
the task will manage `ready-for-work`/`needs-codex` labels or publish evidence;
read-only is sufficient if those actions are handled elsewhere. Give it no Actions
write, Administration, Environments, or Secrets access. Set the repository Actions
variable `BUH_CHATGPT_WORK_ACTOR` to the exact GitHub login used by the connected
Work task (not a display name, and never empty when Work differs from `Fifty5D`).
Trusted workflows allow only `Fifty5D` and that one nonempty configured login as
feature-merger identities. Keep the deploy-capable `BUH_DEPLOY_SSH_KEY` only in
the protected `production` environment and expose it only through Deploy
Production. The forced-command `BUH_OBSERVER_SSH_KEY` is a separate, read-only
credential used by Deploy Production, Operations and Recovery, and Production
runtime fingerprint. With the current workflow layout, make that observer key
and `BUH_VPS_KNOWN_HOSTS` repository-level Actions secrets so the two observer-only
workflows can read them without entering the deploy-capable `production`
environment; keep host, port, and observer user values in repository Actions
variables. Never expose `BUH_DEPLOY_SSH_KEY` to either observer-only workflow.
Protect `release/platform-v*` against update/deletion and restrict creation to
the release publisher. Repository administrators must audit these settings in
GitHub because source code cannot enforce ruleset actors, environment reviewers,
or secret scope.

The trusted readiness workflow creates or normalizes the labels idempotently. For
PR #44, which first introduces that `pull_request_target` workflow, no normal
premerge bot pointer can exist because the workflow is not yet available from
`main`. Reviewers must validate its exact head manually and must not work around
the bootstrap by running PR code with a write token. After—and only after—a
protected two-parent merge of PR #44, trusted-main Prepare Release may use the
PR-44-specific `first-introduction-postmerge` attestation. That path proves the
first parent lacks the workflow, the merge adds it, the second parent is the exact
feature head, the merger is explicitly allowlisted, and the exact check, preview,
labels, and reviews still pass. Any failure stops release preparation and requires
manual investigation; it never deploys. These commands are the idempotent manual
label bootstrap fallback:

```bash
gh label create ready-for-work --repo Fifty5D/B-UH-AllianceAuth --color 1D76DB --force
gh label create needs-codex --repo Fifty5D/B-UH-AllianceAuth --color D93F0B --force
gh label create ui-preview --repo Fifty5D/B-UH-AllianceAuth --color 5319E7 --force
```

## Current release-builder and publisher boundary

`ops/release/buh_release.py` provides the deterministic, non-production release
trust boundary:

- `plan` validates full source commit IDs, source fingerprints, explicit change
  fragments, dependency propagation, compatibility input, and next versions;
- `build` builds only applications selected by the plan;
- `assemble` creates release notes, manifests, install order, wheel checksums, and
  references to reusable unchanged artifacts; and
- `verify` validates the assembled bundle, wheel metadata/records, and checksums.

`.github/workflows/build-platform-release.yml` can run only against an exact main
commit that already has a successful Validate PR push run. It builds and verifies
one candidate artifact. Automatic publication accepts only a first-attempt,
successful same-repository Validate PR `workflow_run` for the exact main merge,
plus the twice-reverified canonical feature-readiness lineage. Manual publication
remains restricted to the repository owner and exact confirmation phrase. Both
modes reverify the downloaded candidate and, under one repository-wide publication
lock, atomically create
an immutable release ref and a disposable sync ref at one release commit whose
sole parent is the tested source. It never updates main or connects to the VPS.

Publication automatically opens the exact release-state synchronization pull
request from `sync/platform-vX.Y.Z`, never from the immutable release ref, and
never merges it. This lets GitHub update the PR branch when `main` advances
without changing published release identity. The helper accepts an advanced
`main` only when the original release source remains an ancestor; divergence
fails closed. If repository policy blocks Actions from creating pull requests,
the publication run fails closed after recording a one-click manual recovery
URL. That PR records the exact published release state on `main`; the publisher
never pushes `main` directly. Validate PR protects the append-only release ledger
by rejecting changes or removals to prior release state and additions that do
not match their immutable release ref. Before another release can be planned, an
exact parity gate requires the highest release ref and the latest release state
on `main` to identify the same release commit and bytes. Missing, extra, or
different state fails closed.

Source and release-candidate jobs now enforce exact transitive hash locks,
exactly pinned build tools, and digest-pinned disposable test images. The
production v2 receiver and backup-aware state machine are now repository-owned
and tested, as is a previous-production upgrade/restore rehearsal. The reviewed
production runtime-image digest is pinned. Receiver installation is operationally
separate from application promotion; a successful no-change preflight and an
explicitly approved deployment are still promotion gates.

The one-time receiver upgrade is transported as a commit-bound current-tree tar
and blob inventory, never as reachable Git history. Its retained receiver backup
contains an exact recovery source/config and a transaction marker. If an
uncatchable interruption leaves `recovery_required: true`, follow the exact-path,
same-commit locked recovery command in `ops/deploy/README.md`; never choose a
backup by recency, retry the upgrade, or alter the database first.

The release builder also refuses publication until
`production_runtime.base_image` contains that reviewed digest. Candidate builds
remain available before then, so the bootstrap prerequisite cannot accidentally
create an immutable but undeployable release.

`ops/supply_chain.py verify` is offline and fail closed. The scheduled
`source-supply-chain.yml` resolver has only `contents: read`: it may contact PyPI
and the two approved public registries and upload a bounded patch for review, but
it cannot push a branch, open a pull request, merge, release, or deploy. Dependabot
updates reviewed direct inputs; lock regeneration and image-digest refresh remain
an explicit reviewed source change.

## Version and schema rules

- Functional changes require a reviewed fragment under `changes/`; the builder
  determines the SemVer bump from declared intent rather than guessing impact.
- A dependent application is retested whenever an upstream application changes.
- Platform and application versions advance independently.
- `apps.toml`, `compatibility.toml`, change fragments, `RELEASE.json`, and
  `INSTALL_PLAN.json` carry explicit schema versions.
- Readers reject unknown schema versions and unknown security-sensitive fields.
- A schema revision requires a new schema file/version plus positive, negative,
  backward-compatibility, and canonical example tests.
- Before changing the current schema version, preserve and version every
  transitive schema-v1 planner and manifest/install-plan reader, route ledger
  replay by each historical manifest's schema, and prove every existing
  immutable schema-v1 release still verifies. The current v1 entry point
  intentionally fails closed until that migration work exists.
- A release manifest records the source commit, prior release, compatibility
  contract, app graph, exact artifact hashes, install order, and migration plan.

Git blob IDs may avoid uploading an unchanged wheel twice, but they are only a
transport optimization. SHA-256, wheel `RECORD`, package metadata, source
fingerprint, and release provenance remain authoritative.

## Platform v2 release flow

1. Merge reviewed source and change fragments through required checks.
2. Run fast checks: Ruff, compilation, JavaScript syntax, Django checks,
   migration drift, and targeted tests.
3. Run the disposable MariaDB/Redis/Celery/fake-ESI integration environment.
4. Reconstruct the immutable v0.3.3 schema, seed synthetic accounting evidence,
   test the in-place upgrade, restore its pre-migration dump to a second database,
   and test the restored upgrade again.
5. Run Playwright table, sorting, row navigation, controls, keyboard, responsive,
   and permission tests with synthetic identities.
6. Resolve the reviewed compatibility contract into exact hashed Python locks and
   digest-pinned container inputs.
7. Plan versions and build only changed applications; test all downstream
   dependents.
8. Reuse unchanged wheels only after their previous identity and SHA-256 match.
9. Assemble and verify one immutable release bundle in an isolated installation.
10. Under the global publication lock, atomically create one absent immutable
    release ref and one absent disposable sync ref at the release commit, whose
    sole parent is the tested source commit.
11. Create the mandatory synchronization PR with the repository-owner
    `BUH_RELEASE_PR_TOKEN`, allowing Validate PR to start without a GitHub run-
    approval click. The token cannot publish a release or deploy production.
12. Run Validate PR on the exact sync head and run the production receiver in
    no-change `preflight` mode. A workflow-authored readiness marker binds the
    successful feature, main, and sync validation runs; feature readiness and
    preview artifact IDs/digests; exact preflight artifact ID/digest and run
    provenance; manifest hash; source; release; and PR into one approval nonce.
13. Have ChatGPT present that evidence and wait for one explicit approval from
    the repository owner. ChatGPT places the exact matching approval marker in
    the merge commit message and performs one merge action; never squash or
    rebase this PR.
14. Revalidate the merged PR, readiness marker, approval-bearing merge commit,
    feature/main/sync workflow runs, release lineage, merge ancestry, and exact
    preflight artifact. Re-run published feature readiness and byte/canonical-
    compare it with the approval record, so changed reviews or labels and missing,
    expired, or replaced readiness/preview artifacts fail closed. The outer
    authorization job repeats this after downloading and validating the preflight
    artifact; the reusable deploy job repeats it again after packaging and
    immediately before forced-command SSH. Only then deploy the exact immutable
    release. A result comment returns success or failure to ChatGPT.
15. Require release-ref/main parity before planning any later release. The sync
    ref may be updated or deleted after merge; the release ref may not.

A release is never rebuilt after publication. A correction receives new app and
platform versions. Publication and synchronization do not deploy or authorize a
deployment.

GitHub can return an empty `pull_requests` array on a successful Validate PR run
after its PR is merged. Post-merge authorization verifies the recorded run ID,
attempt, workflow, event, repository, branch, and exact release commit first.
Only for an explicitly empty array, it then uses the release commit's associated
PRs endpoint and revalidates the exact closed, merged synchronization PR. Missing,
malformed, or conflicting associations still fail. Readiness continues to require
the direct run-to-open-PR association before the owner approves the release.
The approval marker, preflight, release lineage, and artifact checks are unchanged.

## Required production controls

Platform v2 must retain or strengthen the legacy controls:

- protected production environment, with the sole human approval recorded in
  ChatGPT and bound to the exact preflighted release on GitHub;
- checkout of a full, protected release commit rather than an arbitrary branch;
- a repository ruleset that restricts creation of `release/platform-v*` to the
  reviewed publisher and forbids every update and deletion of those refs;
- merge-commit-only handling for release synchronization PRs: disable squash and
  rebase merging repository-wide or enforce an equivalent policy, and do not
  require linear history on `main`;
- minimal GitHub permissions and actions pinned to full commit SHAs;
- separate least-privilege deploy and read-only observer identities;
- pinned known-host validation and no credential output;
- GitHub concurrency plus a VPS-side `flock` deployment lock;
- manifest/schema validation, archive limits, safe filenames, and strict hashes;
- candidate checks before live replacement;
- post-deployment route, database, Redis, Celery, migration, version, and container
  checks; and
- fail-closed diagnostic redaction and a sanitized retained artifact for every
  attempt.

## One-time ChatGPT deployment automation setup

Create one fine-grained personal access token owned by `Fifty5D`, restricted to
this repository, with **Contents: read** and **Pull requests: read and write**.
Store it as the repository Actions secret `BUH_RELEASE_PR_TOKEN`. Give it the
shortest practical expiration and rotate it before expiry. The release workflow
uses it only in the sync-PR job; publication continues to use the short-lived
workflow token and production continues to use the forced-command SSH identity.

Connect the same GitHub account to ChatGPT and create one event-triggered task
for pull-request activity in `Fifty5D/B-UH-AllianceAuth`, filtered to titles
beginning `Sync platform release v`. The task must:

1. react to the `buh-platform-ready:v2` comment by reading the PR, canonical
   feature-readiness lineage, required checks, referenced workflow runs, and
   retained preflight artifact;
2. show the version, source/release commits, manifest hash, Validate PR result,
   preflight result, and PR URL, then wait for the owner's explicit approval;
3. after approval, re-read the unchanged head and checks, then perform exactly
   one merge action using a merge commit with the expected head SHA and the
   supplied `buh-chatgpt-approved:v2` marker in its commit message; and
4. react to the `buh-platform-deploy-result:v1` comment by inspecting the deploy
   run and retained diagnostics and reporting success or the precise safe
   failure. It must never retry or roll forward production automatically.

The automatic release trigger deliberately requires the newest successful
`Validate PR` push run on `main` and at least one tracked `changes/*.toml` file.
Release synchronization deletes consumed fragments, so merging a sync PR cannot
start another release. If `main` advances while a release is queued, the older
run stops and the newer tested commit collects the still-unconsumed fragments.
The trusted automatic no-change preflight may be initiated by the repository owner
or the exact nonempty `BUH_CHATGPT_WORK_ACTOR`; manual preflight dispatch and every
production deployment remain owner-only.

Do not configure a second required-reviewer click on the `production`
environment when the one-approval ChatGPT flow is active. Keep the environment
for secret isolation and deployment policy; the approval-marker/merge gate is
the single human production authorization.

Docker cache entries may improve build speed, but cache keys include the lockfile,
architecture, Python version, and base-image digest. Cached layers are not release
provenance and never bypass artifact verification.

The Platform v2 host Dockerfile also installs one verified wheel per layer in a
stable order: third-party and unchanged wheels first, changed owned wheels last.
Most application-only releases therefore rebuild only the final small layers.
The candidate is still built and checked before live containers are replaced.
Each verified wheel is force-reinstalled from its SHA-256-bound bytes, even when
the distribution version matches the live image. Wheel files remain in their
immutable build layer because a later non-root layer cannot remove a root-owned
`COPY`, and deleting them later would not reduce image size.
Before a candidate build, every Auth service's resolved Compose definition must
use the exact application directory as its build context and the reviewed custom
Dockerfile. The resulting image must change from the captured live image and
must expose exact receiver-generated labels binding it to the platform version,
source commit, release commit, manifest hash, and digest-pinned runtime base.

Production preflight executes the same cached candidate build, per-service
package-version probes, Django checks, and migration plan as deployment. It then
restores each service's exact preflight image ID to its prior Compose image
reference without restarting live containers. Each live image is first pinned
under an attempt-scoped rollback tag so a candidate build cannot make the old
image unreachable; every restored reference is resolved and compared with the
captured image ID before cleanup succeeds. A deployment rollback uses those
captured service images as well, so it does not depend on rebuilding old source
after a failure. Candidate preparation also preserves the owner and mode of
`custom.dockerfile`. The private `local.py` copy is verification-only: the live
mounted settings file is never overwritten, and its bytes, owner, and mode must
remain unchanged.

Every receiver operation uses the complete, ordered Compose stack declared by
the production `.env` `COMPOSE_FILE` value. Each entry must be a unique, regular
file beneath the application directory, and the configured base Compose file
must be included. The receiver supplies every validated file explicitly to
Compose, preserving host-specific service extensions, bind mounts, and sockets
through preflight, replacement, health checks, and rollback. An absent, unsafe,
or unsupported Compose stack fails before candidate preparation or live changes.

The receiver treats each configured Compose service as one logical service with
one or more running replicas. Before building, it captures each service's exact
replica count, image ID, and Compose image reference. Replicas within one service
must agree, but services may have distinct BuildKit image IDs even when Compose
inherits one anchored build definition. Swap and rollback pass those counts back
to Compose explicitly, preventing an update from silently shrinking a scaled
worker pool. Health succeeds only when every expected replica is running on its
expected per-service candidate image and every replaced Auth replica has a zero
restart count.

## Database and rollback safety

Before any production migration, the deployer must:

1. confirm the currently deployed release and schema;
2. validate the migration plan and prior-version compatibility;
3. acquire the host deployment/migration lock;
4. create and verify a restorable database backup or provider snapshot; and
5. record the restore target with the deployment attempt.

Use expand/contract changes whenever possible. Code rollback is automatic only
while the database remains compatible with the previous release. After an
incompatible or irreversible migration, the approved recovery action is the
release's documented forward fix or database restore—not merely restarting old
containers.

The existing legacy v1 updater does not roll back the database. It backs up and
restores `custom.dockerfile` and `conf/local.py`, rebuilds the old application
containers after a failed swap, and leaves its backup/log for diagnosis. Any
legacy release containing a migration must therefore remain backward compatible
or include a separately tested database recovery procedure.

## Verification and release state

A deployment progresses through recorded states: `validated`, `backed_up`,
`migrated`, `swapped`, `healthy`, and `verified`. Failure records the last state,
release/source identity, diagnostics artifact, and permitted recovery action.

Success requires all of the following:

- expected package and platform versions in every Auth service;
- no pending or unexpected migrations;
- Django system checks pass;
- Gunicorn, workers, beat, MariaDB, and Redis are healthy;
- authenticated and unauthenticated smoke routes use their fixed per-route
  status policies, with every 5xx response rejected;
- required Celery tasks are registered and a worker heartbeat succeeds;
- fatal startup patterns and restart loops are absent; and
- sanitized post-deployment diagnostics were collected and validated.

Diagnostic collection may fail independently of application health, but the
workflow must remain unsuccessful and the release must not be declared live
until verification is recovered. Do not automatically restore a healthy database
solely because the read-only observer path failed.

Every workflow attempt also retrieves exactly its own root-owned, bounded attempt
journal through the read-only observer. The retained, host-redacted report keeps
the first-line failure plus a bounded diagnostic tail, avoiding ambiguous
"build failed" reports without exposing arbitrary host files.

## Temporary visual preview

Every preview-relevant change automatically creates an expiring preview from the
same source-built test image; `ui-preview` forces the same path for a change that
falls outside the automatic path policy. It uses a temporary database, fake ESI,
synthetic users, no production secrets, no Discord/ESI egress, a three-day
artifact, and unconditional teardown. A change with neither a relevant path nor
the override records that no preview applies and does not publish preview
artifacts.

## Promotion checklist

- [x] Exact `--require-hashes` dependency locks are generated and verified.
- [x] All disposable source-test service and base images are pinned by digest.
- [x] The production runtime image is pinned by digest.
- [x] Schema v1 validators have canonical and adversarial tests, and schema
  evolution policy is enforced.
- [x] Source, integration, upgrade, concurrency, browser, and contract lanes are
  required GitHub checks.
- [x] Database backup restoration is required in the disposable environment.
- [x] Platform v2 forced-command receiver accepts only the declared manifest and
  install plan.
- [ ] The installed receiver completes a no-change production preflight.
- [ ] Mandatory post-deployment verification and diagnostics pass in production.
- [ ] A Platform v2 rollback drill succeeds before legacy v1 is retired.

Implementation checkmarks describe repository controls. They become operational
evidence only after the corresponding hosted workflow succeeds; no unchecked
production item may be inferred from a source test.
