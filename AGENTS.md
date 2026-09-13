# Codex working agreement

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

- Codex owns implementation, focused tests, synthetic preview preparation, and
  creation or updates of the single feature pull request targeting `main`. After
  the exact head's checks and applicable preview are green, Codex applies
  `ready-for-work`. The
  trusted handoff policy removes that request while it validates and publishes
  the exact-head record, then re-adds it as the final signal. Codex stops only
  after that handoff succeeds; any head, check, preview, or review change
  invalidates it and requires Codex to apply it again after the head is green.
- Codex never merges, publishes, approves, or deploys. ChatGPT Work owns final
  risk review, merge of a validated non-production PR, release/preflight evidence
  verification, the single production-approval request, deployment monitoring,
  and final reporting. A Work rejection uses `needs-codex` and includes an exact
  remediation prompt for the same PR.

- Review for authentication and permission regressions, Moon Tax calculation and
  accounting integrity, migration and retention safety, backup/release/deployment
  behavior, unintended feature changes, theme readability, responsive layout,
  sticky tables, and missing tests.
- Anthony's standing authorization lets ChatGPT Work merge a qualified
  non-production PR after final risk review without asking for merge permission
  again. Source changes to deployment tooling are non-production until installed.
  Codex still hands the PR to Work. Explicit per-task limits override this rule.
- Receiver installation, recovery completion/cleanup, and production deployment
  each require the applicable exact owner approval. Never carry an approval for
  one payload into a different payload or replay a consumed production attempt.
- Production deployments must use the exact immutable release built from the tested
  commit. A merge/deploy approval applies only to the named pull request and SHA.

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
