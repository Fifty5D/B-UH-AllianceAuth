# B-UH Structure Operations

Source for B-UH's permission-aware structure dashboard, operations schedule,
fuel and extraction monitoring, and operational alerts.

Compatibility and exact release pins live in `../../platform/compatibility.toml`.
Published release wheels are immutable build outputs.

## Sync concurrency guards

The app serializes refresh and invalid-grant deletion for django-esi 9.6.0 using
a database lock on each token row. A caller holding an older token instance
reuses a sibling's successful refresh. A changed user/character ownership stops
the operation; rejected grants remain failures, and incomplete SSO responses
preserve the grant without returning an expired token. These guards do not
recover previously disabled sync characters or change stored success flags.

For Moon Mining 3.1.0.post1, the two time-based SQL transitions run atomically
and retry MariaDB error 1213 at most twice. Other errors and errors inside a
caller's existing transaction propagate. ESI calls and notification processing
are not replayed by this retry.

The guards are limited to these exact dependency versions and start with Django
after an approved application release is installed. They do not install
themselves on existing workers, change permissions, or clear the recovery hold.

## Discord guild-owner nickname exclusion

Discord does not permit a bot to change the guild owner's nickname. Alliance Auth
5.2.0 has no owner-exclusion setting, so a deployment may identify that one account
by its Discord user ID in the private `conf/local.py` file:

```python
BUH_DISCORD_GUILD_OWNER_ID = 123456789012345678
```

Use Discord's **Copy User ID** action on the confirmed guild owner and replace the
example value with that decimal ID. Back up `conf/local.py` before the separately
approved configuration change. The guard becomes active when an approved release
containing this application starts; it logs each owner nickname skip as a warning.
It does not change the five-minute schedule, ordinary-member nickname updates,
Discord role updates, username synchronization, retries, or error handling.

## Shared console appearance

`console-theme.css` uses Moon Tax's palette and applies only to Moon Tax,
Structure Operations, its schedule, ESI History Archive, and VPS Health (including their
dialogs and toasts). The release's existing `buh_structure_ops_setup` command
installs it through Alliance Auth's built-in Custom CSS feature. A marked block
is updated idempotently; administrator CSS outside that block is preserved.
Dry runs do not write it. Ambiguous markers stop setup instead of replacing
unrelated styling. No legacy add-on packages, application endpoints, permissions,
tasks, or JavaScript behavior are replaced by this theme.

The stylesheet intentionally contains only flat rules compatible with Auth's
CSS compressor. Responsive layout rules remain in the original app stylesheets.
Darkly and Bootstrap Dark retain the charcoal palette. Bootstrap, Flatly, and
Materia use light panels with darker text and accents; the familiar banner stays
dark in every theme. Long tables scroll within a bounded viewport with opaque
sticky headers, preserving native sorting and horizontal column alignment.
The managed block is presentation data: removing it in Auth's Custom CSS admin
restores the original add-on appearance without changing application data. The
next setup run reapplies the version shipped in the installed wheel.
