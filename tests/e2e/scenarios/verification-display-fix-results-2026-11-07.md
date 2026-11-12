# Verification prose and unnamed editor sections — November 7, 2026

Both focused fixes passed regression tests and a fresh live case 7 repeat.
The [original run](agent-foundation-live-results-2026-11-06.md) remains the record
of the earlier defects; historical report artifacts were not rewritten.

## Changes and regression checks

- `verify_draft()` adds guidance to its existing prompt: produce findings;
  the application handles saving. Existing storage, metadata, error handling,
  and number of calls remain unchanged. No filtering of generated text was added.
- Unnamed editor sections render as visible `div` containers. Named sections
  retain their existing `details` controls. The body/edit/copy paths are shared.
- In Conda `lexintel`, `pytest tests/test_agent_controller.py tests/test_agent_context.py -q`
  passed **72 tests** in 24.96 seconds, with existing dependency warnings.
- On isolated port 3011, `PW_PORT=3011 npm run test:e2e -- case-assistant.spec.js --grep 'unnamed draft bodies|agent candidates remain staged|runs Case Assistant|saved workflow restores'`
  passed **four browser tests** in 36.8 seconds. Coverage includes empty and
  whitespace-only section names, immediate/reloaded visibility, named collapse,
  manual editing, copy-control visibility, staging, and workflow restoration.

## Live repeat: expected versus observed

The exact [case 7 prompt](agent-foundation-live-cases.md) was entered once through
browser control in a fresh Deep-mode chat, with the synthetic fixture uploaded
through the UI. Real local Flask, Celery, and LLM calls ran. The idle main worker
was restarted to load the prompt; the local database was backed up first.

Chat/draft **248**, public ID `920c6211-c984-4126-8e1d-878b5a917172`;
workflow `wf_43f14f74-0be9-4cef-94b4-2c179e34823a`.

| Expected | Observed |
| --- | --- |
| Report findings without a false saving limitation. | R contains source/title/structure findings and coverage limits. No claim that saving was unavailable or the report was not saved. Browser **View saved result** and SQL both confirm the report exists. |
| Keep R tied to checked candidate A; publish revision B without another check. | One verification/report; result, report target, and step summary identify A. Revision targets A; B publishes. Final response distinguishes checked A from separately unchecked B. |
| Show unnamed paragraphs immediately after publication and reload. | Both paragraphs were visible without opening anything. DOM confirms a `DIV` section, zero summaries, and no `open` attribute. Manual-edit/copy controls are visible. |
| Preserve the source facts, requested title and limited revision. | Exact `Order status.` title; first paragraph is one sentence beginning `Agreed terms:`; twelve chairs, $20, $240, June 10 preserved. Second paragraph remains unchanged and qualified. |
| Preserve saved work and one final answer across reload. | A/R/B were observed staged with an empty editor. Final editor equals B. Reload retained one final turn and unchanged saved results/artifacts. |

Artifact IDs: A `d19a4ecf-a57c-42cf-859f-3370adcb6e18`;
R `cc961620-e3c0-4637-9ad2-794583f4ab6c`;
B `6da54ac2-6bb6-415b-8cac-605e53ab8ba2`.

Accepted-to-terminal SQL time was **53.945 seconds**, excluding **17.359 seconds**
of source preparation. Per-chat usage recorded **13 provider responses / 45,049 tokens**,
including instrumented conversation updates; this is not a billing estimate or
isolated provider latency. The original run also recorded 13 responses. This
single repeat is not evidence of a performance improvement.
All 13 provider responses were HTTP 200, with no logged retries or execution
errors. The 24 saved step results and six artifacts from the prior cases
retained their original hashes. No active workflows or preparations remain.

Evidence folder: `artifacts/live-case-assistant/verification-display-fix-20261107-0609/`.
See [final SQL](../../../artifacts/live-case-assistant/verification-display-fix-20261107-0609/case7-repeat-final.json),
[report preview DOM](../../../artifacts/live-case-assistant/verification-display-fix-20261107-0609/report-preview-dom.txt),
[reload DOM check](../../../artifacts/live-case-assistant/verification-display-fix-20261107-0609/after-reload-dom-check.json),
and [reloaded browser](../../../artifacts/live-case-assistant/verification-display-fix-20261107-0609/after-reload.jpg).
The [read-only repeat audit](../../../artifacts/live-case-assistant/verification-display-fix-20261107-0609/case7-repeat-independent-audit.json)
and [prior-record audit](../../../artifacts/live-case-assistant/verification-display-fix-20261107-0609/prior-live-case-integrity-audit.json)
record the saved-state comparisons.

The prompt is generation guidance, not a guaranteed semantic filter. No extra
LLM call, tool, agent-loop rule, verification gate, schema change, or migration
was introduced. This repeat does not exercise crash recovery or large-context
compaction. Changes were local and uncommitted at the time of this run.
