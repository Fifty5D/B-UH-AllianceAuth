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

## Historical resolution and current qualification

A disposition resolves named findings in its recorded source; it does not qualify
that source against a different combined tree. New collections emit a schema-v3
readiness record with `source_qualification`, binding the repository, PR, current
head and target base, combined Git tree, tested source/tree, executing workflow
commit/blobs, source and applicable preview job IDs/run attempts/checkout-log
hashes, and review snapshot digest. The existing schema-v2 bot pointer still
authenticates the entire record through its artifact digest.

The bounded reuse rule is **exact Git-tree equality**. The collector reads
GitHub's current test merge, verifies its two parents are the current base and
head, and requires its tree to equal the tested head's tree. It never decides
equivalence from ancestry, filenames, an "activation-only" description, timestamps
or passing tests on a different tree. Main can activate policy while the source
head stays unchanged only when this check establishes that the combined tree is
already exactly that reviewed/tested source. For example, main can independently
incorporate contents already present in the accepted head. Historical PASS and
its original reviewed base remain unchanged.

If activation adds contents absent from that head, reuse is **blocked**, even if
the changes seem harmless or affect only policy/docs. In particular, PR91's old
PASS is not automatically a review of the newly activated policy's combined tree.
Incorporate the current target into source, obtain applicable source review,
ordinary current-head CI and any required preview, then use a new authenticated
disposition for that head. There is no PR-specific exception or system for proving
arbitrary behavioral equivalence. Missing/conflicting test merges, unavailable
Git metadata, unsupported workflow contracts or missing checkout logs also block.

The workflow's displayed run head is not checkout proof. The collector verifies
the immutable reusable-workflow execution commit reported by GitHub and the
supported source/preview workflow Git blobs from its trusted checkout. It requires
the complete successful CI job profile, successful checkout steps and bounded job
logs containing the actual checkout and `git log -1 --format=%H` identity. Required
CI and applicable preview jobs must have checked out the tested source. The
supported docs-only profile may skip all five runtime lanes together; its
configuration/release/required jobs still run. Changed or unavailable workflow
contracts require fresh supported qualification, not inferred compatibility.

Every new collection, including repeated collection at the same changed base,
performs these checks. It rereads head/base/test-merge identity before returning.
The unchanged publication workflow compares complete records before upload and
before its final signal; the new fields participate in those comparisons. A label
is still asynchronous metadata, not independent qualification. Later published
verification rechecks the real merge's first parent/tree, actual CI/preview jobs
and log hashes, workflow contract and live review evidence against the artifact.
A different base at merge time cannot reuse the prior record. Later main movement
does not rebind an already verified historical merge to a different target.

Retained schema-v2 records without dispositions remain readable for existing
historical release consumers. They cannot carry this new route or its qualification
fields; all current collection emits v3. Removing qualification from v3 or
downgrading a disposition-bearing record to v2 is rejected. Metadata-only static
validation checks record bindings; live collector/published verification supplies
the GitHub provenance. Retained records do not make missing live evidence fresh.

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
   Once all findings have supported dispositions and the current combination has
   applicable current, unexpired source/review/preview qualification, request
   `ready-for-work` once. Verify the
   trusted workflow publishes its new digest-bound artifact and pointer, clears
   `needs-codex`, and emits readiness last. Do not request readiness repeatedly
   while policy activation or finding disposition is outstanding.

Final source risk review and any future merge remain separate from this
administrative correction. No source readiness record establishes installed
correction, recovery completion, historical closure or production health, or
authorizes release publication, receiver access, installation or deployment.
