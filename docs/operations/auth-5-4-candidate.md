# Alliance Auth 5.4.0 and public archive candidate

This is a candidate runbook. Source tests, publication, production preflight, and
installation are separate states. Do not infer production health from a green PR.

## Verified September 27 baseline

- The host's current receipt identifies B-UH platform v0.7.0 at source
  `02a3e0ee84b667797e3521e9ade47e5cb3f3ec27`. Running Auth reports 5.2.0,
  Django 5.2.15, django-esi 9.6.0, Member Audit 5.0.4, Structures 4.0.3, and the
  bundled Moon Mining 3.1.0.post1. The host's absence of a recovery hold was
  checked directly. These values must be read again during preflight.
- The hourly public archive schedule is enabled. Recent sync jobs downloaded
  some files and then failed, so neither an empty queue nor partial bytes prove
  archive health. The September 4 stalled row was marked failed by the
  scheduler on September 27; it is no longer running.
- Provider-advertised EVE Ref child indexes return HTTP 404, while the parent
  still advertises them. Some file responses are complete but their size and
  validators differ from the lagging catalog; the old collector rejected them.
  A root catalog 404 remains a hard error. The candidate records child gaps as
  `UNAVAILABLE`, rechecks them daily, verifies response length, and preserves
  previous file revisions when the provider changes complete content.
- About 3.54 GB of public file content was stored at inspection, with large
  pending and historical failed backlogs. The catalog advertises roughly 2 TiB;
  the host has roughly 215 GiB free. This release does not promise a complete
  mirror. Operational log retention and archive data retention are separate.

## Candidate dependency and migration boundaries

The target is Alliance Auth 5.4.0, not an automatic upgrade to a newer version.
The upstream [5.4.0 release notes](https://gitlab.com/allianceauth/allianceauth/-/releases/v5.4.0)
and [update instructions](https://allianceauth.readthedocs.io/en/latest/installation/allianceauth.html#updating)
were reviewed for this target.
The exact upstream runtime image is digest-pinned in `platform/compatibility.toml`.
The candidate pins Django 5.2.17, django-esi 9.6.0, and aiopenapi3 0.10.0. A
freshly resolved aiopenapi3 0.11.0 broke django-esi's session factory in the
disposable environment; keeping 0.10.0 is a demonstrated runtime requirement,
not an unrestricted compatibility claim. The owned apps' constraints permit
only Auth 5.4.x. Installed community app package metadata permits Auth 5.x,
but metadata alone does not establish behavior; source CI exercises startup,
permissions, browser flows, Celery, fake ESI, MariaDB, and the app tests.

Compared with the official 5.2.0 wheel, the 5.4.0 migrations under `admin_status`
and `menu` are additive in the reviewed candidate. The new MariaDB migration
contract creates synthetic 5.2-era menu and announcement records, applies
those 5.4 migrations, and checks both records survive. The existing legacy
upgrade/restore lane rehearses a full database restore independently. Neither
test is a clone of the production database.

## Before and during the approved deployment

1. Require the exact feature PR head, complete required source/preview checks,
   trusted readiness record, non-production merge, immutable release manifest,
   synchronized release state, and a fresh no-change production preflight.
   Recheck deployed source, recovery hold, capacity, current jobs, and service
   versions. Do not reuse an older preflight or approval for a changed payload.
2. Use the existing Platform v2 deployment flow. It makes a bounded read-locked
   MariaDB dump before migrations, records evidence table counts, restores the
   dump into an isolated temporary database, and checks the restored counts.
   Retain the attempt's root-only backup and receipt. Record the timestamp at
   which writes resume so post-backup changes can be identified.
3. Apply the candidate migrations and image only through that flow. Verify the
   actual image and package versions for every Auth service, migration plan,
   Django checks, web/login and permissions, static assets, worker/beat health,
   Celery registration, and absence of restart loops. Do not equate a GitHub
   publication or passing preflight with installation.
4. Verify a new public archive sync **finishes** without collection errors;
   inspect its `provider_gaps` separately. Verify actual file rows advance to
   `STORED`, byte counts and SHA-256/revision files match, and next retry times
   are bounded. The 404 child indexes may still exist upstream; they should be
   visible gaps, not silent success. Check ESI history capture separately.
5. Install the independent diagnostic collector/publisher only after the same
   explicit production approval covers it. Verify a live report and at least one
   redacted detailed shard through the ChatGPT GitHub connector. Confirm source
   freshness and actual earliest retained timestamps; thirty days cannot exist
   immediately after first installation if earlier logs rotated.

## Failure and recovery

If preflight, backup restore, migration, service checks, archive data checks, or
diagnostic retrieval fails, keep the attempt failed and preserve its backup,
logs, and receipts. The receiver can restore previous application files/images
where its state machine permits, but rolling an image back does not undo a
database migration. Treat the 5.2 runtime against a 5.4 schema as unverified.

Prefer a tested forward correction when live data has already changed. If a
database restore is required, first quiesce writers and capture the current
database and post-backup write interval for reconciliation. Restoring the
pre-migration dump removes every write after that dump unless those changes are
replayed or separately reconciled. Verify owners, Member Audit records, archive
rows/revisions, timers, tax ledgers, and ESI history before declaring recovery.
Do not replace a live database based solely on a failed diagnostic relay.

Remaining upgrade-sensitive points include direct package pins, upstream image
digest, third-party compatibility, migration behavior, release/readiness lineage,
and the live provider's mutable catalog. These are deliberate review gates for
future upgrades.
