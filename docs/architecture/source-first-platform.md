# Source-first B-UH platform

## Status and boundary

This tree is the candidate source-first platform. It does **not** replace the
production deployment yet. Production remains on the checked, manually approved
Moon Tax `v0.3.x` bundles and their forced-command receiver until every
Platform v2 promotion gate below passes.

The repository owns B-UH application source and platform automation. It does not
own AllianceAuth upstream source, production data, credentials, Docker volumes,
or unredacted logs.

## Invariants

- Production releases are immutable, checksum verified, manually approved, and
  serialized.
- Pull requests, tests, and previews never receive production credentials or
  database access.
- Application versions, platform versions, source commits, dependency locks,
  database schema state, and deployment generations are distinct identities.
- Historical tax calculations and director decisions are never silently
  recalculated by an upgrade.
- A failed or incomplete verification is not a successful deployment.
- Existing `v0.3.x` artifacts and deployment tooling remain unchanged and usable
  throughout the migration.

## Repository model

| Area | Responsibility |
| --- | --- |
| `apps/` | Buildable source, migrations, static assets, and tests for owned apps |
| `platform/compatibility.toml` | Versioned, reviewable compatibility contract |
| `platform/testauth/` | Secret-free AllianceAuth project used only by tests |
| `platform/testenv/` | Disposable MariaDB, Redis, Celery, fake ESI, web, and browser environment |
| `tests/` | Cross-app integration, concurrency, fake ESI, browser, and permission tests |
| `ops/supply_chain.py` | Offline lock/image verification and review-only update generation |
| `ops/release/` | Change planning, selective builds, manifests, checksums, and verification |
| `releases/` | Append-only checked release inputs; legacy releases are preserved |

The environment definition is permanent in GitHub. Each runtime instance is
temporary, starts from synthetic state, and is destroyed after its test or
preview run.

## Current implementation

- Moon Tax, Structure Ops, and Mining Analytics are normal source packages.
- Fast process-local unit checks, MariaDB/Redis integration, real Celery worker,
  fake ESI, and Playwright lanes are defined. The fast lane deliberately avoids
  Docker startup; production-equivalent service behavior remains in the blocking
  integration lane.
- Pull requests, main pushes, merge queues, and the weekly compatibility run use
  the same reusable, fail-closed source-test workflow.
- Major UI work can opt into an expiring synthetic screenshot preview; ordinary
  changes do not pay that cost.
- The test network is internal and uses synthetic identities and test-only
  credentials.
- Compatibility and application registries use `schema_version = 1`.
- The Python build, production, and test graphs are exact transitive SHA-256
  locks. CI installs them with `--require-hashes`; the small reviewed source-
  distribution allowlist builds without isolation against exactly pinned build
  tools.
- Every source-test base and service image is pinned as a reviewed tag plus an
  immutable multi-architecture manifest digest. A weekly read-only workflow
  resolves PyPI and registry drift and attaches a bounded review patch; it has no
  write token and cannot merge or deploy anything.
- The release planner uses explicit change fragments, a dependency graph, full
  source commit IDs, selective builds, SHA-256 verification, and reuse metadata
  for unchanged wheels.
- Release and install manifests have published JSON Schema v1 definitions.
- A dispatch-only workflow can build an exact, already-tested main commit and,
  after a separate owner confirmation, publish one new immutable release branch.
  It cannot update main or deploy production.

These pieces are foundations, not proof that Platform v2 is production ready.
Previous-production upgrade-path coverage, complete adapter isolation, backup
restoration, a digest-pinned production runtime image, and the production v2
receiver remain promotion gates.

## Compatibility and upgrade policy

Package metadata states supported ranges; a release resolves those ranges to one
exact, tested runtime. `compatibility.toml` is the canonical support matrix for
AllianceAuth, Django, Python, django-esi, Member Audit, MariaDB, Redis, Celery,
Node, Playwright, and third-party Auth apps. Its `production_baseline` table is a
frozen upgrade-test identity. Application and next-platform versions are
source/release-manifest data and are not copied into the matrix, preventing two
version authorities from drifting apart.

Current source/release validation enforces:

