# ESI History receiver configuration maintenance

Run this bounded command only after the reviewed worker recovery has completed.
It rejects an active recovery, validates the recovery completion receipt and
current receiver provenance, and adds only
`setup_arguments.buh_archive_setup: ["--no-color"]`.

From the exact reviewed, root-owned source, first run the default read-only
plan. Set `PYTHONPATH` to that source directory because `-P` excludes the
working directory from Python's import path.

```bash
sudo env PYTHONPATH=/path/to/exact-reviewed-source \
  /usr/bin/python3 -B -P -m ops.deploy.history_config_maintenance plan
```

Retain the three hashes returned by that exact plan and supply them unchanged
to apply:

```bash
sudo env PYTHONPATH=/path/to/exact-reviewed-source \
  /usr/bin/python3 -B -P -m ops.deploy.history_config_maintenance apply \
  --old-config-sha256 OLD_CONFIG_SHA256 \
  --new-config-sha256 NEW_CONFIG_SHA256 \
  --provenance-sha256 PROVENANCE_SHA256
```

Apply takes the normal receiver host lock, creates a root-only evidence backup,
atomically replaces `receiver.json`, and writes the additive
`HISTORY-CONFIG-MAINTENANCE.json` receipt. It preserves the original
`INSTALL.json` and earlier repair receipts. It does not run the setup command,
deploy a release, restart services, or clean recovery resources. A later normal
deployment can invoke the newly allowed setup command.

Repeating apply with the same three hashes verifies the installed config,
receipt, backup, and live provenance and returns `already-applied`. An
activation failure restores the prior config. A subsequent retry can reuse the
same complete, exact backup; a partial or different backup fails closed.
