# Optional authority research and source-backed authoring

Use the local app and normal workers with real LLM/provider calls. Upload only
`../fixtures/authority-research/research-brief.txt` to a fresh chat. Do not upload
this test guide or tell the controller which tool to choose.

## Research

Submit:

```text
Review the legal references throughout the uploaded research brief. Identify the cases and statutes, investigate conflicting or unresolved references using legal databases and public sources where useful, and save your research. Keep repeated uses distinct. Explain what you located, what you actually read, and what remains uncertain; absence from a database is not by itself invalidity. Link useful sources. Do not change the draft panel yet.
```

Inspect saved operations and records, rather than only the final prose. Confirm
whole-file coverage, actual provider candidates, separate repeated uses, retained
conflicts, official statute sources or explicit limitations, and genuine source
URLs. No particular number or ordering of searches is required. Compare the
stored upload hash with its pre-run value.

## Authoring from source passages

Then submit:

```text
Using the saved research, publish a short memo explaining Brown's holding and relevant reasoning from the actual opinion. Read the relevant source passages before making those claims, cite the source links, and do not infer substantive support merely from case metadata or the research brief. Include a short table describing the other reference findings and their unresolved limitations. Do not rewrite or modify the uploaded file.
```

Confirm the agent can find prior research in the same chat; relevant selected
opinion passages are supplied to the writer; no blanket all-opinion fetch occurs;
and saved findings do not imply quotation, historical-law or subsequent-treatment
verification. Check Preview, Source, reload and actual clickable chat/source links
(visible labels alone are insufficient). Download Markdown,
HTML, Word and PDF from the published revision. Verify the links are preserved
as clickable destinations, including PDF link annotations, and downloads do not
add legal-research calls.

## Ordinary nonlegal assignment

In a separate fresh chat, request a small fictional invoice table/report with no
legal question or authority references. It should finish without automatic legal
lookup or web research during publication. This check concerns the removed
mandatory pipeline, not a restriction on the model's ability to select tools.

Record the actual workflows, source evidence, failures/recovery, source hashes
and any checks not observed. Keep raw provider bodies, logs and downloads under
ignored local test artifacts.
