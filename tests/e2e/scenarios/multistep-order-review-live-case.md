# Live browser case 8: reconcile conflicting records before drafting

This more complex synthetic user case extends [cases 6–7](agent-foundation-live-cases.md).
It uses six short fictional records, an audience clarification, an audit of an
incorrect uploaded summary, cross-document analysis, a reconciliation table,
and a checked final memo. Run through the real browser UI/backend/Celery/LLM;
do not mock decisions, tool results, or progress. No new product behavior or
general mandatory tool sequence is introduced.

## Setup

Create a fresh dedicated test chat, empty Case Background, Deep mode, and an
empty editor. Upload only these six files through the UI and wait until each
is prepared:

1. [01-signed-order.txt](../fixtures/multistep-order-review/01-signed-order.txt)
2. [02-signed-amendment.txt](../fixtures/multistep-order-review/02-signed-amendment.txt)
3. [03-vendor-invoice-and-message.txt](../fixtures/multistep-order-review/03-vendor-invoice-and-message.txt)
4. [04-receiving-note.txt](../fixtures/multistep-order-review/04-receiving-note.txt)
5. [05-payment-and-credit-ledger.txt](../fixtures/multistep-order-review/05-payment-and-credit-ledger.txt)
6. [06-status-draft-to-review.txt](../fixtures/multistep-order-review/06-status-draft-to-review.txt)

All dates/parties/amounts are fictional. **September 12, 2026** is the reporting
cutoff, independent of the day the test runs. The sixth file is the document
to audit, not authoritative evidence or an editor baseline. Record accepted
document/prepared IDs and baseline state before submission.

## Exact user inputs

Submit once:

```text
Review these six fictional order records as of September 12, 2026, using only the supplied files. Before writing the memo, ask whether it is for our internal team or for sending to the vendor; do not choose that audience for me. The uploaded "06-status-draft-to-review.txt" may be wrong: save a verification report checking it against the original records, then use the supported findings to prepare a replacement memo. Save your analysis of the material conflicts and calculations so it can be inspected with the draft. Reconcile the signed order and amendment, the vendor's invoice and assertions, physical receipt, confirmed payments, and the conditional credit request. Include a small plain-text/Markdown financial table that clearly separates the amended agreed basis from the invoice claim, shows the supported balance before any unapproved credit, and labels any approved-credit illustration as conditional. Include a short dated timeline, what the evidence does and does not establish, and practical unanswered questions or records we should obtain. Check the candidate you intend to publish against the originals and my clarification, correct it if needed, and finish with the memo in the editor plus a short chat explanation of the major corrections and remaining uncertainties. Do not treat a draft or a vendor assertion as independent proof. Do not infer invoice acceptance, full performance, a refund, or an overdue balance where the evidence does not establish it. Do not use outside research or legal authorities, and do not send anything to anyone.
```

Answer the audience clarification through the UI:

```text
Internal team only. Use the title "Internal order reconciliation — September 12, 2026". Keep the tone neutral: distinguish the vendor's assertions from supported facts, and do not accuse anyone of dishonesty or breach. The table should call the supported arithmetic remainder "balance pending reconciliation", not an amount currently due. Keep any hypothetical credit separate from confirmed payment. Do not write a vendor demand or send anything.
```

The user asks for these work products and checks, not function names. Source
listing/search/reads, report reads, additional analysis, candidate revisions,
and clarifications may occur in any useful order. Do not manufacture an error
or force a revision when the first replacement candidate is already correct.

## Independent expected facts and arithmetic

