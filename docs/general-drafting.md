# General drafting and controller assessment

## Included

The existing controller uses a general reasoning loop: understand the request,
gather suitable material, create or revise, then assess fulfillment. It can repeat
or omit actions as the task requires. This is guidance for the existing agent loop,
not a fixed orchestration pipeline, new writing mode or additional classifier.

The original assignment and actual clarifications remain authoritative. The
controller is instructed to inspect actual working content with `read_sources`
when needed before completion; a candidate ID/title or save-success message alone
does not establish fulfillment. It can choose more reading, revision, clarification,
calculation or `verify_draft`. Verification is optional and remains tied to its exact
checked candidate. No backend semantic approval gate is added. Existing version,
ownership, cancellation and publication guards remain application guarantees.

## Material purposes

`analyze_material`, `create_draft`, `revise_draft` and `verify_draft` retain
`material_ids` and add `material_uses`: records with `artifact_id` and a free-text
`purpose`. Model-facing calls include the list; older saved calls missing it are
normalized to an empty list without changing their saved arguments. Supplied
references must name selected materials and have nonblank purposes. Prefer one
combined entry per material. Repeated entries for the exact same reference are
accepted: distinct descriptions are joined with newlines in first-seen order,
and identical repeated descriptions appear once. Original wording is preserved;
no model call, summarization, priority inference or conflict resolution is added.
Different references are not merged. Invalid types, blank descriptions and
unselected references remain errors.

For example, one read result may contain Cedar client notes and excerpts from a
separate Answer. Two purpose entries, “Use Cedar notes for facts.” and “Use Answer
excerpts for style only.”, become one entry retaining both descriptions. This
normalization applies to analysis, creation, revision and verification before
material handoff. The recorded raw tool arguments remain unchanged; supplied
material envelopes and saved operation metadata contain the combined purposes.
No fixed role enum or automatic classification is introduced.

The server resolves selected IDs, adds `use_in_this_operation` to copies of their
material envelopes and saves resolved usage metadata with the operation artifact.
Purposes are per operation; they never modify the underlying source/artifact or
become permanent file roles. Moving draft references resolve to exact candidates
for the saved metadata. Omitted purposes remain unspecified. Analysis, writing and
verification receive guidance to use these purposes under the original request,
keep source contents as data and avoid copying unrelated facts from style examples.
Existing compaction notes preserve requirements, purposes and outstanding checks.

## Shared editing

The first revision of an uploaded document uses the accepted prepared source's
exact extracted text as canonical Markdown. Filenames, page labels and provenance
stay outside the editable body. Both uploaded-target `revision_mode` values
(`general_revision` and `preserve_original`) are compatibility names for the same
4B patch/rewrite engine. Workspace targets continue to use `general_revision`.
The old agent paragraph-selection/edit/reconstruction branch is no longer called;
legacy shared utilities remain available but are outside agent dispatch.

The resulting candidate retains the source/preparation references. There is no
extra import candidate or visible editor write before final publication. Importing
an unchanged uploaded body can still create the first result candidate because
that body is not yet the Workspace working draft. Subsequent unchanged revisions
remain no-ops. Original upload bytes/text are never edited, and all later revisions
continue from the 4A working candidate.

## Validation and boundaries

Tests cover material handoff to analysis/writing/verification, free-text purposes,
invalid references, legacy omissions, source immutability, per-operation reuse,
resolved draft identities and compaction/reload. General controller replays cover
style-plus-facts requests, executive briefs, source responses and plain-language
writing without dedicated task branches or mandatory verifier calls. Uploaded-first
checks cover both aliases, atomic rejection, cancellation, source identity and
follow-up editing. A real Redis/Celery replay reads the saved draft before finishing.

These mocked checks establish data flow and application behavior, not live writing
quality or latency. Native Word layout, fuzzy patching, automatic long-document
batching and specialized completeness validators remain deferred. Context limits
still apply without silent truncation or a fallback to the retired editing path.
No frontend changes or database migration are required. Existing accounts, stored
documents, candidates and confirmed version history are retained.

The initial general drafting update passed **625 offline backend tests** (**17 skipped**),
**9 real Redis/Celery workflows passed** (**1 skipped**), and **9 affected browser
workflows passed**. External providers were mocked throughout. The runtime replay
includes material-purpose handoff, shared uploaded-first editing and controller
inspection of the saved working draft before final publication. These results do
not establish live model quality or latency.

Duplicate-purpose follow-up: **118 affected offline checks passed**, including
all four material-using tools and the previously rejected Cedar call structure.
The exact recorded call now combines both purposes without changing its raw
arguments. A fresh natural-request browser run completed in **39 seconds**,
with **zero tool errors**, a **684-word** draft and a verified DOCX download.
That model supplied one combined purpose itself; duplicate normalization was
exercised by the recorded offline replay and operation-handoff checks.


