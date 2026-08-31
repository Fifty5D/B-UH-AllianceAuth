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
candidate, and creates one new release branch with the tested source as its sole
parent. It never updates main and never connects to the VPS.

Source and release-candidate jobs now enforce exact transitive hash locks,
exactly pinned build tools, and digest-pinned disposable test images. The
production v2 receiver, its runtime-image digest, previous-production upgrade
proof, and backup-aware deployment are still gated future work.

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
4. Test both a fresh migration and an upgrade from the currently deployed schema.
5. Run Playwright table, sorting, row navigation, controls, keyboard, responsive,
   and permission tests with synthetic identities.
6. Resolve the reviewed compatibility contract into exact hashed Python locks and
   digest-pinned container inputs.
7. Plan versions and build only changed applications; test all downstream
   dependents.
8. Reuse unchanged wheels only after their previous identity and SHA-256 match.
9. Assemble and verify one immutable release bundle in an isolated installation.
10. Publish the bundle and version updates in one compare-and-swap commit/tag.
11. Select that exact release in the manually approved production workflow.

A release is never rebuilt after publication. A correction receives new app and
platform versions.

## Required production controls

Platform v2 must retain or strengthen the legacy controls:

- protected production environment with manual approval;
- checkout of a full, protected release commit rather than an arbitrary branch;
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

## Temporary visual preview

Major UI changes may create an expiring preview from the same source-built test
image. It uses a temporary database, fake ESI, synthetic users, no production
secrets, no Discord/ESI egress, a three-day artifact, and unconditional teardown.
Minor backend changes do not need a preview.

## Promotion checklist

- [x] Exact `--require-hashes` dependency locks are generated and verified.
- [x] All disposable source-test service and base images are pinned by digest.
- [ ] The future production runtime image is pinned by digest.
- [ ] Schema v1 validators have canonical and adversarial tests, and schema
  evolution policy is enforced.
- [ ] Source, integration, upgrade, concurrency, browser, and contract lanes are
  required GitHub checks.
- [ ] Database backup restoration has succeeded in a disposable environment.
- [ ] Platform v2 forced-command receiver accepts only the declared manifest and
  install plan.
- [ ] Mandatory post-deployment verification and diagnostics pass.
- [ ] A Platform v2 rollback drill succeeds before legacy v1 is retired.
