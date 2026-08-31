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

Source recovery that exactly reproduces an existing wheel does not change the
application version and is recorded in the repository migration history instead.
