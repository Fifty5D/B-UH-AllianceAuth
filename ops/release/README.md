# B-UH source release builder

This directory contains a standard-library release planner and bundle verifier.
It is deliberately separate from production deployment: it never edits an
existing release directory and it never connects to the VPS.

## Inputs

- `apps.toml` is the schema-versioned application registry.
- `platform/compatibility.toml` is the production compatibility contract.
- `changes/*.toml` contains short release-intent fragments.
- the previous immutable `RELEASE.json`, when one exists, is the source of
  prior versions, source fingerprints, and reusable artifact identities.

The compatibility file is intentionally owned by the permanent test platform.
The release builder requires `schema_version = 1` and a `[runtime]` table with
at least `python`, `allianceauth`, `mariadb`, and `redis`. Additional pinned
runtime/application/image keys are preserved in the release manifest.
`[production_baseline]` identifies the frozen legacy deployment used for upgrade
testing; it is historical state, not the next platform version. Application
versions have exactly one authority: each package's version source named by
`apps.toml`. They are deliberately not duplicated in the compatibility file.

`apps.toml` also owns the required `platform_build_inputs` allowlist. Exact
paths and repository-relative glob patterns cover the source-first workflows,
root build configuration, pinned `platform/requirements/*.txt`, transitive
`platform/requirements/*.lock`, the supply-chain verifier, disposable
Alliance Auth settings, Docker/Compose/Bake files, fake ESI, browser tests, and
release tests. Every pattern must match a regular in-repository file. Their
canonical content digest is stored as `build.platform_sha256`; a later change
requires an explicit platform fragment and platform version bump. Workflow
patterns are intentionally limited to source-first names, so guarded legacy-v1
deployment workflows are not pulled into the new release trust boundary.

Example compatibility file:

```toml
schema_version = 1

[runtime]
python = "3.12"
allianceauth = "5.2.0"
mariadb = "11.8"
redis = "8"
```

## Change fragments

The builder does not guess whether a code change is breaking. Each functional
change supplies an explicit, reviewable intent:

```toml
app = "moon-tax"
kind = "feature"
summary = "Add combined billing-account totals"
```

`schema_version = 1` may be included explicitly. A fragment can cover multiple
apps by replacing the top-level `app` and `kind` with one or more
`[[changes]]` tables.

Kinds are `fix`, `security`, `performance`, `internal`, `feature`, and
`breaking`. They map to patch, patch, patch, patch, minor, and major. A changed
app without a fragment fails closed, as does a fragment naming an unchanged
app. Use `app = "platform"` for release-tooling or compatibility-only changes.
On the first source-first release, app fragments are also applied to the
current legacy versions before wheels are rebuilt; bootstrap does not reuse a
published distribution/version identity for different source bytes.

The one reviewed release-gap recovery uses a single platform `fix` fragment
with `deployment_predecessor = "0.5.6"`. This does not change the immutable
ledger predecessor: the next manifest still points directly to v0.6.1. It adds
an exact policy-bound recovery attestation for the verified production v0.5.6 →
v0.6.0 → v0.6.1 chain. Any other baseline, changed policy, missing/reordered
release, compatibility change, application change, or second declaration fails
closed. After that fragment is consumed, later release planning is direct from
the newly published immediate predecessor.

## Commands

```bash
python ops/release/buh_release.py plan \
  --root . \
  --source-sha "$GITHUB_SHA" \
  --previous releases/platform/v0.4.0/RELEASE.json \
  --output build/release-plan.json

python ops/release/buh_release.py build \
  --root . --plan build/release-plan.json --app moon-tax \
  --output-dir build/wheels

python ops/release/buh_release.py assemble \
  --root . --plan build/release-plan.json \
  --wheel-dir build/wheels \
  --previous-dir releases/platform/v0.4.0 \
  --output-dir build/release

python ops/release/buh_release.py verify --release-dir build/release

python ops/release/buh_release.py validate-install \
  --release-dir build/release

GITHUB_TOKEN="$GITHUB_TOKEN" python ops/release/ledger.py verify \
  --root . \
  --source-commit "$GITHUB_SHA" \
  --remote origin \
  --release-root releases/platform \
  --output build/release-ledger.json
```

`ledger.py` requires a full-history checkout. It discovers strict immutable
`release/platform-vX.Y.Z` refs from the authenticated remote, fetches their exact
commits, verifies their sole-parent and predecessor chain, and requires every
release directory in the selected source to be byte-identical to its published
ref. A published release that has not yet been synchronized to `main`, a changed
old bundle, or an unpublished local release directory fails before dependencies
are installed or wheels are built. The canonical JSON report is pinned through
candidate preparation and final publication so a remote-ledger change cannot be
silently accepted midway through a run.

