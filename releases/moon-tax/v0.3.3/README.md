# B-UH Moon Tax 0.3.3

This release updates the Moon Tax application to version 0.3.2 and adds a
combined billing-account summary to the Overview page.

Changes in this release:

- totals every visible extraction bill by its preserved billing identity;
- shows extraction count, mined value, tax, paid, and outstanding totals;
- keeps separate-character billing rows separate from combined Auth accounts;
- calculates outstanding as the sum of each bill's positive balance, so an
  overpayment on one extraction cannot hide debt on another;
- links each summary row to the existing account or unlinked-character
  workspace with keyboard-accessible navigation;
- applies the same permission-filtered scope as the existing ledger;
- introduces no database migration and does not recalculate historical bills.

The four unchanged dependency wheels are reused byte-for-byte from the prior
checked release. The guarded installer retains checksum verification, readable
wheel staging, pre-swap checks, and rollback protection.
