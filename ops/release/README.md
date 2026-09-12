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

PR #55 is the immutable digest-handoff repair from PR #54's exact merge. It
merged as `f064694cca7e9d1147a87b880636a94f5c5cbe94`, with reviewed head
`e4ec240abce3736907d4708fe7fa984413d838b7`, onto exact continuation
`57e0e29042bab3a989c80e5473ad552ec5b4505b`; push validation `34638360999`
passed.

Recovery run `34638993007/1` from that merge passed all eight source lanes and
uploaded exact attestation artifact `10279641606` with repository digest
`sha256:2890ce18bd66892972c732a92fbdd82aec22c899a752353375f5e91a8335c37b`.
Its final job created successful required check `103395196156` on immutable
v0.6.2, then failed while validating GitHub's response: GitHub returned the
canonical check URL
`https://github.com/Fifty5D/B-UH-AllianceAuth/runs/103395196156` rather than
echoing the submitted workflow-run URL. The check is valid retained evidence,
but the run did not publish a readiness comment on PR #52. The contract records
that partial publication truthfully, including every job, the attestation byte
hash, artifact, check node/suite, external binding, and canonical URL. It must
not be rerun, relabeled, replaced, or treated as an entirely successful run.

PR #56 is the only permitted publication-continuation repair from PR #55's
exact merge. Its tree may change only the declared workflow, validation,
approval, documentation, fixture, and test paths. After its reviewed merge,
`Continue Published Release Recovery` reuses the exact artifact and already
created check above. The continuation has `checks: read`; it cannot create,
modify, or delete a check. It revalidates all historical and mutable boundaries
and posts only the missing recovery-readiness comment. A different run,
artifact, attestation, check, external binding, URL, app, context, release SHA,
suite, node, or conflicting/superseding matching check fails closed.

The recovered check's submitted `details_url` remains the exact recovery
workflow URL for operator context, while GitHub's returned and reread identity
is required to be the canonical repository/check-ID URL. The verifier checks
that URL during creation, REST/list rereads, recorded readiness, GraphQL
`CheckRun.isRequired`, and final authorization; arbitrary or wrong-ID URLs are
not accepted. Branch protection is evaluated semantically: the exact context
and GitHub Actions app `15368` must remain required with enforcement for
everyone. Additional requirements are preserved and do not invalidate this
evidence merely because GitHub returns a larger list. A newly created check node
may be absent briefly from GraphQL; only an otherwise exact PR response with a
null node is polled for a fixed bound. Errors, malformed responses, conflicting
identity, and non-required nodes fail immediately.

`validation_recovery.py ledger` accepts only the known stale-v0.6.2 ledger
error and the exact reviewed recovery sequence. It constructs a disposable
synthetic merge of the reviewed publication repair with the immutable release,
runs the ordinary ledger verifier on that future tree, and requires the next
plan to be v0.6.3 from v0.6.2. Every other ledger failure or main advance
remains fatal.

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
The continuation separately revalidates original feature PR #51, activation PR
#53, continuation PR #54, digest-repair PR #55, publication-repair PR #56,
completed synchronization repair PR #57, corrective association PR #59, their
pinned push validations, all three failed recovery runs, the original failed
release run, and the retained successful preflight before it may place recovery
readiness on PR #52. Each
merge is associated to its PR by GitHub's commit-to-PR endpoint, while author,
merger, state, head, base and merge identity come from full PR detail.
Authorization repeats the live checks and requires the order PR #53, PR #54,
PR #55, PR #56, PR #57, PR #59, then PR #52.

