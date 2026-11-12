# Future improvements

Track improvements to consider in later work. Entries marked **Deferred** are
saved for future discussion and implementation.

## Preserve requested tables in generated drafts

**Status:** Deferred. **Observed:** October 28, 2026, Waymo/Uber local test.

The user requested a table connecting disputed issues to supporting passages and
missing evidence. The generated draft instead contained plain paragraphs. Its
introduction described a tab-separated table, but the saved content contained no
tabs or table structure.

The Case Assistant draft templates explicitly prohibit Markdown tables in
`section_content`, conflicting with requests that require a table. See the
formatting instructions in [backend/everything.py](backend/everything.py).

**Future work:** Reconcile the drafting instructions with explicit table requests
and check table handling through draft structuring, editing, display and export.
Changing the prompt alone may not preserve table structure through those steps.

**Acceptance:** A request for an issue/evidence table produces actual rows and
columns with the requested information and source references, retained through
display, reload and export. Ordinary prose drafts retain their current formatting.

## Other saved plans

- **Deferred:** [Multiple batch document editing](BATCHED_DOCUMENT_EDITING_PLAN.md)
  for exact-source edits that exceed one model request. Keep the detailed design
  in that existing file.
