# Local Qwen judge runbook

Use this runbook to evaluate the saved Waymo benchmark outputs with
`qwen3.6:35b-a3b-q4_K_M` through local Ollama. It covers installation, model
download, the six comparison rounds, and reading the cumulative report.

The initial successful trial used Ollama 0.40.4 on an Apple M1 Max. The GGUF
model tag above selects the llama.cpp runner; the earlier
`qwen3.6:35b-a3b` MLX trial crashed while processing a full Answer prompt.
Keep the exact GGUF tag when following these commands.

## 1. Check the saved benchmark inputs

Run Python commands from the repository root in the `lexintel` Conda environment:

```bash
cd /Users/yuxiao/Documents/lexintel
conda activate lexintel
```

For another checkout, replace the `cd` path with that checkout's root.

The local judge requires these existing, ignored files:

- `artifacts/waymo_benchmark/prepared-v2.json`, containing the prepared FAC and
  frozen preprocessing results.
- `artifacts/waymo_benchmark/index-v2.json`, containing all 24 successful candidate
  outputs and Astra's six completed comparison judgments, including blind IDs.
- The source PDFs referenced by the prepared fixture, with matching file hashes.

The local judge reuses those candidate outputs and Astra's candidate order, blind
IDs, labeled references, and rubrics. It does not generate new product outputs
or call OpenAI. A fresh checkout needs the earlier preparation, freeze,
generation, and API judging stages described in [README.md](README.md), or an
authorized copy of their private artifacts. An OpenAI API key alone does not
replace those saved artifacts.

## 2. Install or update Ollama

Ollama commands do not require the Conda environment. On a Mac with Homebrew,
install Ollama if it is not installed:

```bash
brew install ollama
```

If it is already installed through Homebrew, update it instead:

```bash
brew upgrade ollama
```

Check the client version and available disk space:

```bash
ollama --version
df -h .
```

A warning that Ollama cannot connect to a running instance is expected before
the server starts. The model download is approximately 24 GB; allow additional
space for runtime files and reports.

## 3. Start the server in terminal A

```bash
OLLAMA_NO_CLOUD=1 ollama serve
```

Keep this terminal open. A message such as `Listening on 127.0.0.1:11434` means
the server is ready; waiting there is normal. This terminal also shows model
loading, prompt processing, and runner errors.

Only one Ollama server can listen on that address. If it is already running from
the earlier local-only setup, keep using it. If a different server occupies the
port, stop that instance before starting the command above.

The benchmark supplies `num_ctx` on every request, so the earlier
`OLLAMA_CONTEXT_LENGTH=65536` server setting is unnecessary here. Current
per-product allocations are:

| Product | Requested context tokens | Prompt-processing batch |
|---|---:|---|
| Answer | 262,144 | Not explicitly set |
| RFP | 65,536 | Not explicitly set |
| Element Checker | 262,144 | 256 tokens |

The allocation covers input and generated output, including thinking. These are
existing code settings, not prompt truncation limits added by this runbook.
Element Checker's explicit `num_batch=256` processes smaller batches than the
1,024-token batches observed in the failed Metal run. It changes the processing
batch, while retaining the full prompt, context window, model precision, and
rubric. This may reduce GPU pressure; a completed judgment is still needed to
verify runtime stability.

## 4. Download the GGUF model in terminal B

```bash
cd /Users/yuxiao/Documents/lexintel
ollama pull qwen3.6:35b-a3b-q4_K_M
ollama list
```

The download is needed once. Rerunning `pull` may update the model; the judge
records its digest and refuses to mix a changed model with an existing result
index.

An optional interactive check is:

```bash
ollama run qwen3.6:35b-a3b-q4_K_M
```

At the `>>>` prompt, type a short, nonconfidential question. Type `/bye` to return
to the shell. The interactive chat does not need to stay open for the benchmark;
the server in terminal A does.

Check the server and loaded model when needed:

```bash
curl -sS http://127.0.0.1:11434/api/version
ollama ps
```

`ollama ps` can be empty before a request loads the model, or after it unloads.

## 5. Run Answer, one repetition at a time

In terminal B:

```bash
conda activate lexintel
python -m benchmarks.waymo.local_judge --stage preflight --product answer --repeat 1
```

`preflight` sends the complete assembled comparison prompt with a one-token
output limit. It records Ollama's input-token count, checks the allocated
context, and checks for known signs of truncation. It does not score candidates
and does not guarantee that a full judgment will finish.

