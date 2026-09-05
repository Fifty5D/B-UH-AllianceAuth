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
- Run `platform/testenv/run-fast.sh` before pushing. Let the complete Source CI suite
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

- Review for authentication and permission regressions, Moon Tax calculation and
  accounting integrity, migration and retention safety, backup/release/deployment
  behavior, unintended feature changes, theme readability, responsive layout,
  sticky tables, and missing tests.
- Do not merge, publish a release, dispatch deployment workflows, retry production
  jobs, or deploy without the user's explicit approval.
- Production deployments must use the exact immutable release built from the tested
  commit. A merge/deploy approval applies only to the named pull request and SHA.
