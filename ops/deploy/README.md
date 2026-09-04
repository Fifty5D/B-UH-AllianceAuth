# Platform v2 production receiver

This directory contains the separately gated Platform v2 deployment path. It
does not replace or modify the legacy Moon Tax receiver during repository tests,
release building, or installation. Production activation is a distinct,
reviewed host operation.

## What the receiver guarantees

- Only the exact forced commands `preflight platform-v2` and
  `deploy platform-v2` are accepted.
- One nonblocking host `flock` serializes preflights and deployments.
- Request, receiver, release, install-plan, compatibility, and artifact fields
  are parsed fail closed; an unknown security-sensitive field is an error.
- The gzip/USTAR payload is bounded and extracted without following links or
  accepting traversal, PAX metadata, Unicode aliases, nested paths, or special
  files.
- The release must match its exact manifest SHA-256 and the configured GitHub
  repository.
- The release must name one digest-pinned production base image and the host's
  `AA_DOCKER_TAG` must equal it exactly.
- Existing and third-party wheels are installed in stable Docker layers before
  changed owned wheels, so normal releases reuse most of the image cache.
- Candidate package versions, Django checks, and the migration plan pass before
  live Auth containers are replaced.
- A pre-migration database dump is restored into an isolated, memory-backed
  MariaDB container using the exact running database image. Accounting-table and
  migration row counts must match before migration begins.
- Deployment states are journaled atomically. `current.json` changes only after
  container, package, migration, Django, Redis, Celery, log, and HTTPS checks all
  pass.
- Code/configuration rollback is automatic after a failed swap. Database restore
  is intentionally never automatic; migrations must remain backward compatible,
  and the verified backup is retained for an explicit recovery decision.

## Repository validation

Run the small host-independent suite with:

```bash
python -m unittest discover -s tests/deploy -p 'test_*.py'
```

The required source workflow also reconstructs the immutable legacy v0.3.3
schema in MariaDB, seeds synthetic accounting evidence, dumps it, upgrades it,
restores the dump into a second database, upgrades that copy, and verifies the
evidence again.

## Production bootstrap (one time, not automatic)

Do this only after the pull request and hosted checks pass. It changes the SSH
forced-command boundary and therefore requires a separately reviewed maintenance
window.

1. Use read-only host inspection to confirm the Compose filename, service names,
   existing legacy receiver path, and current `AA_DOCKER_TAG`. Do not copy
   environment contents or credentials into GitHub, chat, or diagnostics.
2. Resolve the current production image to its immutable repository digest.
   Add that exact `name:tag@sha256:...` as
   `production_runtime.base_image` in `platform/compatibility.toml`, run all
   source checks, and publish a new immutable Platform release. A tag-only image
   is rejected.
3. Copy `receiver-config.example.json` to a root-only working file, adjust only
   the verified path/service values, and serialize it as canonical JSON. Validate
   it with `ReceiverConfig.load` before installation.
4. From the exact reviewed checkout, install but do not enable the receiver:

   ```bash
   sudo ops/deploy/install-receiver.sh \
     /root/receiver.json /absolute/path/to/the/current/legacy-receiver buh-deployer
   ```

   The installer copies only trusted Python modules, installs exact sudo rules,
   creates private state/backup directories, and preserves the legacy receiver.
   It deliberately does not read or edit `authorized_keys`.
5. Review the existing deploy user's forced-command entry locally. Retain its
   existing public-key material and restrictions, changing only its fixed command
   to invoke:

   ```text
   /usr/local/sbin/buh-deploy-dispatch /absolute/path/to/the/current/legacy-receiver
   ```

   The dispatcher routes the old `deploy moon-tax` command unchanged and adds
   only the two Platform v2 commands. Never send the key material anywhere.
6. Run **Deploy Platform v2** in `preflight` mode with the exact published release
   version/commit and confirmation `PREFLIGHT PLATFORM V2`. It must pass and
   produce sanitized observer diagnostics before a real deployment is considered.
7. During an approved window, rerun in `deploy` mode with confirmation
   `DEPLOY PLATFORM V2`. Do not call the release live unless the workflow and
   observer diagnostics both succeed.

Legacy v1 remains available until a complete Platform v2 production cycle and a
separately approved rollback drill have succeeded.