Continue when the output says `fits_prompt_only`:

```bash
python -m benchmarks.waymo.local_judge --stage evaluate --product answer --repeat 1
```

`evaluate` lets Qwen generate its full judgment of the four anonymous outputs.
The prompt requests JSON; the returned text must pass the existing typed judge
validation before scores are saved. An invalid JSON result receives one retry.

Successful output includes:

```text
Qwen answer round 1: scored
```

Then run repetition 2:

```bash
python -m benchmarks.waymo.local_judge --stage preflight --product answer --repeat 2
python -m benchmarks.waymo.local_judge --stage evaluate --product answer --repeat 2
```

Wait for each command to finish and confirm the preflight passed before running
its evaluation. The initial GGUF trial measured 237,646 input tokens for Answer
round 1 and 243,423 for round 2; both evaluations subsequently reported `scored`.
Token counts can differ if the saved inputs change.

After both Answer judgments, the report should show `qwen_comparisons: 2`.

## 6. Run both RFP repetitions

```bash
python -m benchmarks.waymo.local_judge --stage preflight --product rfp
```

If both rounds say `fits_prompt_only`, run:

```bash
python -m benchmarks.waymo.local_judge --stage evaluate --product rfp
```

Omitting `--repeat` processes repetitions 1 and 2 sequentially. Each evaluation
compares that repetition's four saved final 30-request lists, with the filed RFP
provided as a labeled contextual reference. After Answer and RFP finish, the
report should show `qwen_comparisons: 4`.

## 7. Run both Element Checker repetitions

```bash
python -m benchmarks.waymo.local_judge --stage preflight --product element_checker
```

If both preflights pass, run:

```bash
python -m benchmarks.waymo.local_judge --stage evaluate --product element_checker
```

Successful completion of all products produces `qwen_comparisons: 6`. That means
six four-way judgments: three products times two repetitions, covering all 24
saved candidate outputs.

### Retry Element Checker after a Metal GPU failure

Stop the server in terminal A with Ctrl+C, then restart it to clear the failed
Metal backend:

```bash
OLLAMA_NO_CLOUD=1 ollama serve
```

In terminal B, repeat round 2's preflight with the smaller processing batch:

```bash
cd /Users/yuxiao/Documents/lexintel
conda activate lexintel
python -m benchmarks.waymo.local_judge --stage preflight --product element_checker --repeat 2
```

The old passing preflight does not record the new batch setting, so it is
reprocessed once. If this preflight reports `fits_prompt_only`, evaluate it:

```bash
python -m benchmarks.waymo.local_judge --stage evaluate --product element_checker --repeat 2
```

With the four Answer/RFP comparisons already saved, a successful judgment raises
the cumulative total to 5/6. A runtime failure stays unscored and is not
automatically retried. Inspect the server log before another runtime adjustment.

The subsequent local retry with `num_batch=256` completed Element Checker round
2, bringing the saved total to 5/6. This establishes one successful full judgment
with the smaller batch; round 1 remains unscored, and broader runtime stability
has not been established.

Round 1's suspected input truncation is a separate issue. Retry its preflight
with the same smaller batch:

```bash
python -m benchmarks.waymo.local_judge --stage preflight --product element_checker --repeat 1
```

Only if that preflight passes, run:

```bash
python -m benchmarks.waymo.local_judge --stage evaluate --product element_checker --repeat 1
```

A smaller processing batch does not increase the context capacity. A suspected
truncation still blocks evaluation; no input is shortened to get a score.

## 8. Read or regenerate the cumulative report

Every preflight or evaluation command updates the same local index and
regenerates both reports:

| File under `artifacts/waymo_benchmark/` | Purpose |
|---|---|
| `qwen-gguf-index-v2.json` | Resume state, model digest, preflights, judgments, token usage, and errors |
| `qwen-gguf-report-v2.md` | Readable tables for all products and repetitions |
| `qwen-gguf-report-v2.json` | Detailed criteria, evidence, scores, ranks, and usage |

To rebuild the reports from saved results without contacting Ollama:

```bash
python -m benchmarks.waymo.local_judge --stage report
```

Open the Markdown report on macOS:

```bash
open artifacts/waymo_benchmark/qwen-gguf-report-v2.md
```

Interpret the tables as follows:

- **Repeat** identifies which saved candidate output was judged. The four
  candidates for repeat 1 are compared together; repeat 2 forms another group.
