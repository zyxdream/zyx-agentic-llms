# Waymo guided-drafter model benchmark

This is an isolated, one-case comparison—not a production model switch. The **First Amended Complaint** (`tests/e2e/fixtures/sample-waymo-fac.pdf`) is the only case document provided to candidates. The filed Answer (`tests/e2e/fixtures/sample-waymo-answer.pdf`) and the Attorneys’ Eyes Only RFP (`tests/e2e/fixtures/sample-rfp-uber.pdf`) are **judge-only contextual references**. Their lawyer knowledge, later events, admission choices, and discovery facts are not available to a candidate and must not be expected in its draft.

## Run from the repository root

Open a terminal and run:

```bash
cd /path/to/lexintel
conda activate lexintel
python -m benchmarks.waymo.estimate
```

The root `.env` is loaded by `backend.ai.base` when the benchmark imports the existing OpenAI client. You do **not** need to paste or export the key. Run these commands from the repository root so the `.env`, prompts, and PDF paths resolve correctly. The three source PDFs are tracked fixtures, but each developer must prepare their own ignored working fixture. The preparer refuses to overwrite an existing v2 fixture unless explicitly given `--force`.

### 0. Prepare the local fixture in a fresh checkout

```bash
python -m benchmarks.waymo.prepare
```

This creates `artifacts/waymo_benchmark/prepared-v2.json` without an LLM call. If that file already exists, keep it: it records source paths and hashes for that particular run, and preparation will not overwrite it by default.

### 1. Freeze common FAC-only extraction once

```bash
python -m benchmarks.waymo.freeze --execute-paid
```

This invokes the existing Answer factual-digest and Element Checker upload-analysis routes once, then saves their outputs in the ignored private fixture. It refuses a second paid freeze. It does not read the filed Answer or RFP text for candidate inputs.

### 2. Generate the 24 product outputs

```bash
python -m benchmarks.waymo.pilot --stage generate --warn-cost-usd 400 --execute-paid
```

After generation, run `python -m benchmarks.waymo.pilot --stage report` to inspect the local known-usage estimate and any `cost_unknown` runs. You can also check billed API cost in the OpenAI Platform Usage/Costs dashboard for the project and time period used by this pilot before starting evaluation. **This review is optional; neither it nor `--warn-cost-usd` stops the benchmark.** The ordinary application key cannot query the organization Costs API, which requires an Admin API key.

The four candidates are `gpt-5.5`/medium, `gpt-6-sol`/medium, `gpt-6.1-sol`/medium, and `gpt-6.1-sol`/high. Each runs twice in each product: Answer, Element Checker, and RFP (24 isolated runs). The two runs are independent repetitions with identical inputs and settings, not different prompt configurations. Every Answer run receives the same FAC-only **default `deny`** for all numbered paragraphs; it is a controlled benchmark input, **not** a recommended legal response and **not** copied from the filed Answer. Each run independently suggests defenses, drafts paragraph responses, and drafts every distinct suggested defense. Counterclaims are off. Element Checker varies legal-element, weakness, citation-suggestion, and recommendation calls, while upload extraction is frozen. RFP varies proposals, consolidation, and Further Reduce; only exactly 30 nonblank, textually distinct final requests pass the automatic completeness check. Substantive duplication is assessed by judges and should also be reviewed by a lawyer. Definitions and introduction are excluded.

Outputs are saved under `artifacts/waymo_benchmark/candidate_outputs/<product>/<model>-<effort>/repeat-1.json` and `repeat-2.json`, with matching `.md` versions for convenient lawyer reading. Each contains the complete product result or a clear failure marker. The detailed `run-<id>.json` also records provider attempts, tokens, latency, and cost. Generation is resumable: rerunning the command skips completed slots and does not overwrite existing outputs.

### 3. Evaluate in two comparison rounds per product

```bash
python -m benchmarks.waymo.pilot --stage evaluate --warn-cost-usd 400 --execute-paid
python -m benchmarks.waymo.pilot --stage report
```

Both blind judges—GPT‑6 Astra/medium and GPT‑5.6 Sol/high—run once for each product and repetition:

1. **Comparison round 1:** the judge sees the four anonymous first outputs together, plus the FAC and the real filed Answer or RFP under a separately labeled **contextual reference** field.
2. **Comparison round 2:** the judge sees the four anonymous second outputs with the same reference and rubric.

The two rounds total 6 four-way groups and 12 judge calls. **Only the four generated outputs receive scores and ranks; the filed document is never a fifth candidate.** For Answer, judges compare how each candidate denial addresses its FAC paragraph, using only clear denials in the filed Answer for context. They ignore filed admissions, insufficient-knowledge, mixed, and qualified responses and do not judge whether `deny` was the best lawyer choice. For RFP, the filed 146-request document informs complaint-inferable issue coverage; it is not a 146-item recall target for the 30-request candidates. Element Checker has no corresponding filed analysis, so its reference is the FAC alone. Candidates' model IDs remain hidden. A common 850K-token judge-input ceiling prevents silent truncation; an overlarge group is flagged instead. Judge errors are recorded by exception class only, so provider diagnostics cannot leak the AEO reference.

