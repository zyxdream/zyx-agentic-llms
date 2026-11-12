# Standalone web search — local live results, November 10, 2026

Ran on `Monica-agent` after `ab742665`, with the standalone-search changes in
the working tree. Used the normal local app, authenticated browser UI, Conda
`lexintel`, real `gpt-6.1-sol` controller/search calls and configured providers.
No model decisions or provider responses were mocked in these four scenarios.

## Actual routing

All chats are in the synthetic matter **Authority discovery modes — local test
2026-11-10**. The user prompts are in `standalone-web-search-live-case.md`.

| Request | Chat / workflow | Observed tools and outcome |
| --- | --- | --- |
| Latest Firefox updates, no attachments | `262` / `wf_dd949541-a515-4703-bbe0-a62145986748` | Two `web_search` calls, then completion. No authority research or legal tools. |
| Recent developments in the DOJ Google search-antitrust case, no attachments | `263` / `wf_b0d7c277-dae1-41a9-97e3-856cc6b6b562` | Two `web_search` calls, then completion. The legal topic alone did not trigger citation discovery. |
| Every reference in the synthetic brief | `264` / `wf_e3cb470e-f75a-4738-a906-274b3e72689d` | Read the original, `lookup_authorities(mode="audit")`, read saved research, then completion. Seven separate appearances; no standalone search. |
| Case developments plus brief citation review | `265` / `wf_dea03cdc-c6a4-41d5-b3da-6a858f6421e3` | Original read, standalone `web_search`, audit lookup, saved research read, authority reading, research-linked web search, official-source reading, then completion. |

## Example data flow

The first general-update controller input had no documents or selected legal
research, and included `current_date: "2026-11-10"` plus the tool catalogue.

1. Controller selected `web_search` with the query
   `Mozilla Firefox latest release notes November 10 2026 Firefox updates official release`.
2. The shared adapter called the existing web-enabled model with general
   instructions and the current UTC date. It returned generated synthesis and
   39 observed source URLs. The tool saved both and returned an artifact ID.
3. The next controller call received that summary, source metadata, retrieval
   timestamp and the explicit distinction from full-page text. It chose a
   narrower follow-up search, which returned 21 observed source URLs.
4. The controller completed with dated updates, links from the observed source
   records, and a statement that this was search synthesis rather than a
   full-page review. It distinguished release dates from the check time.

The lawsuit-news searches returned 80 and 68 observed URLs; the mixed request's
standalone search returned 95. These are provider source lists, not counts of
pages independently read by the application. Snippets were often empty and
titles sometimes fell back to URLs. No additional source-count limit was added.

The mixed request also exercised the same adapter through legal research,
retaining the legal prompt and storing those findings in its research record.
Its final answer separated news from the seven citation-review appearances and
identified limitations on substantive support for the fictional assertions.

## Validation

- All four requests completed. Actual initial inputs contained the UTC date.
- Both attachment-free requests had empty accepted-document catalogues and
  created only standalone search artifacts, with no CourtListener tool path.
  CourtListener-hosted URLs in news results were observed web sources.
- All standalone artifacts label summaries and snippets as generated search
  material, retain observed sources, and include retrieval timestamps.
  Independent review matched the news-only saved URLs to actual provider
  metadata and every final external link to those saved sources.
- Both audits retained exactly the seven expected appearances. Their exact
  wording and character spans match the independent fixture inventory, including
  repeated uses, short forms, the misspelling and the suspect page number.
- Both uploaded originals retain SHA-256
  `65b36fbbb57573a99c8a052b769a79d1cfbba4993808b85622fa744d3700519b`.
  All four editor states equal their initial pinned snapshots.
- Reopening the general-update chat restored its answer. The existing saved-result
  viewer opened the persisted search synthesis, source URLs and retrieval time.
- Focused backend tests: **212 passed** in Conda `lexintel`. Saved-results UI
  tests: **24 passed**. Provider errors, empty results and cancellation were
  covered by automated tests; no such failures were injected into live calls.

## Local test observations and evidence

- The frontend test command regenerated the ignored brand config for the hosted
  test environment. Synthetic chat creation failed with network errors before
  local testing continued. Restored it with
  `REACT_APP_BRAND=local node scripts/generate-brand-config.js`; the live runs
  used `http://127.0.0.1:5000/api/`. This required no application-code change.
- Navigating to a fresh task while another task ran briefly retained the old
  Stop state. Reloading the fresh task cleared that client state without
  cancelling the earlier workflow. No unrelated UI fix was added.
- Extension file uploads required an unavailable file-URL permission. Used the
  native picker for the synthetic brief without changing browser permissions.
- Ignored evidence directory:
  `artifacts/live-case-assistant/standalone-web-search-20261110/` contains
  sanitized actual LLM inputs/outputs, initial controller inputs, steps,
  artifacts, integrity validation and screenshots. Private reasoning and
  credentials are excluded from the trace.
- Temporary tracing was disabled after the tests, and the normal local agent
  worker was restored. No database migration or new provider was needed.

These scenarios validate tool routing, persistence and source provenance. They
do not independently fact-check every generated news statement or add general
full-page reading.
