# B-UH Moon Tax 0.2.2

This hotfix corrects the 100x undervaluation of moon-mining ledger quantities.

EVE's current compressed ore keeps the same item count as uncompressed ore; its
documented 100:1 ratio describes volume. Earlier Moon Tax releases divided the
ledger quantity by 100 before applying the compressed Jita buy price.

The migration repairs only untouched mappings that were auto-discovered by
Moon Tax 0.1.0 through 0.2.1. Director-customized compression mappings are left
unchanged. The guarded platform updater runs migrations and queues a fresh audit,
so open extraction periods are repriced immediately.

Wheel SHA-256:

`6a7e65b7f8ec471ba40b020be6244e92c6c2727e75f92ccd78bdf03d10c31071`
