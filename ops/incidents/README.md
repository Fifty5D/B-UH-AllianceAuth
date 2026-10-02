# Staged SSO incident diagnostics

This read-only tool gathers the existing Structures owners, sync characters,
Auth links, token IDs/scopes, Member Audit sticky sections, migration rows,
runtime identities, active nginx route, retained deployment plan and backup
metadata into one indented JSON report.

It targets retained attempt `gh-36955595351-1`. It uses the receiver already
installed on the server for Compose discovery, including existing overlays.
It does not install the staged code into the application or receiver.

Run the checksum-pinned launcher with the owner's existing `b-uh` SSH profile.
Noninteractive sudo is required; no local Git checkout is needed:

```powershell
.\ops\incidents\run-sso-incident.ps1 -ReviewedCommit <qualified-40-character-head> -CollectorSha256 <qualified-collector-sha256> -DatabaseSha256 <qualified-database-sha256>
```

The JSON file is saved to the Windows Desktop and
retained in a private root staging directory. An incomplete report still saves
its safe error categories. No arbitrary exception messages, raw logs, token
credentials, owner hashes, database connection settings or environment values
are exported. Treat account IDs and character mappings as private incident data.

The tool performs SELECT-only database queries; it never calls token refresh,
`require_valid()`, token cleanup, status reset or native sync methods.
A scope-complete token is reported as **not yet refresh-tested**. Disabled
characters and stored error flags are not classified as revoked credentials.
Current rows cannot reconstruct deleted links/tokens without earlier evidence.

Bounds are explicit. Overflows fail the corresponding report section rather
than silently declaring a partial scan complete. Files above 1 MiB, including
database/static backups, receive metadata rather than a costly full hash.
Raw Docker metadata/logs are captured only inside the root process and projected
to permitted fields. Current owner errors bind an exact owner/message shape and
timestamp; they are not added to a deployment ignore list.

The report does not complete rollback, remove a recovery hold, clean retained
containers/backups, change traffic, start deployment, disable owners or requeue
Member Audit. Those decisions require the collected evidence and successful
validation of the existing credentials.

These staged operations files and their dedicated tests/workflow are outside
the application payload and registered platform build inputs. They do not
republish or change the immutable v0.8.3 release.
