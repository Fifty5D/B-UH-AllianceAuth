# B-UH Structure Operations

Source for B-UH's permission-aware structure dashboard, operations schedule,
fuel and extraction monitoring, and operational alerts.

Compatibility and exact release pins live in `../../platform/compatibility.toml`.
Published release wheels are immutable build outputs.

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
