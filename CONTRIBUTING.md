# Contributing to B-UH Alliance Auth

All owned application changes start in `apps/`; published wheels and existing
release directories are immutable outputs and must never be edited in place.

## Development flow

1. Create a branch from the current protected `main` commit.
2. Change source and add focused tests.
3. Add one schema-v1 fragment under `changes/` for every affected application.
4. Run `platform/testenv/run-fast.sh` while developing.
5. Let the pull request run the MariaDB/Redis/Celery/fake-ESI and browser lanes.
6. Merge only after the stable `Required source checks` gate succeeds.
7. Build a candidate from an exact tested commit. Release publication and
   production deployment are separate guarded actions.

Dependency and image inputs are source contracts. Run
`python ops/supply_chain.py verify` for an offline consistency check. Maintainers
may run `refresh-locks` or `refresh-images`, but must review and commit the full
result together; never hand-edit a hash or invent a registry digest. Dependabot
changes direct pins only, so its pull requests remain blocked until the matching
locks are regenerated and reviewed.

Change kinds are `fix`, `security`, `performance`, `internal`, `feature`, and
`breaking`. The release planner owns version increments and fails closed when a
changed application lacks an explicit fragment.

## Safety rules

- Never commit production data, credentials, tokens, SSH material, or unredacted logs.
- Never point tests or previews at production services.
- Keep migrations backward compatible with the deployed code unless an approved,
  tested database restore/forward-fix plan exists.
- Treat permission codenames, named URLs, task names, billing keys, persisted event
  types, manifests, and fixtures as versioned contracts.
- A failed diagnostic or verification step means the release is not deployable.
