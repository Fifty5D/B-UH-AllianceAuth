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

Changes to registered platform inputs, including browser tests, synthetic test
setup, CI, and release tooling, require a separate platform fragment even when
an application fragment is already present:

```toml
app = "platform"
kind = "internal"
summary = "Expand synthetic browser coverage for the updated application."
```

Run the release planner against the latest synchronized release manifest before
merging so missing or stale application and platform fragments are caught early.

An exceptional, platform-only recovery may declare
`deployment_predecessor = "X.Y.Z"` on a single `kind = "fix"` fragment. The
builder accepts it only when that version is the baseline in the exact reviewed
recovery policy and every intervening immutable release verifies in order
without crossing the current compatibility boundary. The candidate still names
the latest ledger release as its immediate `previous_release`; separate recovery
metadata authorizes only the verified live-host transition. The consumed
fragment makes the authorization one-time, and the following release returns to
an ordinary direct deployment transition.

Source recovery that exactly reproduces an existing wheel does not change the
application version and is recorded in the repository migration history instead.
