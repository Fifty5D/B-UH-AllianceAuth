# B-UH Moon Tax 0.3.0

This release replaces the payment-review workflow and adds finer-grained,
auditable director controls while keeping Moon Tax inside Alliance Auth.

Highlights:

- row-level Accept uses a dedicated POST endpoint with a native form fallback;
- drag-and-drop and click actions share the same accounting service;
- payment search, multi-select, and guarded batch actions remain available;
- directors can preview the exact read-only view and scope of an Auth member;
- each Athanor has editable tax and payment-recipient policy;
- bills support reversible waivers, credits, debits, and voids with reasons;
- small residual balances can be waived without deleting accounting history;
- customized group names and permission matrices survive updates;
- exemption history is fully rescanned only when its fingerprint changes; and
- mining-ledger and contract lookups are batched to reduce audit query load.

`DEPLOYMENT.env` declares the checked release. `SHA256SUMS` covers that manifest,
the guarded updater, and all five wheels required by the platform deployment.
The production workflow will reject missing, additional, renamed, or modified
release files before connecting to the VPS.
