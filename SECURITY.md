# Security

Do not commit Alliance Auth `.env` files, database dumps, ESI tokens, Discord
webhooks, SSH private keys, GitHub secrets, or unredacted diagnostic bundles.

The VPS observer uses a dedicated key forced to a fixed read-only entry point.
It cannot open a shell, deploy code, restart containers, or read `.env`.

Deployment credentials and observer credentials must remain separate. Production
deployments will use a protected GitHub Environment, pre-deployment tests,
timestamped backups, health checks, and automatic rollback.
