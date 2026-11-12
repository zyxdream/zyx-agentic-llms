# Standalone web search — local live cases

Use the normal local app, real controller LLM and configured search provider.
Use synthetic chats only. Do not tell the controller tool names or IDs, and do
not replace its decisions with mocks. Keep detailed prompts, provider responses
and screenshots under ignored local artifacts; check in a brief results report.

## 1. General update without documents

Start a fresh chat with no attachments:

```text
What are the latest announced Mozilla Firefox updates? Give a brief dated summary with source links, distinguish release dates from when you checked, and keep the editor unchanged.
```

Expected: standalone web search with no uploaded-file or legal-research
prerequisite. Inspect the saved search summary, observed source URLs and
retrieval time. No CourtListener request or authority-research record is needed.
If current coverage is limited, the answer should disclose that limitation.

## 2. Lawsuit developments without a citation review

In an attachment-free chat:

```text
Give me a brief update on recent developments in the DOJ search-antitrust lawsuit United States v. Google, D.D.C. No. 1:20-cv-03010. Include dates and source links. I want a news/status update, not a citation audit. Keep the editor unchanged.
```

Expected: standalone search can handle this legal subject without creating a
citation-review pipeline. Check the actual tool choices and whether source
publication/event dates remain distinct from the search date. Do not require a
fixed number of searches or particular current news facts.

## 3. Citation audit of a synthetic document

Use a fresh chat with only
`../fixtures/authority-discovery-modes/review-brief.txt` attached and ready:

```text
Review every legal-reference occurrence throughout review-brief.txt only. Keep repeated uses, short forms, the misspelled caption and the suspect page number separate. Show the exact wording, source location and what each appearance is used to support. Do not silently correct the document or change the editor.
```

Expected: the existing legal-research tools, with audit discovery over the
selected brief. Compare the seven separate appearances against the independent
`expected-occurrences.json` fixture; never upload that oracle. Citation identity
does not establish support for a proposition. Standalone news search is not a
substitute for this document review.

## 4. Mixed news and document review

Use another fresh brief-only chat so earlier audit checkpoints do not obscure
the observed route:

```text
Give me a short dated update on recent developments in the DOJ search-antitrust lawsuit United States v. Google, D.D.C. No. 1:20-cv-03010. Separately review every legal-reference occurrence in review-brief.txt, preserving its wording and separate uses. Link the news sources, distinguish the news update from the citation-review findings, and keep the editor unchanged.
```

Expected: both standalone search and legal-reference review as independent
parts of the assignment. News search does not certify the brief's propositions;
audit findings describe the selected original document. Inspect actual calls,
not a prescribed order.

## Evidence and completion checks

For each request record the chat/workflow ID, model, current-date context,
brief actual LLM inputs/outputs, tool arguments, returned sources and the next
controller decision. Keep generated search synthesis separate from source text
actually read. Verify final links come from observed provider source metadata.

Open the saved search result through **Steps and saved results**, then reload
and confirm it remains available. For document cases, compare original-file
hashes and retained source spans. Confirm every editor remains unchanged.
Record provider errors, empty results, incorrect routing or extra calls without
silently replacing a failed case. These live cases check observed model
behavior; focused automated tests separately cover failure and cancellation.
