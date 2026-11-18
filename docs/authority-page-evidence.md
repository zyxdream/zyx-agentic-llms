# Published-page evidence for quotations

New CourtListener source caches preserve explicit `span.star-pagination` boundaries.
When the already-returned HTML and case citations identify one publication, those
boundaries can be returned immediately as page evidence. The
reader extracts text and boundary positions together. It does not align HTML
positions to a different plain-text representation. A Lawbox publication heading
can identify its citation among parallel citations; ambiguous streams, conflicting
labels, missing page sequences and unsupported formats remain unverified.

`read_authority` returns one excerpt with `page_evidence`: original page labels,
text ranges, boundary text and positions, next boundary or scope end, publication
citation, originating field, URL, opinion ID and source digest. Boundary evidence
may lie outside the excerpt; it does not imply that intervening text was read.
`matched_published_pages` maps only the matched original-text range, including an
alternative match, or is null when unsupported or when no phrase matched. Plain
reading can return page evidence without claiming a quotation was located.

The controller receives the evidence directly for general responses. It is also
saved with the passage; selecting that passage supplies identical evidence to
analysis, writing, revision and verification after reload. A short phrase match
never verifies a longer quotation or its legal application. Without sufficient
page evidence, report "not verified from the retrieved sources"; supported content
findings remain usable. Physical PDF pages and arbitrary asterisks are not reporter
pagination. Footnotes do not inherit main-text pagination.

The existing excerpt limit (12,000 characters) and leading context (1,000) control
reading volume, not page verification. Match offsets, continuation, alternative
queries and cache reuse are unchanged. Old caches and historical artifacts are
not rewritten; missing retained pagination remains unverified. No additional
model call, migration or automatic substantive review is added. Optional publication
metadata resolution is described below.

Validation uses a saved public Calpine Lawbox response plus mocked providers. The
fixture was retrieved from CourtListener opinions/1833489 on 2026-11-17; its digest
is checked by the tests. Tests prove evidence delivery, not live model compliance.


## Quotation, context and pinpoint findings

One citation use keeps one review item and the existing `finding`/`evidence_ids`
fields. Describe quotation accuracy, support in context and the published page
separately within that finding. For a paraphrase, assess attributed meaning rather
than quotation wording. Prioritize material wording/context checks over page-only
research unless the user makes pinpoint verification central. The controller
chooses its next actions; there is no required tool sequence or extra reviewer call.

A supported content finding may be `assessed` even when its pinpoint is unverified.
Say whether a page check was not performed or was attempted without sufficient
evidence. This does not relax the requirement that a citation whose occurrence in
the uploaded document is unlocated remains `unresolved`. A match, download or page
association never automatically creates a substantive verdict. Alternative matches
still do not verify the original wording; a short match's page does not locate an
entire longer quotation.

## Optional publication metadata

`read_authority.resolve_page_metadata` is explicit in new model-facing calls;
older calls default to `false` without changing the recorded arguments. False
prevents extra page-only requests, not the return of freely available page evidence.
An explicit true may make one logical CourtListener cluster metadata request for a
returned passage whose retained CAP markers need publication association. All-phrase
misses, exhaustion, mapped sources, web/PDF sources and historical caches without
retained boundaries do not cause that extra request or a body refetch.

The narrow CAP extension supports one stream marked `citation-index="1"`. It checks
the expected cluster and selected opinion's membership, then associates the archive
locator with exactly one distinct structured citation using CAP-compatible reporter
slug normalization, exact volume and first page. For example:
`law.free.cap.f3d.450/1132.3089622.json` identifies `450 F.3d 1132`.
It never uses citation-array order, citation type alone or page-number proximity.
Unsupported/conflicting metadata and ambiguous publications remain unverified.
Provider-marked HTML is retained even before association; it supplies both text and
offsets. Footnote isolation and consecutive-boundary checks still apply.

`publication_source` records the archive locator, pagination identifier, cluster
endpoint and metadata digest separately from the opinion-text source/digest.
`digest_scope` distinguishes a full metadata response from retained fields of an
already-available lookup observation.
Enrichment saves a new immutable cache with identical text, blocks and offsets.
The new research snapshot replaces the matching cache pointer and all its aliases;
historical snapshots/cache artifacts remain unchanged. Resolved and definitively
unsupported metadata are reused. A temporary failure leaves content readable, and
only a later explicit true may retry it. Older plain-text caches are not redownloaded.

The saved World Wide HTML and reduced cluster metadata fixture record their
provenance and digests in `tests/fixtures/authority-pinpoints/world-wide-provenance.json`.
They exercise association independently of citation ordering. Provider documentation:
[CAP reporter slug](https://github.com/harvard-lil/capstone/blob/develop/capstone/capdb/models.py),
[CAP pagination stream](https://github.com/harvard-lil/capstone/blob/develop/capstone/scripts/render_case.py),
and [CourtListener archive-path construction](https://github.com/freelawproject/courtlistener/blob/main/cl/search/management/commands/generate_cap_crosswalk.py).

Offline checks cover delivery, completion safeguards, association, cache replacement,
reload, failures/retries, legacy calls and prompt budgets. They do not establish live
model judgment or latency. Live validation is deferred to the later combined test round.

Validation for this update: **456 affected offline backend checks passed**, including
existing small-context compaction guards, citation-review completion safeguards,
controller/material handoff, source navigation and working-draft regressions.
Providers were mocked. No live tests were run for this update.
