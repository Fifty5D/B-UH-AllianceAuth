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
Release**, **Deploy Production**, and **Operations and Recovery**. `Validate PR /
Validation lanes / Authoritative validation` is the single required status for
`main`; its fan-out includes the complete fast, integration, upgrade, restore,
browser, migration, permission, accounting, and release-plan lanes.

Codex opens or updates one feature PR. UI paths (templates, static assets,
JavaScript/CSS, themes, and browser tests) automatically run the synthetic
preview; `ui-preview` remains a manual override. Every preview artifact is
retained for three days and its JSON manifest binds the PR number and head SHA.
A `synchronize` event removes `ready-for-work` before new-head validation. Codex
may restore that label only when the exact head is green and applicable preview
evidence matches. ChatGPT Work then performs the final risk/review assessment and
may merge this non-production PR. Neither the label nor that merge authorizes
production.

After merge, Prepare Release builds one immutable release and performs the
no-change receiver preflight. Work presents the bound readiness record and asks
Anthony once for production approval. Deploy Production accepts only the exact
approval-marked release evidence. Receiver upgrades, emergency rollback, and
diagnostics are not routine application releases: use the separately reviewed
receiver procedure below or Operations and Recovery, and never automatically
retry a production failure.

## Required repository settings (one-time audit)

Configure a `main` ruleset requiring pull requests and the exact status
`Validate PR / Validation lanes / Authoritative validation`; block force pushes
and branch deletion, require branches to be up to date, and permit only merge
commits. Give the ChatGPT Work identity Contents read and Pull requests
read/write so it can merge an unchanged, validated non-production head, but no
Actions, Environments, Administration, or Secrets write access. Restrict
production SSH secrets to the protected `production` environment and the Deploy
Production workflow. Protect `release/platform-v*` against update/deletion and
restrict creation to the release publisher. Repository administrators must
audit these settings in GitHub because source code cannot enforce ruleset actors,
environment reviewers, or secret scope.

Create the workflow labels once (the commands are idempotent):

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
one candidate artifact. Optional publication is separately restricted to the
repository owner plus an exact confirmation phrase, re-verifies the downloaded
candidate, and, under one repository-wide publication lock, atomically creates
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
    no-change `preflight` mode. A workflow-authored readiness marker binds both
    successful runs, the retained artifact, manifest hash, source, release, and
    PR into one approval nonce.
13. Have ChatGPT present that evidence and wait for one explicit approval from
    the repository owner. ChatGPT places the exact matching approval marker in
    the merge commit message and performs one merge action; never squash or
    rebase this PR.
14. Revalidate the merged PR, readiness marker, approval-bearing merge commit,
    both workflow runs, release lineage, merge ancestry, and downloaded preflight
    evidence, then deploy the exact immutable release. A result comment returns
    success or failure to ChatGPT.
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

1. react to the `buh-platform-ready:v1` comment by reading the PR, required
   checks, referenced workflow runs, and retained preflight artifact;
2. show the version, source/release commits, manifest hash, Validate PR result,
   preflight result, and PR URL, then wait for the owner's explicit approval;
3. after approval, re-read the unchanged head and checks, then perform exactly
   one merge action using a merge commit with the expected head SHA and the
   supplied `buh-chatgpt-approved:v1` marker in its commit message; and
4. react to the `buh-platform-deploy-result:v1` comment by inspecting the deploy
   run and retained diagnostics and reporting success or the precise safe
   failure. It must never retry or roll forward production automatically.

The automatic release trigger deliberately requires the newest successful
`Validate PR` push run on `main` and at least one tracked `changes/*.toml` file.
Release synchronization deletes consumed fragments, so merging a sync PR cannot
start another release. If `main` advances while a release is queued, the older
run stops and the newer tested commit collects the still-unconsumed fragments.

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
- authenticated and unauthenticated smoke routes behave as expected;
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

Major UI changes may create an expiring preview from the same source-built test
image. It uses a temporary database, fake ESI, synthetic users, no production
secrets, no Discord/ESI egress, a three-day artifact, and unconditional teardown.
Minor backend changes do not need a preview.

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
