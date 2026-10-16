# Explicit resolution of COMMENTED review findings

This source-readiness route becomes available only after the independently
reviewed policy is merged into protected `main`. An unmerged policy PR does not
activate it. The existing readiness workflow continues to execute only its
trusted base/default-branch checkout with write credentials; PR source tests and
previews retain read-only credentials. No workflow permission or trigger changes
are required.

GitHub rejected dismissal of a COMMENTED review with HTTP 422, "Can not dismiss
a commented pull request review." Do not retry that transition, edit severity
markers out of historical reviews, delete reviews, or seek artificial approval.
A newer informal PASS does not clear earlier findings. The policy retains every
outstanding serious review, including multiple reviews by the same author.

## Authority and evidence

An authenticated human reviewer with current GitHub repository `write`
permission may disposition only their own COMMENTED reviews. An authenticated
human with current `admin` permission may act as process owner for named reviews
by other authors. The policy reads the repository's collaborator-permission API
and verifies its stable user ID against the comment author. GitHub maps maintain
to write and triage to read in [that API](https://docs.github.com/en/rest/collaborators/collaborators#get-repository-permissions-for-a-user).
Text claiming ownership, author
association alone, login similarity, and bot accounts grant no authority.

The same authenticated user must have authored the referenced source PASS review.
That review must be COMMENTED or APPROVED, have no blocking severity marker,
belong to the exact current head, and contain exactly one identity declaration:

```text
source review: **PASS** at `<40-character head SHA>`, base `<40-character reviewed base SHA>`
```

An existing prefix such as `Independent Work ` is supported. The reviewed base
is verified through GitHub comparisons as an ancestor of both the head and the
current target base. It need not equal the newer main commit that activates this
policy. This preserves an accepted source review's original identity; it does
not attest that a later main change is safe to merge.

Post a **new, unedited PR conversation comment** beginning with the following
marker, a newline, and only the JSON payload. Replace every placeholder with
verified metadata before posting:

```text
<!-- buh-commented-review-disposition:v1 -->
{
  "schema_version": 1,
  "repository": "OWNER/REPOSITORY",
  "pr": 42,
  "head_sha": "REVIEWED_HEAD_40_HEX",
  "base_sha": "REVIEWED_BASE_40_HEX",
  "pass_review": {
    "id": 1235,
    "body_sha256": "sha256:EXACT_PASS_BODY_UTF8_SHA256"
  },
  "resolved_reviews": [
    {
      "id": 1234,
      "body_sha256": "sha256:EXACT_FINDING_REVIEW_BODY_UTF8_SHA256"
    }
  ]
}
```

Calculate each hash over the **entire unmodified UTF-8 review body**, without
newline normalization: `hashlib.sha256(review["body"].encode("utf-8")).hexdigest()`.
List 1–100 unique review IDs in increasing order. Each target must be an earlier
serious COMMENTED review, preceding the supporting PASS in ID and submission
time. The disposition must follow the PASS. Naming a review explicitly attests
that **all its findings** were verified resolved by that supporting review.
There is no partial resolution within a review body; a remaining finding means
the whole review stays blocking. Do not name reviews the PASS did not verify.

The collector retains original review IDs, authors, states, times, commits and
severity flags. The metadata-only readiness snapshot additionally binds review
body hashes, disposition ID/body hash/times, authenticated user IDs, live
authority, GraphQL unedited provenance and source ancestry. Historical reviews,
receipts and artifacts are not rewritten. Missing reviews, changed hashes, an edited disposition, revoked
authority or missing PASS evidence cannot qualify. Edits are refused because
the issue-comment API does not authenticate the editor as its original author.
Equal REST timestamps alone are insufficient: `lastEditedAt` must explicitly be
null, and GraphQL author, identity, body and times must match the REST comment.
An older source disposition remains historical evidence and cannot resolve
findings at a new head; use a new, independently verified disposition there.

Only explicitly named reviews receive resolution. Unnamed reviews, later
rejections, native CHANGES_REQUESTED states, unresolved serious threads and
ordinary serious conversation comments remain blocking. Native approvals and
dismissals retain their existing behavior. This accounting is not a formal
GitHub approval: required-review protection, source tests, previews, release
lineage and production gates remain independently required.

## Activation and later readiness

1. Independently review the separate policy PR at its exact final head, with all
   ordinary source checks, applicable preview and the existing trusted readiness
   handoff. Never run its unmerged policy with production or write credentials
   to qualify itself.
2. Before any future merge, resolve the separate **Prepare Release/host-hold
   mismatch** through the existing trusted process and applicable authorization.
   Prepare Release is active, the repository lacks an active recovery-hold file,
   and the retained record identifies host hold `gh-36955595351-1`. A repository
   `--require-clear` success does not clear that host hold. The documented
   workflow-control pause and verification of no queued/running preparation or
   preflight must precede an authorized source-only merge, unless a separately
   reviewed persistent source hold is established. Keep the pause in force while
   the incident hold remains active. This PR does not change workflow settings,
   retire a hold or authorize merging.
3. After separately authorized policy merge, verify the protected default/base
   branch contains the reviewed correction and the existing write-capable
   workflow still pins that trusted checkout. Only then can it consume a valid
   owner/reviewer disposition for another PR. Recheck that PR's accepted source,
   exact supporting review/body hashes and still-current evidence; preserve its
   original reviewed base and all historical records.
4. Let the disposition's normal comment event invalidate/evaluate readiness.
   Once all findings have supported dispositions and exact-head source/preview
   evidence is current and unexpired, request `ready-for-work` once. Verify the
   trusted workflow publishes its new digest-bound artifact and pointer, clears
   `needs-codex`, and emits readiness last. Do not request readiness repeatedly
   while policy activation or finding disposition is outstanding.

Final source risk review and any future merge remain separate from this
administrative correction. No source readiness record establishes installed
correction, recovery completion, historical closure or production health, or
authorizes release publication, receiver access, installation or deployment.
