# Live browser cases 6–7: clarification memory and verification provenance

These two synthetic user cases extend the [existing five live cases](case-assistant-agent.md).
Run them through the normal browser UI, backend, Celery worker, and selected
LLM. Here, “mock” describes the fictional order/invoice record and user task;
do not mock API responses, workflow progress, tool results, or final answers.
These cases add no product behavior or mandatory agent workflow.

Case 6 covers a clarification answer carried into a later assignment, exact
wording, and chat-only analysis. Case 7 covers a verification report tied to the
candidate actually checked, followed by a permitted revision without another
verification. The agent chooses its own source reads, analysis, and operations;
the review/edit stages below are explicit user requests rather than a harness
that executes tools for the agent.

## Common setup and evidence

- Finish or cancel unrelated active assignments before restarting services.
  Start the current backend, main worker, and Beat together, then reload the
  frontend. Record the branch/commit, model, running service configuration,
  and any local uncommitted changes that affect the run.
- Use a fresh dedicated chat for each case, Deep mode, empty Case Background,
  and only [order-invoice-correction.txt](../fixtures/order-invoice-correction.txt).
  Upload the fixture through the browser and wait for preparation. Do not reuse
  previous final answers as source material.
- Record chat/draft IDs, prepared source IDs, and the initial editor contents
  or hash. An empty editor is a valid Case 6 baseline; state that it was empty
  rather than claiming the run proved protection of an existing document.
- Submit the exact prompts once through the UI. Record workflow IDs, actual
  operation names/arguments/results, states, artifact references, timestamps,
  visible outcomes, and screenshots. Do not expose private provider reasoning.
- Reload at the checkpoints below without resubmitting. Confirm one final
  response per submitted assignment and no duplicate execution/publication.
- Inspect SQL history/artifacts read-only to establish associations and facts
  that browser labels alone cannot prove. Keep any fixture setup separate from
  observed agent execution.

The source establishes twelve chairs at $20 each, an agreed total of $240 and
June 10 deadline; the invoice instead lists $260 and June 12, both disputed.
It establishes no acceptance, payment, completed delivery, agreed change, or
dispute resolution. References must identify the real filename/numbered
sections, without invented pages or outside authorities.

## Case 6: use a clarification in a later chat-only assignment

### Exact user inputs

Submit the first assignment:

```text
Using only the attached "order-invoice-correction.txt", help me prepare a short status note. Before answering, ask whether I want the note to focus on the agreed order terms or the disputed invoice entries; do not choose that scope for me. After I answer, return only the amount and delivery date for the scope I selected, with the filename and relevant section. Do not include a heading or other commentary in that first answer. This is analysis only: do not create or change the editor draft, and use no outside research.
```

When the agent asks the scope question, answer through the clarification UI:

```text
Use the agreed order terms. For my next follow-up, use the exact heading "Order status." including the final period. Do not include that heading in your first answer.
```

Wait for completion and reload. Then submit the second assignment in the same
chat, without repeating the earlier heading or selected scope:

```text
Now turn that into a short note with two client-facing bullets, using the heading I specified earlier and the same scope I selected. Preserve the source's uncertainties about acceptance, payment, delivery, changes to the agreed terms, and dispute resolution. Cite the filename and real section references. Keep this in the chat only; leave the editor unchanged and use no outside research.
```

### Expected behavior and observations

1. The first assignment pauses for the requested scope question. Its saved
   clarification step contains the actual question and the complete answer.
   Answering resumes that assignment rather than creating a second workflow.
2. The first final answer reports **$240 / June 10**, with the filename and
   section 1, and follows the requested narrow form without the heading.
   If it repeats the heading anyway, record that formatting defect and that
   the later answer no longer isolates retrieval from clarification history.
3. The follow-up has the exact heading **Order status.**, including the final
   period, and two bullets focused on agreed terms. It preserves source
   uncertainty, distinguishing any invoice entries it mentions from agreement.
   Inspect the second assignment's saved conversation input: the first
   assignment's actual clarification question/answer should be available even
   though the first final answer omitted the heading. Do not infer that input
   merely because the final heading happens to be correct.
4. The editor remains equal to its recorded baseline after both assignments
   and reload. There is one final answer for each assignment. No outside
   source or unsupported assertion enters the result.

