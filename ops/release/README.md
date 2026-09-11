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

## One-time published-v0.6.2 validation recovery

`published-release-recovery-v0.6.2.json` is the executable, one-release recovery
contract for immutable commit
`6074b965cbd2e6ab2630cd539ee455b8d419aef6`. It pins its source and tree,
release-manifest hash, synchronization PR #52, original main validation, every
job in the original Prepare Release run, and the successful retained preflight
artifact. It does not rebuild or change v0.6.2.

PR #53 is the immutable original activation. It merged as
`e0bd37fafcedee3135aa4c4d6bdfc7778d032d48`, with reviewed head
`a8aaf10e03c35a5510f0a7e03a92d4f19e8be0e8`, onto exact source
`4f98e7cb559ee1a9b269ea1f94938678d2dac4df`. Recovery run
`34428188769` then failed only in the fast lane because its two-file harness
imported approval helpers absent from the frozen release checkout. The contract
retains that run's exact jobs and bounded failure artifact; it is historical
evidence and must not be rerun or relabeled.

PR #54 is the immutable first continuation from that merged activation. It
merged as `57e0e29042bab3a989c80e5473ad552ec5b4505b`, with reviewed head
`a743e222546371f386a629459ab8e7b209684905`, onto exact activation
`e0bd37fafcedee3135aa4c4d6bdfc7778d032d48`; push validation `34440024442`
passed. Recovery run `34440488685` then passed all eight source lanes and
uploaded exact artifact `10137840766`, but its final publication job failed
before its first GitHub request because the upload action's bare SHA-256 output
was passed to a consumer that requires canonical `sha256:<digest>` form. The
contract retains that run, every job, and its verified artifact as a second
truthful failed attempt. It published neither a recovered check nor readiness.

PR #55 is the only permitted digest-handoff repair from PR #54's exact merge.
`validation_recovery.py ledger` accepts only the known stale-v0.6.2 ledger
error, this exact PR number and base, and its executable allowlisted tree delta.
It verifies PR #53 and PR #54 independently, constructs a disposable synthetic
merge of the reviewed repair with the immutable release, runs the ordinary
ledger verifier on that future tree, and requires the next plan to be v0.6.3
from v0.6.2. Every other ledger failure or main advance remains fatal.

After the digest repair, `Validate Published Release Recovery` runs the complete
reusable source suite against the exact published v0.6.2 commit. Only the fast
lane replaces these two reviewed test fixtures from the digest-repair merge:

- `tests/deploy/test_request_archive.py`
- `tests/platform/test_coordinated_recovery_rehearsal.py`

The approval portion of that rehearsal loads only the following attested support
under `.buh-recovery-test-support`; it does not overwrite release/runtime code:

- `.github/workflows/source-published-release-recovery.yml`
- `ops/release/buh_release.py`
- `ops/release/ledger.py`
- `ops/release/open_sync_pr.py`
- `ops/release/platform_approval.py`
- `ops/release/published-release-recovery-v0.6.2.json`
- `ops/release/recovery_policy.py`
- `ops/release/validation_recovery.py`
- `tests/release/test_platform_approval.py`

The manifest records every staged path, source blob and SHA-256. Deployment,
application, receiver, migration, browser and release bytes remain those of
v0.6.2; the staged files exercise only activation-side readiness and approval.
The last job separately revalidates original feature PR #51, activation PR #53,
continuation PR #54, digest-repair PR #55, all three push validations, both
failed recovery attempts, the original failed release run, and the retained
successful preflight before it may place recovery readiness on PR #52. Each
merge is associated to its PR by GitHub's commit-to-PR endpoint, while author,
merger, state, head, base and merge identity come from full PR detail.
Authorization repeats the live checks and requires the order PR #53, PR #54,
PR #55, then PR #52.

After all eight reusable-suite lanes pass, the recovery workflow records their
exact job IDs and publishes one GitHub Actions check named
`Source test suite / Required source checks` on immutable PR #52 head
`6074b965cbd2e6ab2630cd539ee455b8d419aef6`. Its report distinguishes the
unchanged release commit/tree, original activation, continuation, digest repair,
two replaced fixtures, and isolated test support. It links the verified workflow
path and run, and binds repository, event, actors, head SHA, attempt, attestation
artifact and every exact lane name and ID. GitHub's job `workflow_name` is a
mutable display title, so it is diagnostic only and is never authorization. Before posting
recovery readiness, the verifier requires main's
protected context to remain bound to GitHub Actions app `15368`, confirms the
original failed check remains historical evidence, and records the new check's
exact REST ID, GraphQL node ID, check-suite ID, completion time, and evidence
binding. GitHub's `filter=latest` check listing is latest per suite and may
therefore retain older suites; it is not treated as one global winner. The new
check must remain the latest row in its own suite, have no pending or newer
conflicting matching suite, and GitHub GraphQL `CheckRun.isRequired` must report
that exact node as required for PR #52. Authorization repeats the same query for
the merged PR; a private comment or an arbitrary successful row cannot
substitute for it. Failed runs `34428188769` and `34440488685` published no
recovery check; the future successful suite and node identities are captured
only by a new run from the reviewed PR #55 merge.
The deployment workflow stages the recovery-aware verifier from the exact PR #52
merge before checking out v0.6.2, then uses that staged verifier at both queued
authorization boundaries. The deployment archive and receiver still come only
from the immutable release.

