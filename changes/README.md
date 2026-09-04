# Change fragments

Every functional change to an owned application must add one TOML fragment:

```toml
app = "moon-tax"
kind = "feature"
summary = "Add a director aging report."
```

Supported kinds are `fix`, `security`, `performance`, `internal`, `feature`,
and `breaking`. The release builder chooses the highest required version bump.
Fragments are consumed only when an immutable release is published.

An exceptional, platform-only recovery may declare
`deployment_predecessor = "X.Y.Z"` on a single `kind = "fix"` fragment.
The builder accepts it only when the named immutable release, every skipped
release, and the candidate have identical receiver-visible artifacts,
compatibility, and install plans. The consumed fragment makes the bridge
one-time; the following release returns to the immediate ledger predecessor.

Source recovery that exactly reproduces an existing wheel does not change the
application version and is recorded in the repository migration history instead.
