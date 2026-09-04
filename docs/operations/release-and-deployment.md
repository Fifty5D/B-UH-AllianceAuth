# Release and deployment operations

## Deployment generations

There are two deliberately separate generations:

1. **Legacy v1:** the production-tested Moon Tax `v0.3.x` bundles, guarded GitHub
   workflow, and forced-command VPS receiver.
2. **Platform v2 candidate:** source-built multi-application releases described
   by `RELEASE.json` and `INSTALL_PLAN.json`.

Legacy v1 remains the production authority. Do not edit, regenerate, or silently
replace an existing `v0.3.x` directory. Platform v2 receives a separate release
path and deployment entry point and may replace v1 only after all promotion gates
in the architecture document pass.

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
commit that already has a successful Source CI push run. It builds and verifies
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
never pushes `main` directly. Source CI protects the append-only release ledger
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
11. Review and merge the mandatory synchronization PR from the sync ref
    after approving its queued workflow run and after Source CI verifies
    append-only history and exact release-ref/main parity. GitHub requires this
    one-time run approval because the PR was opened with `GITHUB_TOKEN`. Use a
    merge commit; never squash or rebase this PR. The sync ref may be updated or
    deleted after merge; the release ref may not.
12. Require that parity before planning or publishing any later release.
13. Separately select that exact release in the manually approved production
    workflow.

A release is never rebuilt after publication. A correction receives new app and
platform versions. Publication and synchronization do not deploy or authorize a
deployment.

## Required production controls

Platform v2 must retain or strengthen the legacy controls:

- protected production environment with manual approval;
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

Production preflight executes the same cached candidate build, package-version
probe, Django checks, and migration plan as deployment. It then restores the
exact preflight image ID to every prior Compose image reference without restarting
live containers. A deployment rollback uses that captured image ID as well, so it
does not depend on rebuilding old source after a failure.

The receiver treats each configured Compose service as one logical service with
one or more running replicas. Before building, it captures the exact replica count
and common image across every Auth container. Swap and rollback pass those counts
back to Compose explicitly, preventing an update from silently shrinking a scaled
worker pool. Health succeeds only when every expected replica is running on the
shared image and every replaced Auth replica has a zero restart count.

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
