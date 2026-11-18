# CourtListener opinion selection

`read_authority` retains its existing nullable `opinion_id` argument. Start with
null: a complete one-opinion inventory is selected automatically; multiple opinions
return explicit numeric choices. No opinion bodies are fetched merely to list
choices. Inventory pagination, case membership and cancellation checks remain.
Webpages and PDFs continue through URL reading with a null opinion ID.

Lookup, search and choice responses normalize numeric IDs, opinion API URL strings
and older URL-only dictionaries through one helper. Each choice includes numeric
`opinion_id`, URL and available type/author details. The existing `id` alias remains
for compatibility. Public case permalinks are never parsed as opinion API IDs.
Older saved records are normalized on use; historical artifacts are not rewritten.

An invalid explicit selection returns `error_code=invalid_opinion_id`, its requested
ID and valid `opinions`, with the latest `research_id`/`source_id` for retry. It does
not silently select another opinion, create a passage/cache, mark anything read, or
record the source as unavailable. Actual network/access/content failures retain
their existing handling. Reading an opinion does not verify its legal application.

Offline tests replay the Perrin case-ID/opinion-ID confusion, successful retry,
automatic and multiple-opinion selection, older metadata, reload/cache reuse and
unchanged web/PDF behavior. Provider doubles prove data flow and recovery capability;
they do not establish that a live model always chooses the right next action.