The recovery descriptor holds automatic release creation on the PR #55 repair
merge and the later PR #52 synchronization merge. This is a deliberate, bounded
continuation exception: main temporarily contains the reviewed unconsumed
platform fragments while the published release is not yet synchronized, but
ordinary ledger validation is never disabled and no new release may start. Any
other main advance, unexpected parent/tree, changed ref, rerun, missing artifact,
or expired artifact fails closed.

### Work activation sequence

Work must perform these steps in order; none is an instruction for Codex or a
routine operator to merge or deploy:

1. Review follow-up PR #55's final head and its concrete continuation. Require
   all required checks, applicable Preview UI evidence, trusted readiness, and
   no `needs-codex`, then request Anthony's approval for this non-production
   merge. Reconfirm that main is exactly
   `57e0e29042bab3a989c80e5473ad552ec5b4505b`, PR #53 and PR #54 full details
   match their pinned identities, and PR #52 is open at
   `6074b965cbd2e6ab2630cd539ee455b8d419aef6`, and both `release/platform-v0.6.2`
   and `sync/platform-v0.6.2` still resolve to that commit.
2. After that approval, merge PR #55 with a merge commit, without squash or
   rebase. Its ordered parents must be the PR #54 merge above and the reviewed
   PR #55 head, and its tree must equal that reviewed head. If main has moved,
   stop; do not update the base or repair contract opportunistically.
3. Require the push-triggered `Validate PR` for that digest-repair merge to pass.
   Inspect the associated `Prepare Release` run and require the
   `published-release-recovery` hold result; it must not build or publish v0.6.3.
4. While the retained preflight artifact is still present and unexpired, dispatch
   the reviewed workflow once from that exact PR #55 merge. This must be a new
   run, not a rerun of failed run `34428188769` or `34440488685`:

   ```bash
   gh workflow run source-published-release-recovery.yml \
     --repo Fifty5D/B-UH-AllianceAuth \
     --ref main \
     --field confirmation='VALIDATE PUBLISHED V0.6.2'
   ```

   Only the repository owner or the exact `BUH_CHATGPT_WORK_ACTOR` may dispatch
   it. Run attempt 1 must complete successfully; do not rerun a failed attempt.
   The canonicalization step must turn the upload action's verified bare digest
   into exactly `sha256:<64 lowercase hexadecimal characters>`; the approval
   consumer must then match it to REST metadata and the downloaded artifact.
   Inspect its `platform-validation-recovery-v0.6.2-6074b965cbd2-<run>-1`
   artifact. Confirm that the new GitHub Actions check
   `Source test suite / Required source checks` is successful on exact commit
   `6074b965cbd2e6ab2630cd539ee455b8d419aef6`, links to this recovery run, and
   retains failed check `102298823162` as history. Confirm the recovery record's
   check ID, GraphQL node, suite ID, completion time, run/attempt, artifact, and
   lane binding, and that GitHub reports that exact check node as required for
   PR #52. Then inspect the bot-authored recovery-readiness comment on PR #52.
   If the protected check is absent, not required, superseded, conflicting, or
   still failing, stop; do not merge with admin bypass.
5. Present that exact validation artifact, unchanged release/manifest, original
   preflight evidence, and generated approval marker to Anthony for the single
   production approval. Before approval, do not merge PR #52. After explicit
   approval, Work may merge PR #52 with a merge commit whose message contains
   the exact generated `buh-chatgpt-approved-recovery:v1` marker. Its ordered
   parents must be the PR #55 digest-repair merge and immutable v0.6.2.
6. The existing `Deploy Production` pull-request event must authorize and deploy
   only v0.6.2. Do not manually dispatch another deployment or retry a failed
   production run. Confirm its retained evidence and final version through the
   normal reporting path.
7. Keep the recovery contract and release hold in place until production v0.6.2
   is confirmed. Retiring this one-time mechanism is a later reviewed PR; it
   must preserve the still-unconsumed platform fragment so the next ordinary
   release is planned as v0.6.3 from synchronized v0.6.2.

Useful read-only checks before steps 2 and 4 are:

```bash
gh pr view 55 --repo Fifty5D/B-UH-AllianceAuth \
  --json headRefOid,baseRefOid,mergeStateStatus,isDraft,statusCheckRollup,labels
gh pr view 54 --repo Fifty5D/B-UH-AllianceAuth \
  --json headRefOid,baseRefOid,mergeCommit,state,mergedAt
gh pr view 53 --repo Fifty5D/B-UH-AllianceAuth \
  --json headRefOid,baseRefOid,mergeCommit,state,mergedAt
gh pr view 52 --repo Fifty5D/B-UH-AllianceAuth \
  --json headRefOid,baseRefOid,state,isDraft,title
gh api repos/Fifty5D/B-UH-AllianceAuth/git/ref/heads/release/platform-v0.6.2
gh api repos/Fifty5D/B-UH-AllianceAuth/git/ref/heads/sync/platform-v0.6.2
```

If preflight artifact `10083806725` (digest
`sha256:d527be28e5f79972835cd2d12c92e6ce98f4c9f77f081e872df76908c6ca0510`)
expires or any pinned identity differs, this continuation is blocked. Do not
relabel old evidence or rebuild v0.6.2; a separately reviewed contract update
must bind a truthful fresh no-change preflight for the same immutable release.

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
