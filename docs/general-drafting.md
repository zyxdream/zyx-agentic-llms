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