## CourtListener court information

Citation lookup returns CourtListener case-identification sources directly, as well
as retaining the complete saved research record. Existing court values are kept;
an omitted court stays null. Available `headmatter` is preserved as readable
`case_header`, with `case_header_source` recording the response URL and field.
No court is inferred from that text by the backend and no additional docket/court
requests or model calls are introduced. The controller can use an explicit court
designation in the identified decision's header as evidence.

The header and other identifying information accompany opinion choices, passages,
and selected analysis/drafting/verification materials, separately from opinion
text. Body offsets, matching, pagination and download-cache identities do not
change. New snapshots can fill missing source information; existing values and
conflicting provider observations are retained, and historical artifacts remain
unchanged. Older artifacts without the optional header fields remain readable.

Controller and helper guidance requires evidence for court assertions and treats
uploaded parentheticals and prior generated statements as claims to check. Court
identity does not establish quotation accuracy or support for a legal proposition;
the controller still chooses subsequent source reads and substantive checks.


## Citation-review completion accounting

Current-request `lookup_authorities(mode="audit")` calls declare source-version and
range-specific review scopes. Lookup saves and returns stable `review_items` for
identified citation uses. Overlapping selections deduplicate the same original
occurrence; different uses remain separate. Provider references without confirmed
original locations remain unresolved discovery items. Discovery limitations stay
visible. Searches and overview lookups do not expand these scopes automatically.

`complete_task.citation_review_results` supplies one `review_item_id`, `status`
(`assessed` or `unresolved`), nonblank `finding` and `evidence_ids` list per item.
Assessed findings require saved original-source/passage or research evidence;
unresolved entries may have none. Reads alone never create an assessed outcome.
Missing/duplicate/out-of-scope items or unauthorized evidence return a normal tool
error before publication. The controller can investigate or explicitly disclose
unfinished work, then resubmit the complete outcome list. No tool order or extra
reviewer call is imposed.

Accepted outcomes are saved in completion/turn metadata and rendered as a coverage
table in the final chat response. The authored Output draft is left unchanged.
Accounting completeness does not certify legal correctness, exhaustive discovery,
the model's scope selection or the accuracy of a later rewrite. Ordinary drafting
and overview research keep their behavior; old calls missing the new field are
normalized to an empty list. Historical artifacts are never rewritten. Citation-review
accounting is independent of the source-navigation guidance below.


## Ordered alternative queries and source navigation

`read_authority` accepts `alternative_queries`, an ordered list; `[]` keeps the
single-phrase behavior. New strict tool calls supply this field; older saved calls
missing it default to `[]` without changing their recorded arguments. Blank entries
and nonempty alternatives with `query=null` are rejected.

The reader searches the original `query` first, then each alternative from the same
input `start`, stopping at the first successful phrase. It returns and saves one
original-source passage within the existing passage-size limit. Alternatives reuse
the existing formatting-tolerant matcher and downloaded source cache; they do not
introduce automatic query generation, another model call or semantic matching.

Results retain `query` and add `original_query_found` and `matched_query`. For an
alternative success, `match_found=true`, `original_query_found=false`, and
`matched_query` identifies the alternative. Finding an alternative locates related
discussion; it does not validate the original wording or legal proposition. If all
phrases miss, no passage is saved and match positions remain null. New fields are
null when no phrase search occurred, including plain reads, opinion selection,
unavailable sources and exhaustion. Historical artifacts remain readable unchanged.

`match_start` and exclusive `match_end` always identify the original-text span for
`matched_query`. Find another occurrence with that phrase as `query`,
`alternative_queries=[]` and `start=match_end`. Read the next passage with
`query=null`, `alternative_queries=[]` and `start=next_start`. Different phrases
may require restarting at zero. Matches and misses cover only the suffix searched.

Controller guidance suggests shorter terms, surrounding text and useful existing
sources after misses, before repeating broad research. The controller still chooses
its next action; no fixed research sequence or automatic citation assessment is added.
Offline replays establish retrieval and metadata behavior. Live model adoption and
latency measurement remain separate validation.

Validation: **360 affected offline backend checks passed**, including controller
handoff/reload, matching/navigation, source ownership, court metadata, citation
completion and the existing context-budget guards. The saved Soundview replay
reported the original phrase absent, matched `irreparable harm` at original-text
positions `[123753, 123769)`, returned one passage and fetched the body once. Frozen
benchmarks remained unchanged; live model behavior and latency were not evaluated.