The assembled directory contains `RELEASE.json`, `INSTALL_PLAN.json`, release
notes, wheels, and strict SHA-256 sums. The reusable and manually dispatchable
`build-platform-release.yml` workflow can optionally add the new directory,
version-file updates, and consumed-fragment deletions in one new release-branch
commit whose sole parent is the tested main commit. The atomic publication also
creates a disposable `sync/platform-vX.Y.Z` ref at that commit. It never writes
main or deploys production. After publication it opens, but never merges, the
exact release-state synchronization PR from the sync ref; the immutable release
ref is never an updateable PR head. `auto-platform-release.yml` runs this path
after successful newest-main Validate PR when change fragments remain, then runs
the no-change production preflight and publishes canonical approval evidence.
The repository owner gives the only production approval in ChatGPT; ChatGPT
places the supplied marker in the merge commit message and performs one merge
action after all checks pass.
`deploy-approved-platform-release.yml` revalidates every bound input before it
calls the guarded deploy workflow. Unchanged wheel entries
retain their previous `git_blob_sha`, allowing later repository operations to
reference the existing Git object instead of uploading the wheel.

The sync PR is created with the narrowly scoped `BUH_RELEASE_PR_TOKEN` Actions
secret so GitHub treats it as an owner-created same-repository PR and starts
Validate PR without a separate workflow-approval click. The token needs only
repository Contents read plus Pull requests read/write and is not passed to
candidate, publication, preflight, approval, or deployment jobs.

Repository policy is part of this boundary: only the reviewed publisher may
create `release/platform-v*`, and no identity may update or delete those refs.
Synchronization must use merge commits (never squash or rebase), while the
disposable sync ref may move to satisfy up-to-date branch protection.

Git object IDs are only a transport optimization. SHA-256, wheel metadata, and
wheel `RECORD` validation remain the integrity checks. When a Git object ID is
present, assembly and verification recompute the Git blob framing hash from
the wheel bytes (SHA-1 for 40-character repositories or SHA-256 for
64-character repositories). Newly built owned wheels are also checked against
every wheel in immutable `releases/` history; the same distribution and version
may not reappear with a different SHA-256.

`validate-install` first re-verifies the complete bundle, then creates a fresh
virtual environment and installs every declared wheel with `--no-index`,
`--no-deps`, and an ignored user pip configuration. It checks the installed
distribution versions using that environment's Python. Full imports, Django
checks, migrations, and setup commands currently run against editable source in
the compatibility integration job, where the pinned Alliance
Auth/MariaDB/Redis runtime is available and state is disposable. Repeating that
runtime suite against the assembled candidate wheels is a required Platform v2
promotion gate; isolated wheel installation does not claim to replace it.

The Python verifier is authoritative and remains standard-library-only for
offline deployment verification. The JSON Schema files are strict contracts
for external consumers; release tests keep their safety-critical hash formats,
Git object formats, required provenance fields, and byte limits in parity with
the Python verifier. A full JSON Schema runtime is deliberately not required on
the production host.

Third-party dependencies are exact registry entries: distribution, version,
filename, SHA-256, optional existing Git blob ID, and a repository source path.
The first source-first bundle copies and verifies the pinned source wheel;
later bundles reuse the prior immutable artifact when all identity fields still
match. Changing a dependency entry changes the registry digest and therefore
requires an explicit platform change fragment.

## Dependency compatibility policy

Planning compares every owned application's static `project.dependencies`
against all owned and pinned third-party versions in the proposed bundle. The
registry's internal dependency graph must also exactly match the owned-package
requirements. Assembly repeats the check against each newly built or reused
wheel's actual `Requires-Dist` metadata, and standalone verification repeats it
again from bundle bytes.

The builder never silently bumps a downstream app or assumes compatibility.
If (for example) Structure Ops moves to `0.4.0` while Moon Tax still declares
`aa-buh-structure-ops<0.4`, planning fails. The Moon Tax range must be reviewed
and changed in source with its own change fragment; both wheels are then built
and versioned. Internal requirements must be unconditional and use the
supported reviewable numeric PEP 440 subset (`<`, `<=`, `>`, `>=`, `==`, `!=`,
`~=`, and equality wildcards). Unsupported internal markers, extras, direct
references, prereleases, or local versions fail closed.