The final private reports are `artifacts/waymo_benchmark/report-v2.md` and `report-v2.json`. They include both judges' scores and ranks for each of the two rounds, critical errors, cost, and per-product two-run means and ranges. A separate lawyer may review all 24 preserved outputs and judge disagreements afterward; no lawyer approval gate blocks generation or scoring. The prior `artifacts/waymo_benchmark/review.md` remains unchanged but is not used by this v2 protocol.

### 4. Independently judge saved outputs with local Qwen (optional)

See [QWEN_RUNBOOK.md](QWEN_RUNBOOK.md) for the complete setup and command sequence,
including download, both repetitions for every product, resuming, and reading
the cumulative report.

This route reuses the 24 saved candidate outputs and Astra's candidate order, blind IDs, references, and rubrics. It does **not** regenerate drafts or call OpenAI. The local Ollama build cannot enforce a JSON schema for this model, so the prompt requests JSON and the result is checked against the existing typed judge contract. A failed or incomplete result is marked unscored, never assigned zero.

The judge uses `qwen3.6:35b-a3b-q4_K_M`, the GGUF/llama.cpp route used after the
earlier MLX runner crashed. It writes `qwen-gguf-index-v2.json`,
`qwen-gguf-report-v2.json`, and `qwen-gguf-report-v2.md` under
`artifacts/waymo_benchmark`. Every command updates those cumulative files.
Omitting `--repeat` processes both repetitions sequentially, and completed
comparisons are skipped on resume. The model digest and source fingerprint
prevent mixing results after either changes. Astra's scores appear separately
in the report; scores from different judges are not averaged together.

## Privacy, cost, and limits

The three source PDFs are tracked in `tests/e2e/fixtures`; the existing RFP fixture is marked Attorneys’ Eyes Only, so limit repository access accordingly and do not paste its text into ordinary logs or a PR. Generated working fixtures, judge evidence, and outputs remain Git-ignored; generated directories are mode `0700` and files mode `0600`. The candidate subprocess receives a separate FAC-only fixture that omits the filed Answer and RFP entirely; its requests are also built from that explicit allowlist. Reference text is sent only to the two approved API judges or the optional local-only Qwen judge during a comparison round. Keep Ollama cloud disabled when using the local judge.

The illustrative uncached Standard-tier preflight is **$50.76** for 24 candidate runs and 12 four-way judgments under the stated token assumptions; doubled tokens yield **$187.20**. These exclude paid freezing, retries, cache effects, and other surcharges, and are not caps. The optional `--warn-cost-usd 400` prints a warning when locally estimated known usage reaches $400; **it never stops generation or evaluation**. Omit it for no local cost warning. The old `--max-cost-usd` spelling still works as a deprecated warning-only alias. A call that fails before returning usage or a hard-stopped run may incur unknown cost, and the local estimate excludes the paid freeze step. You may review billed costs in the OpenAI Platform at any time. For a provider-enforced monthly stop, configure a project hard spend limit there; it applies to all API traffic in that project, not just this benchmark, and small overages can occur while it propagates. Both approved judges have a 1.05M-token context window, but the benchmark stops a judge call above its lower common safety ceiling rather than truncating one model's output.

Each candidate runs in an isolated subprocess with the same product-specific hard ceiling for every model: 40 minutes for Answer, 21 minutes for Element Checker, and 31.5 minutes for RFP. This harness does not change production Celery limits. A hard-killed process is marked `cost_unknown`. Prompts, schemas, chunking, and retries are unchanged across candidate configurations.

Rates and API behavior should be rechecked before paid execution: [GPT‑5.5](https://developers.openai.com/api/docs/models/gpt-5.5), [GPT‑6 Sol](https://developers.openai.com/api/docs/models/gpt-6-sol), [GPT‑6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol), [GPT‑6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra), and [GPT‑5.6 Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol). The combined reference-guided, multi-candidate comparison follows [OpenAI evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices), with typed judge results following [Structured Outputs guidance](https://developers.openai.com/api/docs/guides/structured-outputs).

## Offline verification

```bash
pytest -q tests/benchmarks/test_waymo_benchmark.py
pytest -q tests/benchmarks/test_waymo_local_judge.py
```

These mocked tests verify source alignment, OCR completeness, FAC-only candidate inputs, output persistence, 24-run scheduling, the combined blind comparison protocol, two evaluation rounds, scoring, task limits, and local-judge isolation. They do not establish live drafting quality. This is one case; even fully completed scores alone should not trigger a production-wide model change.
