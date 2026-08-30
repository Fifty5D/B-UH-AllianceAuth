# B-UH Alliance Auth

Private source, tests, guarded deployment tooling, and support utilities for the
Bureau of Unified Harvesting Alliance Auth installation.

## Guardrails

- Source changes are tested in GitHub Actions before deployment.
- Production changes use timestamped backups, health checks, and rollback.
- The diagnostics key is read-only and cannot open a shell or read `.env`.
- Deployment and diagnostics use different SSH users and keys.
- Secrets stay in GitHub Actions secrets and are never committed.

## VPS diagnostics console

Repository issue **#1** is the private diagnostics console. After the one-time
observer setup, the repository owner can comment `/diagnostics 15` to collect a
sanitized 15-minute status and log report. Supported windows are 5, 15, 30, 60,
180, and 360 minutes. Longer windows are intended for diagnosing completed
one-shot jobs whose final logs are no longer recent.

The report includes host pressure, every current Compose container, container
resource usage, health and exit states, Celery workload, and bounded recent logs.
New Compose services are discovered automatically.

## One-time observer setup

1. Download this private repository as a ZIP and extract it on Windows.
2. Open PowerShell in the extracted folder.
3. Run:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\setup\Setup-BUH-GitHubObserver.ps1
   ```

4. Add the three printed repository variables and two repository secrets under
   **Settings → Secrets and variables → Actions**.

Never paste the generated private key into chat.

## Emergency Discord token rotation

If a Discord bot token must be replaced, reset it in the Discord Developer
Portal, download the latest repository ZIP, and run:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup\Rotate-BUH-DiscordToken.ps1
```

The helper accepts the new token through a hidden prompt, validates it directly
with Discord, creates a timestamped `local.py` backup, runs Django checks,
recreates the Auth services, rolls back on failure, and reinstalls the hardened
diagnostics observer. The token is never printed or placed in a command line.
