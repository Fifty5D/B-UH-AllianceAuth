# ESI History Archive

Owned source for `aa-buh-max-history`. Distribution, Django label, permissions,
URLs and existing task names retain their identities. The ordinary release
planner versions and builds this application; no receiver patch is needed.

## Collection and cadence

| Source | Stored data | Schedule and limits |
| --- | --- | --- |
| Existing django-esi callers | Distinct successful GET/HEAD response bodies, including pagination and cache hits | Whenever an installed app reads ESI; the caller controls its refresh cadence |
| Active ESI collection | Reviewed authenticated GET endpoints for currently owned characters and their corporations/alliances; related records discovered from authorized responses | One 15-minute beat task; default 120 logical reads per batch; current reads generally hourly, location/online/ship/fleet every 15 minutes, never before the API cache expires |
| Auth business history | Concrete business records from Member Audit, Structures, Moon Mining, Moon Tax and Structure Operations | After committed ORM saves/deletes; a resumable scan catches existing records and bulk updates, default 500 rows per batch; completed scans due again after an hour |
| EVE Ref public datasets | Catalog metadata, complete public downloads and retained revisions | Existing hourly public-mirror schedule; root collection discovery daily; default 12 download attempts and 4 GiB per run |

The reviewed manifest inventories 114 authenticated GET endpoints from the
2026-09-13 official compatibility specification, plus four related public
identity/killmail reads. Protocol headers are supplied by django-esi. Existing
owned-character tokens must match user, character owner hash and all required
scopes. Corporate roles are checked by ESI; denials remain visible and can try
another already-authorized corporation character. This does not grant new
scopes, log into another Auth installation or acquire other players' private data.

Current reads and historical pages have separate persistent cursors. A long
mailbox backfill cannot suspend first-page refreshes. The collector follows
page counts, mail/transaction/calendar cursors and modern `before` cursors;
industry reads include completed jobs and project listings include all states.
Nested reads use identifiers returned by authorized parent responses. Search
endpoints need an explicit query; other undiscovered identifiers appear as
awaiting input in the inventory. The four read-only POST asset name/location
helpers are not actively collected by this GET collector. Public universe and
market coverage comes from provider datasets and existing app reads, rather than
an unbounded live crawl of every public ESI endpoint.

EVE Ref discovery found 37 top-level collections at implementation time. New
collections are enabled while existing enabled/disabled choices are preserved.
Configured subtrees are not downloaded twice through their parents; unconfigured
siblings are discovered. Files directly on a partially configured parent are
reported as `partial_parent` for review. Datasets can overlap semantically;
coverage does not mean every EVE event exists in a provider's archive.

## History and failure recovery

Payloads are compressed and deduplicated per stream. Consecutive observations
form dated spans: A → B → A retains three spans and two payloads. Detailed order
starts with this upgrade; old snapshots keep their original first/last times.
The timestamps record observations, including cache hits, not the exact instant
an in-game state changed. A response page is a snapshot, not a normalized event
ledger. Data already outside an API/provider retention window cannot be rebuilt.

Public replacements retain content-addressed hard links before replacing the
current path. Repeated content shares payload storage. Old stored files gain
revision records on their next download/replacement. Provider `file_time` is
recorded separately from local download/observation time where supplied. A
failed transfer preserves the previous file; missing ESI payloads are repaired
when seen again. Corrupt retained public bytes fail verification rather than
being silently accepted as a valid historical revision.

A stopped worker releases its MariaDB session lock. The next bounded batch
resumes persisted work; SQLite development uses a process-owned file lock.
Public mirroring and active collection have separate locks. Losing a session
stops that collector. Transient failures back off from 15 minutes up to 24 hours;
HTTP 420/429 and django-esi rate-limit exceptions persist a cooldown, respecting
Retry-After/reset. Unavailable scopes retry after six hours. No successful
archive receipt means no cursor advance. Collector initialization failures and
storage guards appear in capture issues/coverage instead of being called saved.

Auth history excludes credential tables and credential-named fields; raw
private payload downloads require their own permission. ORM rollback produces
no committed history. Direct SQL deletes and changes overwritten between scans
cannot be reconstructed. A capture/storage outage can lose an intermediate
business state; subsequent scans recover the current surviving record. Auth
and ESI records remain private even when related ESI routes are public.

## Storage, search and operation

The existing limits are preserved: default 25 GiB free-space reserve and 512 MiB
capture-response limit. Public failures and partial transfers count toward byte
and attempt budgets. Oversized files stay pending and are reported. Discovery,
catalog traversal, downloads and active reads have bounded time/work budgets;
current data, backlog and retries share turns. At a large scale, targets can be
overdue despite their requested cadence: coverage shows actual last success,
next check, backlog, missing access and errors. Increase budgets only after
checking storage, request budgets and observed backlog.

`/esi-archive/history/` searches ESI/Auth source, character ID or record parameters,
filters dates, displays per-target and full endpoint coverage, and exports
metadata in pages of 50. Stream timelines show the latest 100 state spans.
`/esi-archive/public-history/` searches provider files and downloads retained
versions. These are the first browsing/export surfaces; cross-source analytical
reports and full-text search inside compressed bodies are not yet implemented.

History has no automatic age-based deletion. Archive files and the database
must both be backed up consistently. An off-server backup destination is not
configured by this application and must be supplied and restore-tested before
local storage can be treated as durable disaster recovery.

Use the existing PR, validation and immutable release pipeline. Additive
migrations preserve old rows and database defaults support old model writers.
A migration creates one bounded 15-minute history timer, preserving an existing
customized timer of the same name. Run migrations and restart web/workers/beat
with the approved release; keep the archive volume mounted for all of them.
`manage.py buh_archive_report` reports actual configuration, coverage and storage.

The platform recovery hold still applies. A source merge does not publish a
release, install this app, finish recovery, perform cleanup or authorize a
production deployment.