- **Qwen score** is the weighted rubric score from 0 to 100. **Qwen rank** is
  Qwen's relative ranking within that four-candidate group, with 1 best.
- **Astra score/rank** comes from Astra alone. Neither GPT-5.6 Sol nor Qwen is
  averaged into these columns. The original `report-v2.md` has the earlier
  combined API-judge results.
- Scores are calculated from Qwen's integer criterion ratings; ranks are
  independently supplied by Qwen. All candidates can receive 100 while having
  different ranks, as happened in Element Checker round 2. Inspect the JSON
  evidence and comparison explanation before interpreting such a ranking;
  maximum ratings do not independently establish perfect legal accuracy.
- A dash means the Qwen comparison is unscored. It can be pending, blocked by a
  preflight, or failed; inspect the recorded errors to distinguish them.
- **Two-run model summary** uses only scored rounds. With one scored round,
  `85 [85, 85]` is one observed score, not a completed two-run result. With two
  scored rounds, the mean and minimum/maximum cover both.
- The current **Failures** column counts all unscored rounds, including pending
  ones. An unscored row's **Critical errors: 0** also means no Qwen assessment
  is available yet. Actual evaluation errors appear under **Comparisons needing
  a rerun**; preflight details are in the JSON report.
- Token totals include preflight probes and reported evaluation usage. Input
  processed in both stages is counted twice. The original candidate cost is the
  saved API generation cost; local Qwen calls add no API fee.
- The JSON report records each new preflight's `runtime_options`, including its
  one-token output limit, and each evaluated row's `judge_runtime_options`.
  Runtime settings for older completed judgments are `null` because they were
  not recorded at the time. Those completed scores are retained.

The Astra and earlier MLX Qwen files remain separate. Each command loads the
GGUF index before adding results, so later rounds preserve earlier scores.

## 9. Resume or diagnose a failed command

Rerun the same command after resolving a failure. Completed comparisons and
successful preflights with matching runtime options are skipped; unscored
comparisons can be retried. An unscored round with older or different preflight
options requires a new preflight. Run one benchmark command at a time because
they share the same index file.

Useful checks are:

```bash
python -m benchmarks.waymo.local_judge --help
curl -sS http://127.0.0.1:11434/api/version
ollama list
ollama ps
df -h .
```

| Symptom | Check or action |
|---|---|
| Exact Qwen tag is not installed | Run the `ollama pull` command from step 4. |
| Cannot connect | Check terminal A and the `/api/version` request. |
| `preflight not passed` | Run that product/repetition's preflight and inspect its result; a changed runtime setting also requires a fresh preflight. |
| HTTP 400/500 or runner crash | Read terminal A for the runtime error; verify the GGUF tag and allocated context. |
| Ollama runtime error with HTTP 200 | The response contained an error instead of normal usage data. Inspect terminal A; after a Metal backend failure, follow the restart and retry sequence in step 7. |
| Source results or model changed | Keep the existing index as an archive; investigate the changed fixture, candidate results, blind IDs, or model digest before starting a separate evaluation set. |
| Missing prepared fixture or saved runs | Complete or restore the prerequisite artifacts from step 1. |
| No progress after `judging...` | The nonstreaming request prints its next update when it finishes or fails. Watch terminal A; there is no benchmark wall-clock timeout. |

Check the printed status rather than relying only on the shell exit code: the
program records some evaluation failures and still writes a report. If a long
request is interrupted with Ctrl+C, completed earlier comparisons remain saved;
the interrupted comparison has no completed score. A local Ollama request may
continue after the client disconnects, so check terminal A before retrying.

After finishing, release the loaded model if desired:

```bash
ollama stop qwen3.6:35b-a3b-q4_K_M
```

This retains the downloaded weights. Ctrl+C in terminal A stops the server.

## Source code and verification

The judge is [local_judge.py](local_judge.py); its offline tests are
[test_waymo_local_judge.py](../../tests/benchmarks/test_waymo_local_judge.py).
From the repository root:

```bash
conda activate lexintel
pytest -q tests/benchmarks/test_waymo_benchmark.py tests/benchmarks/test_waymo_local_judge.py
```

The saved RFP reference is marked Attorneys' Eyes Only. Keep Ollama local with
cloud disabled and leave the working fixture, candidate outputs, and Qwen
artifacts in their ignored directory. The judge saves validated ratings and
usage without logging raw prompts, responses, or thinking text.
