# B-UH Alliance Auth

Private source, tests, guarded deployment tooling, and support utilities for the
Bureau of Unified Harvesting Alliance Auth installation.

## Source-first platform

Owned applications now live under `apps/` as normal buildable Python projects.
`platform/` defines the permanent disposable test environment, compatibility
contract, synthetic fixtures, and fake services. Exact build outputs remain in
append-only release directories.

The production deployment path is deliberately separate from CI. Pull requests,
previews, and source tests never receive production credentials or database data.

## Guardrails

- Source changes are tested before release or deployment.
- Production remains manually approved, serialized, checksum verified, and rollback aware.
- Existing `v0.3.x` bundles and the legacy Moon Tax receiver remain available for rollback.
- The diagnostics key is read-only and cannot open a shell or read `.env`.
- Deployment and diagnostics use different SSH users and keys.
- Secrets stay in GitHub Actions secrets and are never committed.
- Application and platform versions are tracked independently.
- Published release directories are immutable.

## Test lanes

- **Fast:** Ruff, compilation, JavaScript syntax, migrations, and targeted Django tests.
- **Integration:** MariaDB, Redis, a real Celery worker, fake ESI, repeatable fresh
  migrations, and concurrency checks.
- **Browser:** Chromium checks for sorting, row navigation, director controls, and
  permission boundaries using only synthetic identities.
- **Compatibility:** scheduled revalidation of the pinned production-compatible stack;
  next-version canaries remain a documented Platform v2 promotion gate.
- **Supply chain:** exact transitive Python hash locks, exact PEP 517 build pins,
  immutable test-image digests, and a weekly read-only drift report with a
  reviewable patch.

See `docs/architecture/source-first-platform.md` for the rollout and safety gates.

## VPS diagnostics console

Repository issue **#1** is the private diagnostics console. After the one-time
observer setup, the repository owner can comment `/diagnostics 15` to collect a
sanitized status and log report. Supported windows are 5, 15, 30, 60, 180, and
360 minutes.

## Emergency Discord token rotation

If a Discord bot token must be replaced, reset it in the Discord Developer Portal,
download the latest repository ZIP, and run:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup\Rotate-BUH-DiscordToken.ps1
```

The helper accepts the token through a hidden prompt, validates it directly,
creates a timestamped backup, runs checks, recreates Auth services, and rolls back
on failure. The token is never printed or placed in a command line.
