# Contributing to B-UH Alliance Auth

All owned application changes start in `apps/`; published wheels and existing
release directories are immutable outputs and must never be edited in place.

## Development flow

1. Create a branch from the current protected `main` commit.
2. Change source and add focused tests.
3. Add one schema-v1 fragment under `changes/` for every affected application.
4. Run `platform/testenv/run-fast.sh` while developing.
5. Let the pull request run its required lanes. A proven prose-only diff skips
   runtime lanes; code and unknown diffs run the full suite.
6. For a UI-relevant change, require the automatic synthetic **Preview UI** run
   (or the explicit `ui-preview` override) to succeed for the exact current head.
7. Apply `ready-for-work` only after
   `Source test suite / Required source checks` and every applicable preview pass
   for that same head. The trusted readiness record must bind that head, contain
   no unresolved serious review finding, and leave `needs-codex` absent.
8. ChatGPT Work performs the final risk review and merges that exact qualified
   non-production head under Anthony's standing authorization; no repeated merge
   approval is needed. A stale label or record, a changed head, an expired preview,
   a pending or failed check, or a new serious finding blocks the merge.
9. Release automation builds from the exact tested merge when recovery is clear.
   During an active recovery hold, features accumulate on main with their change
   fragments; no additional repair PR is needed to keep that hold valid.
10. Request one production approval for the concrete immutable candidate and
    preflight evidence. Receiver maintenance and recovery cleanup retain their
    own exact approvals. See [the delivery process](docs/operations/delivery-process.md).

Dependency and image inputs are source contracts. Run
`python ops/supply_chain.py verify` for an offline consistency check. Maintainers
may run `refresh-locks` or `refresh-images`, but must review and commit the full
result together; never hand-edit a hash or invent a registry digest. Dependabot
changes direct pins only, so its pull requests remain blocked until the matching
locks are regenerated and reviewed.

Change kinds are `fix`, `security`, `performance`, `internal`, `feature`, and
`breaking`. The release planner owns version increments and fails closed when a
changed application lacks an explicit fragment. Changes to registered platform
inputs (including browser tests and synthetic test setup) also require an
`app = "platform"` fragment. Validate the release plan against the latest
synchronized `RELEASE.json` before merging, not just after Validate PR passes.

## Safety rules

- Never commit production data, credentials, tokens, SSH material, or unredacted logs.
- Never point tests or previews at production services.
- Keep migrations backward compatible with the deployed code unless an approved,
  tested database restore/forward-fix plan exists.
- Treat permission codenames, named URLs, task names, billing keys, persisted event
  types, manifests, and fixtures as versioned contracts.
- A failed diagnostic or verification step means the release is not deployable.
