# Luna document preparation and relevance lookup example

This example uses actual `gpt-6-luna` medium-reasoning output from the synthetic `escrow-record.txt` live smoke check. It is not a customer upload. The source and Luna output are saved together so the retained facts and evidence can be inspected.

The preparation calls were executed live. The query walkthrough below illustrates how the current lookup design consumes those artifacts; it is not a claim that a separate live planner/database search was executed for this walkthrough. Document and published-artifact IDs in the rendered card are placeholders because the smoke check made no database writes.

## Files

- [Full synthetic source](examples/document_preparation/escrow-record.txt)
- [Actual Luna preparation output](examples/document_preparation/escrow-record.preparation.json)
- [Planner card rendered by the current code](examples/document_preparation/escrow-record.planner-card.json)

## Preparation result

| Property | Actual result |
| --- | --- |
| File | `escrow-record.txt` |
| Model and reasoning | `gpt-6-luna`, medium |
| Preparation version | `luna-source-records-v2` |
| Readable source | 32,104 estimated tokens; 221,333 characters |
| Completed section records | 2 of 2 |
| Readable character coverage | 221,333 of 221,333 |
| Original extraction | Complete for this controlled text fixture; no gaps |
| Generated preview | 157 tokens |
| Generated detailed digest | 316 tokens |
| Rendered planner card | 409 tokens with placeholder IDs; real IDs affect the rendered size |

The source is long because it contains repeated administrative filler around a small number of substantive clauses. The digest is therefore shorter than the normal 1–2K-token target. This fixture checks content retention across a long input; it does not demonstrate the information density of a complex contract or establish PDF extraction fidelity.

## What appears in the document list

**Filename:** escrow-record.txt

**Headline:** Escrow Record: Delivery Timing, Release Objection Terms, and Separate Breach Allegation

**Document type:** Escrow record and administrative correspondence excerpts

**Actual Luna preview:**

> The supplied record identifies North Company and South Company and states that delivery is due at the end of a Relevant Period, defined as ending March 15, 2031. It does not say what must be delivered, to whom, or by what method. A separate term states an escrow release amount of $77,777 and requires a release objection within forty-five days, while preserving a fraud objection afterward. The text does not identify the event that starts the forty-five-day period, who must submit an objection, or how objections are handled. A further section reports that South Company alleges breach by North Company, but says the correspondence does not report a judicial finding of breach. This allegation is not an adjudicated finding. Repeated administrative-background text is described as adding no operative terms.

Luna also generated four key points, parties, dates and topics. The UI can expose that richer metadata and load the detailed digest on expansion. The planner renderer applies a separate 450-token ceiling: this card retains two key points and removes the separate parties/dates/topics arrays to fit. Its preview still contains those substantive parties and dates. See the saved rendered card for the exact fields.

## Detailed digest on expansion

The following is the actual generated digest, without a three-sentence restriction:

> The record identifies North Company and South Company as parties. Its delivery provision makes delivery due at the end of the Relevant Period. A definition states that the Relevant Period ends March 15, 2031, and expressly applies that definition to the delivery obligation stated at the beginning. The available text does not specify the delivery item, recipient, or method.
>
> A separate provision states an escrow release amount of $77,777. It requires a release objection to be delivered within forty-five days and provides that a fraud objection remains permitted afterward. The text does not identify the event from which the forty-five days run, who is responsible for delivering an objection, or a procedure for submitting, evaluating, or resolving objections. It also does not define fraud or describe how a fraud objection is asserted. These provisions state terms and an exception; they do not establish that an objection or fraud occurred.
>
> The record contains extensive repetitive administrative-background wording. Paragraph 1 describes such background as correspondence context that adds no operative deadlines, amounts, duties, or exceptions. The same characterization recurs in later text, including paragraphs 1371 and 1600. A separate section states that South Company alleges breach by North Company. It characterizes this as an allegation and says the correspondence does not report a judicial finding of breach. That statement concerns what the correspondence reports; it does not establish whether a finding exists outside it. The source gives no details of the alleged conduct or North Company's response. Accordingly, the alleged breach should not be treated as a finding or linked to a particular escrow term absent further information.

## Structured information available to lookup

These are selected actual section facts, condensed for display. Their exact statements, quotes and offsets are in the saved preparation JSON.

| Information | Attribution | Source block | Canonical character range |
| --- | --- | --- | --- |
| Delivery is due at the end of the Relevant Period | Obligation | `b000002` | 82–132 |
| Relevant Period ends March 15, 2031 | Source statement and definition | `b001204` | 165889–166020 |
| Escrow release amount is $77,777 | Source statement | `b000403` | 55334–55371 |
| Release objection is due within forty-five days; trigger unspecified | Obligation | `b000403` | 55372–55432 |
| Fraud objection remains permitted afterward | Exception | `b000403` | 55434–55487 |
| South Company alleges breach by North Company | Allegation | `b001605` | 221222–221268 |
| Correspondence does not report a judicial finding | Source statement about the correspondence | `b001605` | 221269–221333 |

Ranges are zero-based character offsets with an exclusive end. These controlled fixture blocks do not provide PDF page numbers; none are invented. The full source preserves the text at every range. In ordinary uploads, available page/paragraph/part locations are also carried through the source map.

The complete section records retain uncertainties as well, including the unspecified objection-period trigger, delivery item, responsible party and objection procedure. Section-level statements must retain their section scope; the final digest reconciles information from both sections.

## Example relevance lookup

**User prompt:** “Can a fraud objection still be raised after the normal objection deadline, and what amount is at stake?”

1. The server freezes the authorized document/artifact snapshot. This file's compact card appears with current attachments, prior attachments or matter background according to its actual scope.
2. The planner can identify this file from its preview and key points. Example query expansions include “escrow release objection,” “fraud exception,” and “forty-five days.” These expansions are illustrative, not captured planner output.
3. Structured-section and original-chunk search run within the same permitted snapshot. The original clause remains searchable even if a future compact card omits it.
4. Candidate review can load this file's detailed digest and matching section records/passages in a bounded batch.
5. Evidence extraction uses the original passage, including the exception:

> The escrow release amount is $77,777. A release objection must be delivered within forty-five days, except a fraud objection remains permitted afterward.

This passage is source block `b000403`, characters 55334–55487 of the saved source. The answer should preserve both the general period and the fraud exception, and explain that the source does not specify when the forty-five days begin. It should not infer that fraud occurred or that a court found breach.

For “find every deadline,” the workflow enumerates all requested source sections instead of relying on this compact card. It must also read the delivery obligation and the separately located Relevant Period definition.
