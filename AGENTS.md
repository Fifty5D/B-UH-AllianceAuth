# Project working agreement

This repository deploys a live Alliance Auth installation. Treat authentication,
permissions, accounting, migrations, backups, releases, and deployments as
production-critical paths.

## Before changing code

- Read `CONTRIBUTING.md`, the relevant app documentation, and the current code and
  tests before editing.
- Work on a feature branch or the existing pull-request branch. Never push directly
  to `main`.
- Preserve existing behavior, JavaScript-facing IDs, permissions, and accounting
  rules unless the request explicitly changes them.
- Never edit immutable artifacts under `releases/` or committed wheel files.

## Implementation and validation

- Add focused regression tests for changed behavior and one schema-v1
  `changes/*.toml` fragment for every affected app.
- Browser tests, synthetic test setup, CI, and other registered platform build
  inputs also require an `app = "platform"` fragment. Before merging, validate the
  release plan against the latest synchronized `RELEASE.json`; passing source
  tests does not by itself prove that release metadata is complete.
- Run `platform/testenv/run-fast.sh` before pushing. Let the complete Validate PR suite
  validate MariaDB, Redis, Celery, fake ESI, migrations, backup restoration, and
  browser behavior.
- Inspect failures and fix the same pull request until
  `Source test suite / Required source checks` succeeds. Do not call work complete
  while relevant checks are pending, failed, or unrun.
- Keep secrets, production data, credentials, and unredacted logs out of commits,
  pull requests, test fixtures, and diagnostics.

## User interface changes

- Use Moon Tax as the shared visual baseline unless the request says otherwise.
- Verify Darkly, Flatly, Materia, Bootstrap, and Bootstrap Dark at desktop and
  mobile widths. Normal text must meet 4.5:1 contrast and large text 3:1.
- Long tables must retain sticky headers, horizontal scrolling, column alignment,
  sorting, links, and existing interactions.
- Prefer shared design tokens and scoped selectors so one Auth theme cannot leak
  unreadable Bootstrap defaults into an app surface.

## Review and release boundaries

- The implementer owns source changes, focused tests, synthetic preview preparation, and
  creation or updates of the single feature pull request targeting `main`. After
  the exact head's checks and applicable preview are green, the implementer applies
  `ready-for-work`. The
  trusted handoff policy removes that request while it validates and publishes
  the exact-head record, then re-adds it as the final signal. The implementer stops only
  after that handoff succeeds; any head, check, preview, or review change
  invalidates it and requires the implementer to apply it again after the head is green.
- A capable, authorized operator owns merge of a validated non-production PR,
  release/preflight evidence verification, the applicable owner-approval request,
  production execution, monitoring, and final reporting. The operator may be the
  implementer only after a genuinely separate independent reviewer has reviewed
  the exact final commit and, before an operational action, its exact payload and
  evidence. The reviewer must not author or amend that implementation, prepare or
  execute that payload, or approve their own work. Resolve blocking findings and
  repeat review when a bound commit, payload or evidence identity changes.
- These are responsibility and capability requirements, not product names. A
  changed task title, mode label or receipt owner string supplies neither access
  nor independence. Retain every platform, user, GitHub and environment approval,
  least-privilege permission and technical gate. Independent agent review never
  substitutes for a required human or GitHub approval.
- Only one authorized operator may write to production at a time. Preserve the
  existing locks, backups, rollback, recovery holds and immutable evidence. Other
  agents remain read-only during that operation. Rejection uses `needs-codex`
  and includes an exact remediation prompt for the same PR.

- Review for authentication and permission regressions, Moon Tax calculation and
  accounting integrity, migration and retention safety, backup/release/deployment
  behavior, unintended feature changes, theme readability, responsive layout,
  sticky tables, and missing tests.
- Anthony's standing authorization lets the authorized operator merge a qualified
  non-production PR after independent final risk review without asking for merge permission
  again. Source changes to deployment tooling are non-production until installed.
  The implementer still hands exact reviewable evidence to the independent
  reviewer. Explicit per-task limits override this rule.
- Standing non-production merge approval does not cover release publication,
  synchronization merges that initiate production, workflow dispatch/retry,
  receiver installation, recovery completion/cleanup, or production deployment.
  Those actions require the applicable separate owner approval. Never carry an
  approval into a different payload or replay a consumed production attempt.
- Production deployments must use the exact immutable release built from the tested
  commit. Production approval binds that release and its associated synchronization
  head, base and evidence; it cannot be reused after those identities change.

## Council discussion and votes

- Consult the five-member council before significant decisions. Whenever a
  council is called, give all five members the same question, options, evidence
  and authority limits, plus peer task IDs and a supported direct agent-message
  channel. Members must exchange positions, challenge alternatives and answer
  one another before each records an explicit vote and rationale.
- Record all five votes, abstentions, the tally, the recommendation and unresolved
  dissent. If direct discussion or a member's vote is unavailable, disclose that
  limitation; never invent participation or consensus. Council votes recommend a
  course of action; they grant no permissions and waive no approval or gate.
  A tie or no recommendation requires an owner decision; unresolved blocking
  findings remain blocking even when a majority favors proceeding.
- Strategy discussion is not final artifact review. The independent reviewer must
  still inspect the exact final commit and payload under the separation above.

## Adoption and existing contracts

- A governance amendment cannot authorize its own adoption. The existing
  authorized owner/process must independently review and adopt it before the new
  responsibilities apply. A local proposal or draft PR changes no production
  authority and does not lift a hold.
- Preserve the legacy `codex`, `ready-for-work`, `needs-codex` labels, receipts,
  `work_actor` field and `BUH_CHATGPT_WORK_ACTOR` variable. Their names are
  compatibility contracts; the configured owner/operator GitHub identity
  allowlist is a security control and remains mandatory. This policy neither
  changes that allowlist nor creates credentials or access.
- Existing immutable evidence, consumed attempts and frozen qualifiers retain
  their original requirements. Obtain the genuine review those contracts require;
  never relabel a reviewer or edit frozen bytes to claim compliance. A replacement
  contract needs its own reviewed, tested, separately qualified artifact and the
  applicable authorization. Historical failures and unknown outcomes stay intact.

## Avoid repeated work

- Read `docs/operations/delivery-process.md` for the current delivery and recovery
  state. Check live GitHub once at the start; screenshots and old handoffs are
  historical evidence, not current branch or host state.
- Diagnose the complete remaining recovery checks together before proposing a
  receiver repair. Use the staged `recovery_audit` tool; diagnosis does not need
  another receiver installation. Keep fatal findings visible and explain their
  cause before changing classification policy.
- Keep one PR for a coherent change. Fix failed tests on that PR, batch independent
  reads, and request readiness once its final head is qualified. Do not ask for
  approval again for an action already authorized in the session.
- Use automatic UI relevance. Do not add `ui-preview` to server-only repairs
  unless a concrete visual risk requires it. Prose-only PRs use the conservative
  CI scope; code, workflow, test and uncertain diffs run all source lanes.
- While `recovery-hold.json` is active, feature work and qualified source merges
  continue. Release preparation and all receiver access remain held. Never add a
  feature's SHA to a historical repair allowlist just to make the release hold pass.
- Final reports state: source merged, release published, receiver installed,
  production verified, and the next necessary action. Report only states backed
  by evidence; do not call a green GitHub build a successful production deployment.
