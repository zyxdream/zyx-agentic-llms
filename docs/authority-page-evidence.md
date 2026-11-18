# Published-page evidence for quotations

New CourtListener source caches preserve explicit `span.star-pagination` boundaries
when the already-returned HTML and case citations identify one publication. The
reader extracts text and boundary positions together. It does not align HTML
positions to a different plain-text representation. A Lawbox publication heading
can identify its citation among parallel citations; ambiguous streams, conflicting
labels, missing page sequences and unsupported formats remain unverified.

`read_authority` returns one excerpt with `page_evidence`: original page labels,
text ranges, boundary text and positions, next boundary or scope end, publication
citation, originating field, URL, opinion ID and source digest. Boundary evidence
may lie outside the excerpt; it does not imply that intervening text was read.
`matched_published_pages` maps only the matched original-text range, including an
alternative match, or is null when unsupported or when no phrase matched. Plain
reading can return page evidence without claiming a quotation was located.

The controller receives the evidence directly for general responses. It is also
saved with the passage; selecting that passage supplies identical evidence to
analysis, writing, revision and verification after reload. A short phrase match
never verifies a longer quotation or its legal application. Without sufficient
page evidence, report "not verified from the retrieved sources"; supported content
findings remain usable. Physical PDF pages and arbitrary asterisks are not reporter
pagination. Footnotes do not inherit main-text pagination.

The existing excerpt limit (12,000 characters) and leading context (1,000) control
reading volume, not page verification. Match offsets, continuation, alternative
queries and cache reuse are unchanged. Old caches and historical artifacts are
not rewritten; missing retained pagination remains unverified. No additional
runtime request/model call, migration or automatic substantive review is added.

Validation uses a saved public Calpine Lawbox response plus mocked providers. The
fixture was retrieved from CourtListener opinions/1833489 on 2026-11-17; its digest
is checked by the tests. Tests prove evidence delivery, not live model compliance.
