# B-UH Moon Tax 0.3.1

This release improves the Moon Tax navigation and Director workflow while
hardening its preserved accounting history.

Highlights:

- the full Athanor row opens that extraction rather than requiring a precise
  click on its name;
- the full account row opens a dedicated person workspace with linked
  characters, billing rules, exemptions, bills, payments, and reversible
  adjustments;
- Directors can waive, credit, debit, void, and reverse a bill from the
  overview, extraction, or person workspace with a permanent audit reason;
- every Moon Tax data table supports accessible one-click sorting, including
  correct date and numeric sorting for mining quantities, Jita prices, values,
  and taxes;
- row Accept and multi-select batch actions remain available while the
  confusing payment drag-and-drop interaction has been removed;
- per-account billing can be combined, separated by character, or returned to
  the site default without rewriting decided historical bills;
- account profiles include account-wide and character-specific exemption
  history with current, future, expired, and inactive states;
- approved or adjusted unlinked-character bills retain their identity if the
  character later links to Auth, preventing duplicate billing;
- outstanding totals are calculated per bill so overpayment on one extraction
  cannot hide another unpaid extraction; and
- payment decisions lock their evidence and target bills during allocation to
  prevent simultaneous Director actions from double-applying ISK.

`DEPLOYMENT.env` declares the checked release. `SHA256SUMS` covers that manifest,
the guarded updater, and all five wheels required by the platform deployment.
The production workflow rejects missing, additional, renamed, or modified
release files before connecting to the VPS.
