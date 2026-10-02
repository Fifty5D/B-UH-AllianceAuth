# Staged SSO incident diagnostics

This read-only tool gathers the existing Structures owners, sync characters,
Auth links, token IDs/scopes, Member Audit sticky sections, migration rows,
runtime identities, active nginx route, retained deployment plan and backup
metadata into one indented JSON report.

It also checks public ESI corporation/character identity for affected Structures
owners and the pilot, with no stored credentials. It stops on provider throttling
or repeated transport failures and caps public requests at 64.

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

Fresh MariaDB sessions initialize through the installed Django driver before
the SELECT-only report guard is enabled. Session isolation setup is not an
application-data write. Multiline read-only metadata queries remain permitted;
writes, locking reads and output-file statements remain rejected.
The dedicated regression lane covers the actual
Django shell invocation and a fresh connection against disposable MariaDB.

The retained `previous-static-root` is listed as a directory; it is preserved,
and its contents are explicitly not recursively verified. Unsafe entries remain
visible and incomplete while the other backup entries are still inventoried.

An incomplete database report contains bounded source function/line information
without exception messages, SQL or locals. The known earlier private collector
reports are read only to establish their database failure category.

The launcher also copies a compact summary for a normal chat message. It keeps
the complete JSON on the Desktop and in private host staging. The clipboard
summary does not prove token validity and does not contain credential strings.
