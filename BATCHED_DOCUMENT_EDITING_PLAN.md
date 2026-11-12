# Multiple batch document editing

Deferred design for extending Case Assistant's anchored, preserve-original editing to documents whose selected passages exceed one edit request. This work is not implemented. The existing production document preparation and scoped retrieval work is separate.

The proposed workflow is one frozen edit plan, multiple isolated edit batches, deterministic reconstruction, and atomic publication. Start with independent local edits. Document-wide restructuring requires dependency-aware phases.

## Current behavior

The preserve-original path loads canonical extracted text, assigns stable paragraph anchors, selects targets from compact previews, and sends selected paragraphs with neighboring context in one combined edit request. Both the target map and editable payload have an approximately 104K-token application guard. The scope name `whole_document_batch` does not currently execute multiple batches.

The new design must address target selection and final reconstruction as well as the edit call; splitting only the editable payload would leave other large-document bottlenecks.

## Proposed workflow

1. **Freeze authorized source and target versions.** Record the authorized document/artifact snapshot, current draft revision, canonical source hashes, stable block IDs and edit instructions. Every batch uses the same source. Revalidate authorization and versions before execution and publication. Concurrent user edits must not be overwritten.
2. **Create a shared edit plan.** Identify affected sections, permitted changes, shared terminology, definitions, relevant source passages and preservation constraints. Use bounded section-level target selection when the full target map is too large. Whole-document operations must enumerate every eligible block; targeted operations record their selected scope.
3. **Assign exclusive editable blocks.** Each block has one batch owner. Include neighboring paragraphs, governing definitions, exceptions and dependent clauses as read-only context. Keep boundary-spanning clauses together where possible. Oversized paragraphs need stable child ranges and exact parent reconstruction. Avoid overlapping writable ranges.
4. **Budget input and replacement output.** Count instructions, schemas, shared context and source text, then reserve enough generation capacity for full replacements and reasoning. Initially evaluate approximately 8–12K editable source tokens per batch; tune this target using fidelity, latency and output-size measurements. Never infer that input fitting guarantees output fitting.
5. **Execute resumable batches.** Persist run, plan and batch IDs; source/target versions; owned anchors; context references; model settings; attempts; leases; results and validation outcomes. Use limited concurrency for independent batches and ordered phases for dependent changes. Keep transactions closed during provider calls. Retry unfinished work without repeating successful batches. Cancellation fences late workers.
6. **Validate results before merging.** Require a changed, unchanged or needs-more-context outcome for every assigned block. Accept replacements only for the batch's owned anchors with matching original text hashes. Reject malformed results, unknown or duplicate anchors, incomplete output and unauthorized deletions. A missing result is not an implicit unchanged result. Requests for extra context trigger bounded retrieval and retry or an explicit block.
7. **Reconstruct deterministically.** Apply validated replacements to the frozen canonical source in document order. Retain untouched text and structural mappings. Do not send the full merged document to an LLM for regeneration or restructuring; that would reintroduce context limits and risk changing preserved text. Preserve exact separators and parent/child range boundaries.
8. **Check cross-section consistency.** Review changed blocks, their neighbors and relevant dependencies in bounded passes. Check defined terms, parties, amounts, dates, exceptions, numbering, cross-references and preservation constraints. Consistency corrections are explicit anchored operations with their own validation. Hash checks establish mechanical integrity, not semantic correctness.
9. **Publish atomically.** Once every required batch and check succeeds, revalidate the target revision and save the complete revision, change record and provenance. Keep the previous draft available. A failed batch leaves the current draft unchanged and successful checkpoints available for retry. Preserve existing target confirmation and review behavior; batching does not require a separate confirmation for every batch.

## Scheduling by edit type

| Edit type | Execution |
| --- | --- |
| Independent paragraph corrections | Parallel batches with shared instructions |
| Consistent terminology changes | Shared rules across batches, followed by a consistency check |
| Moving sections, changing definitions or renumbering | Ordered phases based on dependencies |

## Initial delivery scope

Implement multiple batches of local, anchored edits first, including bounded target selection, durable recovery, exact reconstruction and atomic publication. Add document-wide reorganization after dependency-aware planning and cross-section validation are established.

Preservation refers to canonical extracted text and the application's draft structure. Preserving original Word/PDF layout requires format-aware editing in addition to this design.

## Required validation

- Exact equality of untouched source ranges, including separators and Unicode boundaries.
- Complete accounting for every assigned block and every section of a whole-document operation.
- Boundary-spanning obligations and exceptions, locally scoped definitions, repeated terminology and cross-references.
- Output expansion, oversized paragraphs, incomplete provider results and missing context.
- Unknown, duplicate, out-of-scope and conflicting edits; explicit deletion handling.
- Worker crashes, rate limits, retries, cancellation and stale-worker publication.
- Concurrent user edits, changed artifact versions, scope changes and cross-user/matter/chat isolation.
- Final reconstruction without an oversized whole-document LLM call.
- Representative fidelity, latency, token usage and recovery measurements before rollout.
