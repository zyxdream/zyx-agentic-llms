# Legal authority research in the agent

The Case Assistant agent chooses legal lookup, search and source reading as
separate operations. Publication does not perform a hidden citation lookup.
Standalone Hallucination Guard and older workflows retain their existing paths.

## Tools

- `lookup_authorities(targets, mode, purpose, research_id)` loads accepted files,
  exact original ranges, a draft revision/current draft, or explicit research
  text. A new request uses `research_id: null`. Whole-file research scans all
  readable canonical text; catalogue previews are not the dataset. `mode` is
  required for new model calls. Use `overview` normally for distinct reference
  and caption variants with representative examples, or `audit` for every
  occurrence, including repeated references and contextual short forms. Both
  process exactly the selected files/ranges; neither performs internal relevance
  filtering. `targets: []` continues the last selected scope of `research_id`,
  rather than every historically pinned target; older records without a saved
  scope reuse their pinned targets. Older saved calls without a mode explicitly
  default to overview.
- `search_legal_sources(research_id, authority_ids, query, provider)` extends a
  saved research snapshot using CourtListener or the existing web provider.
  An empty authority list searches the broader question. Results are source
  candidates, not retrieved opinion passages.
- `read_authority(research_id, source_id, opinion_id, query, start)` reads a
  selected source. Use `opinion_id: null` for webpages/PDFs or to obtain opinion
  choices, `query: null` for ordinary paginated reading, and `start: 0` initially.
  Returned passage artifact IDs can be supplied as drafting `material_ids`.

Each operation returns a new immutable research snapshot. Continue using its
`research_id`. Stable authority IDs preserve occurrences, attributed propositions,
provider candidates, conflicts, source reads and generated assessments. Ordinary
`read_sources` can inspect saved artifacts; `analyze_material` can add a generated
assessment without replacing source facts. Research can be explicitly reused
within the same chat after ownership and original source-version checks.

## Evidence and limits

CourtListener offsets refer to provider input only. Original references use
application-owned file/preparation or draft identities and exact original
context. A candidate or absent database result is not a universal validity
finding. Reading a source does not by itself certify a quotation, proposition,
historical version or subsequent treatment.

Discovery uses non-overlapping extraction windows built from original structural
spans, with a 20,000-character extraction ceiling and up to 1,500 characters of
reading context on each side. Original reference spans determine ownership;
context-only references belong to their own extraction window. Overview counts
describe recorded examples; audit asks for every occurrence in the selected scope.
Completion means the selected scope was processed without outstanding extraction
failures, not proof that every reference was recognized or legally verified.
Gaps in a subsequently read external opinion or PDF remain source-reading
limitations; they do not make discovery of the selected original text incomplete.
Mode and chunk-plan version participate in checkpoints, so overview work cannot
satisfy an audit and older chunk checkpoints cannot skip the new windows. Older
artifacts remain readable without being relabeled as audited.

Mode changes discovery detail only. Web research, opinion reading, quotation and
proposition checks remain separate operations. `verify_draft(check_citations=True)`
uses overview for its existing identity/source lookup; detailed discovery is an
explicit audit-mode lookup. The new research code does not call Hallucination
Guard or reuse its helpers.

Search observations initially show five sources; complete observed results stay
saved. Source reads return up to 12,000 characters with continuation. The web
reader supports HTML and native PDF text with page references, uses public
HTTP(S) URLs, and bounds redirects, time and download size. Inaccessible pages,
image-only PDFs, extraction gaps, failed ranges and provider limits remain
visible as coverage limitations.

For a newly authored holding/reasoning claim, provide the writer with actual
retrieved passages and their opinion/source identities. Case metadata, generated
summaries and search previews do not substitute for those passages. Relevant
opinion selection remains an agent decision; the tool does not fetch all
opinions by default.

## Links and configuration

`PUBLIC_APP_URL` is the public frontend origin used for absolute CaseViewer
links, including links in Word/PDF exports. Its local default is
`http://localhost:3000`. Configure the actual frontend origin when deploying.
CourtListener cluster IDs come from provider results; other authorities retain
their observed external URLs. Preview, publication and export use saved links
without new research calls. The legacy `validated_cases` viewer cache is not
the authority-research record.

Word exports define and apply the standard Hyperlink character style so the
existing Docker Writer runtime retains clickable PDF links. Export cache entries
include a renderer version; older files stay saved and a new download uses the
current renderer. No Docker image upgrade is needed for this change.

## Checks

Activate Conda `lexintel` before backend checks. Focused tests cover retrieval,
source anchoring, immutable continuation, ownership, saved passages, controller
publication and research UI. The local live scenario is documented in
`tests/e2e/scenarios/authority-research-live-case.md`.