- exact Python 3.12.13 transitive locks with artifact hashes and
  `--require-hashes` installation;
- exactly pinned PEP 517 build dependencies with build isolation disabled;
- source-test and browser images pinned by immutable manifest-list digest; and
- owned application and bundled-wheel dependency constraints checked against the
  production lock before `--no-deps` installation.

Before Platform v2 production promotion:

- the eventual production runtime image must also be pinned by immutable digest;
- the exact production lock is blocking CI;
- A scheduled non-production lane tests the next intended AllianceAuth, Django,
  and Python versions and reports deprecations.
- Lock or compatibility changes run all application and browser tests, regardless
  of path-based change detection.
- The generated release manifest records the compatibility contract and lock and
  image digests used to build it.

Dependency updates move one compatibility layer at a time. A supported range is
never interpreted as permission to deploy an untested version.

## Plugin contracts and adapters

Owned domain logic must not spread direct dependencies on third-party internals.
Adapters will isolate at least:

- AllianceAuth ownership, notifications, menu/URL hooks, permissions, and groups;
- Member Audit character, wallet, contract, and refresh interfaces;
- Moon Mining extraction, observer-ledger, and refresh interfaces;
- Structure Ops role configuration; and
- ESI HTTP and schema behavior.

Contract tests run against the exact production lock and the next-version canary.
Permission codenames, named URLs, Celery task names, billing keys, and persisted
event types are public contracts. Renames require an alias or data migration and
a documented deprecation window. Django system checks must reject an incompatible
plugin before migrations or container replacement.

## ESI evolution

Fake ESI fixtures are versioned synthetic contracts, not copies of production
responses. Containerized integration, browser, and preview services run on an
internal network without public egress. The host fast lane uses mocks and local
fixtures but its runner is not an egress sandbox. Adapters must tolerate additive
fields while failing safely on missing or type-changed required fields.

Before promotion, ESI handling must cover pagination bounds, finite timeouts,
bounded retry with jitter, rate/error-limit headers, token revocation, stale-data
marking, and upstream outages. Unknown or unavailable data must not silently
become authoritative zero values. Stored accounting evidence records the parser,
calculation version, source timestamp, and platform release that produced it.

## Database evolution

Every migration is tested from both an empty database and the previous production
schema using production-equivalent MariaDB settings. Data migrations must be
idempotent, resumable, and bounded. Large or destructive changes use an
expand/contract sequence so old and new application code can coexist during the
rollback window.

The legacy installer migrates the live database before swapping application
containers. Its rollback restores the previous Dockerfile and settings, but does
**not** restore database changes. Therefore legacy rollback is safe only when the
new migrations are backward compatible with the prior code. This limitation must
remain explicit; restoring configuration is not a database rollback.

Platform v2 requires a host-side migration lock, a verified pre-migration backup
or snapshot, an upgrade test from the deployed schema, and a release-specific
rollback or forward-fix plan. Irreversible migrations require explicit approval
and a tested database restore path.

## Platform v2 promotion gates

- [ ] Source-built wheels reproduce the behavior and packaged contents of the
  deployed custom applications.
- [ ] Required GitHub checks run fast, integration, migration, browser,
  permission, and release-verification lanes.
- [x] Exact hashed Python locks and digest-pinned source-test service/base images
  are recorded and enforced.
- [ ] The future production runtime image is pinned by digest and recorded in the
  promoted release manifest.
- [ ] Third-party calls are behind tested adapters and startup compatibility
  checks.
- [ ] Manifest, install-plan, compatibility, and fixture schemas are versioned;
  readers reject unsupported versions and schema changes include fixtures/tests.
- [ ] Fresh-install and previous-production upgrade migrations pass on MariaDB.
- [ ] Backup restoration and rollback/forward-fix procedures are demonstrated.
- [ ] Release artifacts are built once, provenance recorded, and promoted without
  rebuilding.
- [ ] Production v2 uses manual approval, host locking, least-privilege forced
  commands, mandatory health checks, sanitized diagnostics, and safe rollback.
- [ ] The legacy `v0.3.x` deployment remains available until one full production
  cycle and a rollback drill succeed on Platform v2.
