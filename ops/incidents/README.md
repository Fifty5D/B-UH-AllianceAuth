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


## Bounded Structures existing-token pilot

The separate run-structures-pilot.ps1 launcher defaults to report mode.
Apply mode is a production data mutation for exactly one selected configured
Structures character. It refreshes the existing stored token record, verifies
its signed EVE identity and current scopes, checks current corporation identity,
and runs the existing Structures/asset/notification sync methods synchronously.
Only after all three real sync timestamps advance in a committed transaction
does it re-enable the exact stale "No valid token found for character" selector.

Use an exact tested commit and all three reported file hashes. Character
selection is an argument, not production account data committed to this repo.
The helper reuses the complete private report from the qualified read-only
collector and independently checks current eligibility before any mutation.

Token cleanup APIs are forbidden. Permanent OAuth rejection remains fail-closed;
temporary/unclassified failures preserve the disabled selector for later review.
Refresh grant rotation is retained on the same token PK even if a later sync
fails. Native sync writes roll back together on a failed section. All deletion of
links, tokens, owners, Structures records and historical notifications is refused.
Only the native replacement of current StructureItem inventory for the selected
owner is allowed. A provider omission that would prune a historical Structure
stops the pilot and needs explicit incident review.

The operation takes the existing deployment lock without changing the recovery
hold. It verifies current traffic and live Auth runtimes against the retained
previous-image identity, checks public smoke routes and disk headroom, caps the
network interval and incoming assets/notifications, and preserves all retained
deployment resources. It never invokes deployment recovery or cleanup, sends
notification webhooks, installs source, changes Member Audit, or starts v0.8.3.

The report includes fresh refresh outcome, token/link IDs, before/after sync
timestamps and retained counts, committed versus rolled-back steps, private
baseline report digest, image/upstream identities, and safe error categories.
A compact clipboard result accompanies the Desktop/root-private JSON. An
unchanged false is_up may still reflect forwarding or a periodic status check;
it must not be manufactured into a successful current health result.

Apply mode should first be used for a single incident pilot. Subsequent operations
must follow reviewed pilot results. Rerunning uses the same record and does not
create a replacement credential. No output contains a grant, access token, owner
hash, raw OAuth response, arbitrary exception message, or connection secret.


The pilot checks the character's current public corporation with a separate
credential-free, streamed ESI GET. The pinned Structures provider deliberately
does not expose that public character operation. The GET has a 15-second request
limit, the overall pilot deadline, a 16 KiB response cap, no redirects, and
strict JSON/corporation validation. Provider failures leave the selector
disabled without mislabelling a freshly refreshed SSO token as revoked.

Focused checks use the real pinned provider to prove that restriction and
exercise the prepared public HTTP request, native sync methods and retained
rows. Failure reports include only the operation phase and bounded function/line
locations, never exception messages, locals, response bodies or credentials.


The optional run-structures-batch.ps1 launcher reuses the same pilot for an
explicit bounded incident roster. It inspects the already-recovered pilot in
report mode, then verifies two representative owners before continuing in groups
of at most three, sequentially. The first incomplete refresh/sync, preservation
mismatch, changed recovery/traffic identity, excessive disk growth or invocation
with an unknown mutation outcome stops additional apply operations. Every owner
retains its separate root-private report; one combined Desktop report and compact
clipboard result cover the sequence.

Current snapshots read the qualified outage roster, each owner's four native
freshness properties and persisted is_up flag, plus live Member Audit sticky
counts. They never refresh another owner's token, modify forwarding/status
timestamps or clear Member Audit. A successful three-section repair does not
claim that notification forwarding or periodic status evaluation has completed.
Host memory availability and sustained load are checked before and after each
pilot alongside the existing disk/runtime/public-smoke guards. No notification
webhook is manually sent by the recovery tools.


Native Structures replaces current service rows as part of a non-empty structure
sync, just as it replaces current asset items. The recovery guard permits those
two current snapshots only for the selected owner. Each SQL delete must name
exact primary keys already checked by the Django deletion signal; raw deletes,
other owners, credentials, links, Structures, notifications and fuel-alert
history stay blocked. A later sync failure rolls back those replacements while
retaining the refreshed grant on the same token record.

Focused SQLite and MariaDB regression checks exercise the real non-empty native
manager/service/item persistence, another owner's unchanged rows, late failure
rollback, and blocked raw/history deletion. Failure reports identify the safe
model category that was blocked, without SQL, parameters or credentials.

Initial report mode also includes the sanitized Member Audit/token/section and
retained recovery evidence already collected in the qualified incident report.
Its original observation time and SHA-256 remain explicit; it is not a fresh
validation of Member Audit grants and does not repeat the database collection.
The combined private batch report carries this evidence once, rather than
duplicating it for every apply operation.


The pilot launcher caps source archives at 256 KiB and each source file at
128 KiB. Apply reports remain capped at 256 KiB. Read-only report mode admits up
to 10 MiB because it carries the existing sanitized incident inventory (whose
source file is separately capped at 8 MiB), plus bounded live host/owner evidence.
No collection limits, provider limits, deadlines or mutation bounds are relaxed.

Launcher failures include only fixed guard categories, phase/function/line and
byte counts. Raw stdout, stderr and arbitrary exception messages are not exported.
A failure of the initial read-only probe prevents every apply operation. Tests
execute the actual extracted root launcher with synthetic archives and child
reports, including a complete report above the former 256 KiB limit and rejected
oversize/malformed output, wrong mode/schema and source hash mismatch.


## Reconcile the retained attempt after Structures recovery

The run-retained-recovery.ps1 bridge defaults to report mode. It reuses the
already-staged, checksum-qualified pilot sources and the installed receiver;
it does not install replacement application or receiver code.

Recover mode is a production host operation. It takes the existing receiver
lock and calls only the installed DockerHost.recover_incomplete_plan API.
Before that call, every qualified incident owner must currently be enabled,
up, identity/scope complete and fresh for all four native checks, including
notification forwarding. A false is_up or stale forwarding timestamp blocks
cleanup. The exact attempt and retained hold digest must match. The installed
receiver runtime files are checked against its root-owned receipt.

The supported API restores and verifies the previous configuration, images,
replica topology, migrations, Redis/Celery, HTTP/static routes, application
health and new logs before removing its own safety containers/image pins and
retiring the active plan. No global log exception is added. Database migrations
are not reversed and no history/database restore is performed.

One existing database backup, bounded to 1 GiB, receives a streamed SHA-256 check
against BACKUP.json before recovery. Its inode/size/mtime and continued presence
are checked afterward. The bridge also verifies the unchanged service container
identities, exclusive previous-service traffic, current owner health and exact
Auth/token inventories after recovery. It retains a completion receipt in the
existing backup directory only after those checks pass.

The bridge never refreshes credentials, clears Member Audit flags, manually
forwards notifications, deploys a release or performs custom container cleanup.
It does not repeat recovery on any failure, missing plan or unknown outcome.
Failed reports export only fixed guard categories, phase and bounded function/
line information; raw command logs, exception messages and secrets stay private.
The launcher saves one bounded combined Desktop/root-private JSON report.