After all eight reusable-suite lanes pass, the recovery workflow records their
exact job IDs and publishes one GitHub Actions check named
`Source test suite / Required source checks` whose immutable execution
`head_sha` is `6074b965cbd2e6ab2630cd539ee455b8d419aef6`. GitHub may refresh that
historical check's embedded PR #52 association to the live synchronization
head and base. The verifier checks execution commit, app, suite, run, result,
URL, and attestation independently, while requiring any present association to
equal the separately verified live PR #52 repository, branch, head, and base.
An arbitrary or unreviewed association still fails closed. Its report
distinguishes the unchanged release commit/tree, original activation,
continuation, digest repair, two replaced fixtures, and isolated test support.
Its report records the verified workflow path and run and binds repository,
event, actors, head SHA, attempt, attestation artifact and every exact lane name
and ID. GitHub's job `workflow_name` is a
mutable display title, so it is diagnostic only and is never authorization.
Before posting recovery readiness, the verifier requires main's
protected context to remain bound to GitHub Actions app `15368`, confirms the
original failed check remains historical evidence, and records the recovered check's
exact REST ID, GraphQL node ID, check-suite ID, completion time, and evidence
binding. GitHub's `filter=latest` check listing is latest per suite and may
therefore retain older suites; it is not treated as one global winner. The new
check must remain the latest row in its own suite, have no pending or newer
conflicting matching suite, and GitHub GraphQL `CheckRun.isRequired` must report
that exact immutable node as required for the current PR #52. Its check-suite
commit remains the immutable release while the separately queried PR head must
equal the native-tested synchronization head. Authorization repeats both
identities for the merged PR; a private comment or an arbitrary successful row
cannot substitute for it. Failed runs `34428188769` and `34440488685` published no
recovery check. Failed run `34638993007` published the one valid recovered check
and the reviewed continuation later published historical readiness comment
`5641670561`. That schema-7 comment remains audit history for immutable head
`6074b965cbd2e6ab2630cd539ee455b8d419aef6`; it cannot authorize a changed PR
head or base. The deployment workflow stages the recovery-aware verifier from
the exact PR #52 merge before checking out v0.6.2, then uses that staged
verifier at both queued authorization boundaries. The deployment archive and
receiver still come only from the immutable release commit, never from the
updated synchronization head.

The recovery descriptor records the completed PR #57 merge and PR #52's first
update, then holds automatic release creation on the exact PR #59 repair merge
and the later, second PR #52 update. This is a deliberate, bounded continuation
exception: main temporarily contains reviewed unconsumed platform fragments
while the published release is not yet synchronized, but ordinary ledger
validation is never disabled and no new release may start. Any other main
advance, reset of the existing synchronization head, rebase, unexpected
parent/tree, changed immutable ref, missing native check, missing artifact, or
expired artifact fails closed.

### Work activation sequence

Work must perform these steps in order; none is an instruction for Codex or a
routine operator to merge or deploy:

1. Review corrective PR #59 from exact main
   `8795ebf2e82b0bc204047b0b7834ee23cd24edb6`. Require all protected checks,
   applicable Preview UI evidence, trusted readiness, and no `needs-codex`.
   Reconfirm PR #52 is open at existing head
   `4b4a6f2f2e6eeb7bd85010537fb76ac2c2e44ef6`, with base equal to main, and that
   its ordered parents remain immutable v0.6.2
   `6074b965cbd2e6ab2630cd539ee455b8d419aef6` then the completed PR #57 merge.
   Its tree must remain `94094d88bf64f9f65c58e0cc8d724eb8364a9d62`.
   Keep draft policy PR #58 separate.
2. Under the standing authorization for validated non-production repairs, Work
   may merge PR #59 with a merge commit, without squash or rebase. Its ordered
   parents must be `8795ebf2e82b0bc204047b0b7834ee23cd24edb6` and PR #59's
   exact reviewed head, and its tree must equal that reviewed head. If main or
   PR #52 moved, stop instead of updating this contract opportunistically.
3. Require push-triggered `Validate PR` for the PR #59 merge to pass. Inspect
   `Prepare Release` and require the
   `check-association-repair-pending-update` hold result; no v0.6.3 release may
   be built or published.
