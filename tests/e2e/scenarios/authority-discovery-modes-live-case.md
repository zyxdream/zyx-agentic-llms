# Authority discovery modes — synthetic local live cases

Use the normal local app, workers, real LLM and configured source providers.
“Mock live” means fictional documents; do not replace controller decisions or
discovery responses with mocks. Label any separate mocked failure checks as
such. Do not tell the controller tool names, mode arguments, IDs or offsets.

Fixtures are in `../fixtures/authority-discovery-modes/`. Upload only the `.txt`
files named below. Keep `expected-occurrences.json` out of the chat: it is the
independent inspection reference, with original hashes, exact spans and the
long document's expected extraction-window boundaries.

## 1. General research overview

Create a fresh chat and upload `review-brief.txt`, `reply-note.txt` and
`invoice-control.txt`. Wait until all attachments are ready, then submit:

```text
Give me a concise working overview of the two legal documents, review-brief.txt and reply-note.txt. Identify the legal references involved, keep the suspicious wording or conflicting citations visible, and tell me what questions matter before we prepare a reply. Representative examples are enough; I do not want an occurrence-by-occurrence citation audit. The invoice is background only. Keep the draft editor unchanged.
```

Inspect whether the agent chooses overview discovery when legal lookup is
useful and selects the relevant files/ranges. The invoice must not silently
become part of the legal discovery scope. Conflicting captions and starting-page
variants must survive; database absence alone must not become fabrication.
Do not require a specific number or ordering of searches.

## 2. Detailed review of one selected document

Continue in the same chat:

```text
Now review every legal-reference occurrence throughout review-brief.txt only. Keep repeated uses separate, including the two identical full citations in the same paragraph, the later short form and name-only reference, the misspelled caption, the suspect page number and the statute. For each appearance, show the exact drafted wording, its source location and what that particular appearance is being used to support. Preserve previous research, but do not extend this review to the reply note or invoice. Do not change the draft editor.
```

Expected: audit discovery over the full selected brief. Existing overview
checkpoints do not stand in for this scan. Compare every retained source span
with prepared original text, and compare the independent inventory. The two
same-paragraph full citations carry different assertions; `Id.` and the later
name-only reference retain their own positions. Check saved mode/scope labels,
reload, and confirm earlier overview snapshots remain unchanged.

## 3. Boundary-spanning reference and narrowed continuation

Create another fresh chat and upload only `boundary-review.txt`, then submit:

```text
Review every legal-reference occurrence throughout boundary-review.txt. This is a source-fidelity exercise: preserve wrapped reference text, keep each use and its attributed assertion separate, and report locations against this uploaded document. Do not silently correct the draft's claims. Keep the editor unchanged.
```

The wrapped Brown reference begins before an actual structural extraction-core
boundary and ends after it. It is visible in overlapping context but must be
saved once, under its starting core. The independent manifest records expected
cores and the exact original locations. Inspect per-window research artifacts,
not just the final answer.

Then narrow the same chat's review:

```text
For the next review, examine only the text between FOCUSED REVIEW START and FOCUSED REVIEW END in boundary-review.txt. Check each reference appearance in that passage, and report the exact selected scope. Retain the earlier findings as history; the opening outside-control citation is outside this new review. Keep the editor unchanged.
```

Expected: an explicit original range, with no silent whole-file expansion. Old
findings may remain visible as history; current coverage and new occurrence
labels must describe only the selected range. Distinguish newly processed
scope from all historically saved research targets.

## 4. Nonlegal control

Create a fresh chat and upload only `invoice-control.txt`, then submit:

```text
Summarize these fictional invoices in a short table with billed, paid and outstanding amounts, plus totals. Flag any arithmetic inconsistency. Keep the draft editor unchanged.
```

Expected totals: billed `$850`, paid `$390`, outstanding `$460`. Observe whether
the agent finishes without mandatory legal lookup, source search or opinion
reading. These are observations, not an application ban on particular tools.

## Evidence and agent data flow

Before collecting final evidence, exercise source reading in the original
overview/audit chat with this additional focused request:

```text
Now perform a focused source-backed check: read a relevant passage from the Brown opinion for the brief’s equal-access reference, and the official text of 42 U.S.C. § 1983 for its proposed-remedy reference. Explain only what the sources establish and what cannot be assessed from these fictional assertions. Link the actual sources and save the evidence. Select relevant opinion text; do not read every opinion. Keep the draft editor unchanged.
```

Inspect actual source passages, selected opinion identity, official statute URL,
and whether those passages reach the next controller call before its substantive
answer. Record any search/reading failure and correction. This checks reading
separately from the occurrence inventory; a successful identity lookup alone
does not satisfy this request.

For each request record the chat/workflow, accepted files, selected source
scope, model, timing and final status. Keep a compact chronological trace:

| Step | Brief LLM input | LLM output | Executed operation / next observation |
| --- | --- | --- | --- |
| Controller decision | User request, prepared catalogue, editor summary, tools, saved research metadata | Tool name and essential arguments | Saved step begins |
| Discovery inside lookup | Exact bounded text, owned range, context, source units, mode and compact provider findings | Structured references with unit/index anchors | Exact spans validated; coverage and findings saved |
| Next controller decision | Original goal plus completed function-call output / saved research ID | Next research/read/analysis call or final response | Continue until completion |

Capture brief actual values, not only the intended architecture. Record failures,
partial coverage, unnecessary calls, selected excerpts and corrections. Save
raw provider bodies, downloads, screenshots and detailed traces only under
ignored local artifacts. Final checked-in results should contain summaries and
artifact/workflow references, not credentials or full prompts containing private
documents. Recheck uploaded-original hashes and confirm no editor publication.
