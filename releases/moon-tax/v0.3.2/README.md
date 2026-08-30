# B-UH Moon Tax 0.3.2

This deployment-only patch preserves the complete Moon Tax 0.3.1 application
and fixes the checked wheel permissions used by the non-root Docker build user.

The v0.3.1 deployment reached the VPS after the IONOS outage recovered, but
Docker could not read the newly copied Moon Tax wheel. The guarded installer
stopped before replacing any live containers and restored the previous Auth
configuration.

Changes in this release:

- release wheels are installed into the Docker build context with mode `0644`;
- the guarded root deployer now stages future wheels with mode `0644` as an
  additional defense;
- deployment guard and CI checks require readable wheel staging;
- all five v0.3.1 application/dependency wheels are reused byte-for-byte.

The Moon Tax Python package remains version 0.3.1. No application logic,
database migration, or accounting behavior changed in this patch.
