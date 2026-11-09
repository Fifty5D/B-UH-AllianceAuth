# B-UH Member Audit auto-registration

The application source was recovered from the installed, MIT-licensed
`aa_buh_memberaudit_autoreg-0.1.0-py3-none-any.whl` (SHA-256
`e26b225324996c679dddae29d45abf1afce573494fa0f293e755f2072631f0ce`).
This successor keeps the application code and widens its AllianceAuth dependency
only after the AllianceAuth 5.4 compatibility tests pass.

An already registered character now schedules targeted recovery after a new
full-scope authorization. The worker retries only sections with token-error flags;
it does not clear flags until Member Audit's supported task records success.
Recovery preserves current ownership, disabled characters, and audit history.

`auth buh_memberaudit_recover` previews existing failures without refreshing tokens.
Applying requires `--apply --snapshot <new-private-json-path>`, with optional
`--expect-characters` and `--expect-sections` checks. Host-based recovery and bounded
periodic prevention are documented in the operational release's HOST-RECOVERY.md.
