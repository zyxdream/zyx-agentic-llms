# Legal model-comparison benchmark v2

This pack tests independent review and revision of original pleadings through the
normal signed-in Workspace browser. It changes evaluation prompts and inputs,
not the application, model settings or infrastructure. Suites, recorded baselines
and comparison rules are indexed in [the live registry](../README.md).

## Workflows

| Workflow | Turns | Requests |
| --- | ---: | --- |
| Waymo | 4 | Compare original/amended complaints; propose ten provisional responses without client instructions; make a focused edit; verify against the pleadings. |
| Sullivan | 3 | Review five original motion paragraphs; investigate unresolved/conflicting authorities and revise those paragraphs; verify and explain the corrected draft. |
| Coomer | 3 | Review selected original opposition passages; investigate unresolved citations/quotations and propose supported replacements; verify and prepare a filing-lawyer handoff. |

Only four original PDFs, totalling 106 pages, are uploaded. Follow-ups introduce
no additional documents. Every workflow starts with a fresh project and
conversation and inherits only its own generated history. Do not reuse a v1
conversation: its previous uploads and corrections would change the test.

The exact prompts, order, original input hashes and upload lists are in
`scenarios.json` and `prompts/`. Requests do not name the known defective cases,
give the correct answers, explain internal tools, or restrict tool choices.
They allow the agent to correct its previous draft rather than preserve errors.
Waymo response choices require client confirmation; a complaint alone cannot
establish what a client should admit or deny.

## Keep evaluation references out of the conversation

Never upload the Sullivan correction letter, Coomer revised filing/show-cause
response/sanctions order, Waymo filed Answer, reviewer checks, manifests, reports
or evaluator notes. This applies to every turn. Selected reference file records
are separate from allowed uploads, and preparation checks reject overlap.

Use those references only after capture to evaluate the agent's work. The
correction letter is a guide to selected known issues, not a replacement for
checking the underlying law. Accept independently supported corrections even
when the wording differs from counsel's. The historical Answer does not establish
which response the agent should choose without the client's actual instructions.

Unrestricted live research may find public correction materials or sanctions
orders. Record the exact exposure and affected findings; do not claim independent
discovery for them. No application search filter or guarantee of a blind test is
introduced. Prior model knowledge is another limit. Do not conceal a discovered
source or silently rerun until a preferred result appears.

## Run and capture

Execution is opt-in and uses real providers. Use only Workspace frontend 3010,
backend 5010 and Redis 6390, with the normally authenticated evaluation browser.
Creating this pack does not run it or send documents to a provider.

From the repository root:

```sh
.venv/bin/python tests/live/model-benchmark/prepare.py --validate-only
.venv/bin/python tests/live/model-benchmark/prepare.py --label openai-v2 --cdp-port 64954
node tests/live/model-benchmark/browser.cjs /absolute/path/to/new-run run
.venv/bin/python tests/live/model-benchmark/collect.py /absolute/path/to/new-run
.venv/bin/python tests/live/model-benchmark/report.py /absolute/path/to/new-run
```

Preparation freezes the pack, configuration and code identity in a new ignored
run directory without contacting a model. An optional `waymo`, `sullivan` or
`coomer` final argument selects one workflow in the browser runner. It resumes
admitted run IDs instead of resubmitting and stops on failure or clarification.
Retain every failed/recovered attempt; browser failures and model/tool failures
are separate. Never export credentials, browser authentication or private model
reasoning. Earlier frozen runs must not be edited.

Capture includes exact requests, source/tool observations, function-call choices,
final chat responses, confirmed draft versions, screenshots and DOCX downloads.
Collection checks the DOCX ZIP, nonempty text and capture hash, and compares
captured Markdown with its confirmed version. Full visual layout and legal
correctness need separate review. Completion alone is not a quality pass.

The report generator summarizes observed metrics and errors. Semantic quality
remains pending until an evaluator records evidence in `quality-review.json`;
it does not copy conclusions from the earlier baseline.

## Review and compare

Evaluate initial findings, follow-up resolution, revised text and final
verification separately. For each relevant citation use, record what was read,
what was established and what remains unchecked. A lookup alone does not verify
a quotation or its application; an unsuccessful search does not prove invalidity.
Explicit unresolved outcomes are correct disclosure but incomplete substantive
review. Use the held-out checks as selected examples, not an exhaustive answer.

Version 2 changes both prompts and agent inputs. Its current OpenAI baseline is
`artifacts/model-benchmark-openai-budget-recovery-r2-20261118-202204`, run on
application commit `4d42f56` with `gpt-6-luna` at medium effort: 10/10 turns,
3180.594 agent seconds, 275 LLM calls, 26,926,920 tokens, 12 tool errors, zero
whole-run retries and 10/10 verified downloads. The run's `code-identity.json`
records the pre-rewrite head `4a15ebe` (now `e9da487`) because it started before
the change was committed; its application source hash is identical to `4d42f56`.

That run followed the fatal Sullivan drafting-budget failure in
`artifacts/model-benchmark-openai-condition-strength-20261118-131407` (7/8 turns).
The S1 `create_draft` that previously exceeded the budget completed with 19
requested materials resolved to 14. Compare later runs against the r2 baseline,
not against v1.

New runs after the role-policy change use `gpt-6.1-sol` medium for the
controller, analysis, writer and verifier, and `gpt-6-luna` medium for
preparation, research, search, utility and court formatting. The r2 run remains
the recorded all-luna baseline; label a new run separately when comparing the two
policies.

Version 1 remains historical evidence, not a matched
timing/accuracy comparison. Its frozen prompts and eight-input design remain in
`artifacts/model-benchmark-openai-20261115-152018/` and later replay directories;
do not rewrite those results with the new expectations.

The browser runner scopes its conversation locator to the active project's
conversation list. Earlier runs could collide when two projects both contained a
conversation named `Benchmark waymo`; keep the project-scoped locator. The failed
preparation attempt from that collision is retained at
`artifacts/model-benchmark-openai-budget-recovery-20261118-202106`.

Hold pack version, input hashes, application source, tool availability and the
model mix constant when comparing providers. Record controller, writer,
preparation and research helper settings. Changed helpers make a whole-system
comparison. Keep sources and exact generated histories, noting live-web changes.

Metrics retain per-turn agent time, model-call/token deltas, tool errors, retries,
search/read counts, output lengths, confirmed versions and download checks.
Preparation and operator pauses are separate. Repeated context contributes to
token totals; they are not billing figures or provider-only latency. One pass
does not establish a performance distribution. Retain all observations and
compare quality before judging speed.
