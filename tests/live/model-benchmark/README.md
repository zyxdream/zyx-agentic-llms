# Legal model-comparison benchmark v1

This pack measures the existing Workspace agent with real providers through the
normal signed-in browser. It adds evaluation files only, not application logic,
model routing or a special legal workflow. The application baseline is commit
`43b4821`; actual source hashes, model settings and run evidence are saved per run.

## Workflows

| Workflow | Turns | Inputs and work |
| --- | ---: | --- |
| Waymo | 4 | Compare original/amended pleadings; add the filed Answer; draft ten aligned responses; make a focused edit; verify the current draft. |
| Sullivan | 3 | Audit five selected motion paragraphs; add counsel's correction letter; produce corrected paragraphs; preserve them through a client-facing follow-up. |
| Coomer | 3 | Review selected evidence/citation arguments; add counsel's response and revised filing; decide whether the revision cures the issues; verify and prepare a filing-lawyer handoff. |

Exact natural prompts, order, per-turn attachments, neutral upload filenames and
input SHA-256 hashes are in `scenarios.json` and `prompts/`. Each workflow gets a
fresh project and conversation. Project documents are uploaded once. Follow-up
attachments are introduced only on their specified turn, then retained normally.
Runs execute sequentially so these benchmark runs do not compete with one another.
The agent receives no tool restrictions or explanation of its internal tools.

The eight selected PDFs total 153 pages. Original PDFs remain private under
`instance/evaluation-inputs/`; the copied complete `hg-briefs` collection is also
private. Duplicate Sullivan PDFs, scanned Mata inputs, and other unused materials
are retained for future expansion, not counted in this baseline.

## Evaluation material stays separate

Never upload `reviewer-checks.json`, manifests, benchmark scripts, metrics, reports,
or evaluator notes. The Coomer sanctions order is an evaluator reference, not an
agent upload. Sullivan's correction letter is withheld on turn S1 and deliberately
introduced on S2. Score S1 detection separately from S2 guided correction.

These are not guaranteed blind tests: models can have prior knowledge and live
research may discover correction letters or court criticism online. Inspect and
record those sources. A cited case existing is not proof of its proposition;
a failed search is not proof of nonexistence. The key provides selected anchored
checks, not an exhaustive gold standard for every citation in these documents.

## Run and capture

Opt-in execution makes real provider calls. Use only this project's local frontend
3010, backend 5010, Redis 6390, and normally authenticated evaluation browser.
`prepare.py` creates a new ignored `artifacts/model-benchmark-<label>-<timestamp>/`
directory, validates hashes, freezes prompts/configuration/code identity, and
initializes fresh run state. Supply the existing signed-in Chrome CDP port; the
command does not start a browser or contact a model. Never export cookies, CSRF
tokens, passwords, API keys, or encrypted provider checkpoints.

From the repository root:

```sh
.venv/bin/python tests/live/model-benchmark/prepare.py --label openai --cdp-port 64954
node tests/live/model-benchmark/browser.cjs /absolute/path/to/run run
.venv/bin/python tests/live/model-benchmark/collect.py /absolute/path/to/run
.venv/bin/python tests/live/model-benchmark/report.py /absolute/path/to/run
```

An optional final argument `waymo`, `sullivan`, or `coomer` selects one workflow.
The runner resumes captured turns and existing run IDs rather than resubmitting.
It stops for failure/clarification; inspect and retain that attempt. Do not replace
an unsuccessful run with an unlabelled successful rerun. Browser-control failures
are recorded separately from agent/tool failures.

Capture records include exact user prompts, run IDs, durable tool arguments and
results, function-call decisions without private reasoning, source/research
artifacts, final responses, all confirmed draft versions, per-turn Markdown,
browser screenshots and actual DOCX downloads. Download checks reopen the ZIP,
extract document text, compare saved hashes, and match Markdown to its confirmed
version. Apply the evaluator checks manually with evidence; completion alone is
not a quality pass.

## Metrics and comparison rules

`metrics.json`/`.csv` contain per-turn agent wall time, decision/tool steps and
their elapsed times, successful LLM-call/token deltas, tool errors, explicit run
retries, research/search/read counts, exact repeated search arguments, candidate
counts, source counts, output lengths, versions and download timing/size.
Preparation timings are separate in `results/evidence.json`. Usage deltas use
fresh conversations and before/after counters; preparation and export model usage
are separate. Total tokens are not dollar billing or a substitute for a tokenizer-
independent quality measure. Totals include repeated/cached input tokens; the
current counters do not separate input, output, reasoning, or cached tokens.
HTTP retries/failed calls are not fully measured by
successful-response counters. Exact repeated arguments do not measure all wasted
research; inspect the saved queries, source reads and their outcomes.

Compare the same pack version, input hashes, prompts, application source and
workflow order. Preserve actual generated conversation history per workflow.
First-turn inputs are fixed; later turns naturally depend on earlier generated
answers, so this is an end-to-end workflow comparison, not identical-context
inference timing. Model decision and tool-step timings include orchestration and
queue time, not provider-only latency. Separate preparation, runtime, downloads
and human pauses. Document hardware, worker concurrency and any competing load.

Record the full model mix: controller, writer, preparation, legal/web research and
other helpers. When changing only the controller, hold auxiliary model settings
constant. If those also change, label the result a whole-stack comparison.
Record native web-search/tool availability; a different provider's search facility
can change both quality and time. Preserve retrieved evidence, but acknowledge
that future live search results/network conditions can differ.

The initial pass is one observation per turn. It establishes a reproducible
reference and exposes failures; it cannot establish a latency distribution or a
statistical performance regression. Before making a model-choice claim, repeat
each workflow on both models with fresh projects, retain all attempts, compare
quality first, and report runtime/call/token variability separately.

## Initial OpenAI baseline

Executed on November 15, 2026 with application commit `43b4821` and real OpenAI
gpt-6-luna (medium): three projects, ten turns, eight input PDFs. All ten turns
completed and all ten downloaded DOCX/confirmed-Markdown checks passed. Quality
was mixed: Waymo's core alignment/edit checks passed; Sullivan retained unresolved
authorities; Coomer missed a known citation defect and introduced a wrong court
during final verification. No application fix was made to improve this baseline.

Private evidence and the per-workflow review:
`artifacts/model-benchmark-openai-20261115-152018/REPORT.md`.
The report separates eight model-argument error observations, four external
failures, zero explicit run retries, and unresolved coverage/design findings.
Summed agent time was 42 min 43.688 s; ingestion, downloads and operator pauses are
separate. Keep the complete private run directory and `baseline-sha256.json`
for comparison; the original model outputs contain the observed mistakes.