| Topic | Expected interpretation and source |
| --- | --- |
| Original agreement | 24 chairs × $50 + $90 flat freight = **$1,290**, original September 10 deadline; file 01 §§ 2–3. |
| Amended agreement | 28 chairs × $50 + the same $90 freight = **$1,490**; 20 due September 10 and 8 due September 17; deposit/payment conditions unchanged; file 02 §§ 2–3. Do not add freight twice. |
| Vendor claim | 28 × $55 + $90 + $60 rush = **$1,690**; file 03 § 1. Difference from amended basis is **$200**, comprising **$140** unit-rate difference and **$60** rush. The vendor's phone-authorization/all-received assertions are not independently established by this record. |
| Receipt | **20 physically received, including 2 damaged**, not 22 or 28; file 04 §§ 1–2. Signature is not quality/invoice acceptance. Replacement or damage resolution is not established. |
| Remaining delivery | Receipt of **8** is unconfirmed as of September 12. Their September 17 amended deadline is still in the future; do not call it missed or infer that absence of a receipt proves non-delivery; file 04 § 3. |
| Confirmed payment | **$400** cleared deposit; no other payment recorded in this supplied extract, rather than proof none exists elsewhere; file 05 § 1. |
| Supported arithmetic remainder | **$1,490 − $400 = $1,090**, labeled **balance pending reconciliation**. It is not an established currently payable/overdue amount: complete delivery AND invoice acceptance are required and not established; files 01 § 3, 02 § 3, 04–05. |
| Conditional credit | **$140 requested, not approved/applied/refunded**. If later approved against the amended basis, **$1,090 − $140 = $950**. Keep that illustration separate from the confirmed payment and supported remainder; file 05 §§ 2–3. |
| Invoice-basis comparison | Vendor invoice less confirmed deposit is **$1,290**. The old draft's **$1,150** is $1,690 − $400 − unapproved $140: arithmetically possible, but unsupported as an agreed/current amount due. Do not conflate this $1,290 with the original order's coincidentally identical total. |
| Timeline | August 28 original order; August 30 deposit; September 3 amendment; September 10 receipt; September 11 invoice/message and credit request; September 12 cutoff; September 17 remaining contractual deadline, not a completed past event. |

The draft audit should identify unsupported price/rush agreement, full delivery
and acceptance, extra/replaced damaged chairs, approved credit, overdue balance,
missed remaining deadline, and absence of unresolved questions. A sound memo
should seek remaining delivery evidence, invoice/price/fee support or correction,
credit decision, damage/replacement status, and invoice acceptance/payment
reconciliation. Wording may vary; do not add unstated legal conclusions.

## Observe the actual multi-step work

- Capture the workflow and actual question/answer; verify the saved answer
  reaches the later drafting/checking inputs. The memo must use the exact title,
  neutral internal tone, and **balance pending reconciliation** label.
- Inspect the uploaded-draft audit's target/file ID, evidence/material IDs,
  findings, and later use. Inspect the saved analysis and arithmetic. Correct
  final text alone does not establish that the agent used its earlier findings.
- Inspect final verification's report-to-candidate target. Any subsequent
  revision remains a different candidate; record which candidate was checked
  and any disclosed limitation without imposing an application publication gate.
- Observe that report/analysis/candidate creation stays private until final
  publication. If no intermediate checkpoint was captured, mark it unobserved.
  Reload during/after work without resubmitting and check one final answer,
  stable workflow, saved artifacts, and final memo/table persistence.
- Compare every money/date/quantity claim and qualification to the six originals.
  There should be no outside research, authority citations, vendor communication,
  unsupported accusation, or hidden assumption resolving the contradictions.

## Performance and reporting

Record six-file preparation time separately; accepted-to-question time, human
answer waiting, resumed execution, total workflow duration, actual decision/tool
order and durations, provider usage where instrumented, errors/recoveries,
artifact IDs, editor staging, and reload result. Step durations include queueing;
instrumented counters are not automatically complete billed cost. No new latency
threshold is imposed. Save a dated expected-versus-observed report, distinguishing
achieved criteria, bugs, unobserved checkpoints, and possible improvements.

This case adds cross-document dependency and conflicting/conditional evidence
within current tools; it does not test spreadsheets, external writes, outages,
concurrent workers, context-limit compaction, or legal research. The arithmetic
oracle is test-specific evidence for this fictional record, not a new app rule.