4. While artifacts `10083806725` and `10279641606` remain present and unexpired,
   use PR #52's normal **Update with merge commit** action exactly once. Do not
   rebase, force-push, add a manual commit, edit PR #52 files, or move the
   immutable release ref. The second sync update must preserve existing head
   `4b4a6f2f2e6eeb7bd85010537fb76ac2c2e44ef6` as parent one and use the exact
   PR #59 merge as parent two. Its tree must equal Git's merge tree for those
   commits. Only `sync/platform-v0.6.2` may move.
5. Wait for native `Validate PR` on that exact second sync head. Require the
   genuine GitHub Actions aggregate `Source test suite / Required source checks`
   to succeed, every additional protected requirement to be satisfied, and the
   PR merge box to be clean. The failed check on the old immutable head and the
   recovered successful check remain unmodified history; neither substitutes
   for native validation of the new head.
6. From the exact PR #59 merge on `main`, dispatch only the synchronized recovery
   workflow once. Do not dispatch or rerun
   `source-published-release-recovery.yml`, do not rerun a failed historical job,
   and do not publish a check manually:

   ```bash
   gh workflow run continue-synchronized-release-recovery.yml \
     --repo Fifty5D/B-UH-AllianceAuth \
     --ref main \
     --field confirmation='PUBLISH UPDATED V0.6.2 READINESS'
   ```

   Only the repository owner or exact `BUH_CHATGPT_WORK_ACTOR` may dispatch it.
   Run attempt 1 must complete successfully. Confirm historical comment
   `5641670561`, its schema-7 nonce, check `103395196156`, failed check
   `102298823162`, artifact `10279641606`, and preflight artifact `10083806725`
   are unchanged and reverified. Confirm exactly one new bot-authored
   `buh-platform-ready-recovery:v2` comment is added. It must bind both the
   completed PR #57 merge/readiness and the PR #59 merge/readiness, the prior
   and second synchronization heads, the new native run/check, immutable
   release, manifest, retained evidence, and the schema-7 record it supersedes.
   A missing, duplicate, stale, expired, or mismatched record blocks continuation.
7. Present the updated native validation, unchanged release/manifest, retained
   recovery/preflight evidence, and newly generated
   `buh-chatgpt-approved-recovery:v2` marker to Anthony for the single production
   approval. Immediately before approval, recheck that main is still the exact
   PR #59 merge, PR #52's head and base still match the new record, the merge box
   remains eligible, and both retained artifacts remain unexpired. The earlier
   schema-7 approval template is ineligible. Before approval, do not merge PR
   #52. After explicit approval, Work may use the normal protected merge-commit
   action with the exact v2 marker. Its ordered parents must be the PR #59 merge
   and the native-tested second sync head; the merge tree must equal the tested
   sync-head tree. Never use an admin bypass.
8. The existing `Deploy Production` pull-request event must authorize the updated
   synchronization identity but select, archive, and deploy only immutable
   v0.6.2 commit `6074b965cbd2e6ab2630cd539ee455b8d419aef6`. Both queued
   authorization checks must reproduce the same result. Do not manually
   dispatch another deployment or retry a failed production run. Confirm its
   retained evidence and final version through the normal reporting path.
9. Keep the recovery contract and release hold in place until production v0.6.2
   is confirmed. Retiring this one-time mechanism is a later reviewed PR; it
   must preserve the still-unconsumed platform fragment so the next ordinary
   release is planned as v0.6.3 from synchronized v0.6.2.

Useful read-only checks before steps 2, 4, 6, and 7 are:

```bash
gh pr view 59 --repo Fifty5D/B-UH-AllianceAuth \
  --json headRefOid,baseRefOid,mergeStateStatus,isDraft,statusCheckRollup,labels
gh pr view 52 --repo Fifty5D/B-UH-AllianceAuth \
  --json headRefOid,baseRefOid,mergeStateStatus,isDraft,statusCheckRollup,state,title
gh api repos/Fifty5D/B-UH-AllianceAuth/git/ref/heads/release/platform-v0.6.2
gh api repos/Fifty5D/B-UH-AllianceAuth/git/ref/heads/sync/platform-v0.6.2
gh api repos/Fifty5D/B-UH-AllianceAuth/issues/52/comments --paginate
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