This is a normal user-instruction and clarification-memory test. If the agent
never attempts candidate publication, editor immutability does **not** establish
that the editor-write permission guard executed. Record that distinction; do
not induce a forbidden publication just to exercise the guard in a live run.

## Case 7: check candidate A, then publish wording revision B

Use a second fresh chat and upload the same fixture through the UI. This is
one assignment so the report, checked candidate, and revision stay in the same
operation history and artifact scope.

### Exact user input

```text
You may change the editor for this assignment. Using only the attached "order-invoice-correction.txt", prepare a short draft titled exactly "Order status." including the final period. Use two short paragraphs: first the agreed order terms; second the disputed invoice entries and what the record does not establish. Preserve all qualifications, and cite the filename and relevant numbered sections. Save a verification report of that version against the source and my exact title requirement. After that review, make one final wording-only revision: make the first paragraph a single sentence beginning exactly "Agreed terms:" while keeping its unit count, unit price, total, and deadline. Keep the title and second paragraph unchanged. Publish that revised version. Do not run a second verification or create another verification report for the final wording-only revision. In your final response, say which version the report checked and that the published wording-only revision was not checked separately. Use no outside research or legal authorities.
```

### Expected behavior and observations

1. The first saved draft candidate **A** has the exact title **Order status.**,
   two paragraphs, correct agreed terms, disputed invoice entries, and the
   source limitations. Inspect the actual artifact IDs; A/B/R are labels for
   this explanation, not IDs or function names passed to the application.
2. A saved verification report **R** checks A against the source and the
   original exact title requirement. Its returned result, saved report
   `target`, and saved step summary all identify A. The report also records
   the exact checking `instructions`, `check_citations`, and execution `error`
   flag. A title-period complaint contrary to the original prompt is a
   semantic failure, even if artifact associations are correct. Report prose
   should contain findings, without claiming that saving was unavailable or
   that the saved report was not saved; persistence status comes from the app.
3. The later wording revision produces a distinct candidate **B**. Its first
   paragraph is one sentence beginning **Agreed terms:**, preserving twelve,
   $20, $240, and June 10. Its title and second paragraph remain unchanged.
   Inspect the revision inputs/replayed results to establish that the agent
   had R's findings available; do not assume that several completed labels
   establish this connection.
4. R remains a report about A, with unchanged saved contents and target after
   B exists. There is no second verification/report for B and no claim that R
   checked B. B publishes successfully: verification is an optional ordinary
   operation, not a publication gate.
5. The final response identifies the checked version and clearly qualifies
   that the published wording revision was not checked separately. The editor
   contains B after completion and reload, with one final response and one
   publication for the assignment. An unnamed body section must show its
   paragraphs immediately, without opening a browser-generated Details control.

Observe candidate staging if possible: creating/checking/revising candidates
should leave the editor at its baseline until publication. If no intermediate
checkpoint was captured, mark staging **unobserved** rather than passed.
Additional source reads or analysis are allowed; the requested review then
wording revision must be observable without requiring one exact global tool
sequence. If the agent omits the requested first report, correct final text
alone does not cover verification provenance.

## Performance and result record

Create a separate dated result report with an expected-versus-observed table
for each case and links to local evidence. Include:

- Source preparation time separately from assignment execution.
- Each workflow's accepted-to-terminal elapsed time, actual model/tool
  operation order and duration, recoverable operation errors, and publication
  outcome. Prefer saved timestamps and identify the measurement used.
- For Case 6, time to the clarification question, human answer wait, resumed
  processing time, and follow-up assignment duration separately. Human waiting
  must not be presented as model/backend processing time.
- IDs for the clarification, candidates, report and checked target; editor
  baseline/final comparison; reload outcome; duplicate/error counts.
- Observed bugs, possible improvements, and unobserved or untested behavior as
  distinct findings. No arbitrary latency pass threshold is introduced by
  these cases; report measured performance rather than calling it fast/slow
  without a comparison.

These live runs do not establish crash recovery, cancellation races, context
compaction under large histories, production database locking, or concurrent
duplicate-delivery safety. Those remain separate regression/harness coverage.
Do not deliberately crash shared workers or force oversized inputs for these
two user cases. The scenarios are specifications; only a dated result report
should claim an executed pass or failure.
