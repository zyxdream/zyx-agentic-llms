#!/usr/bin/env python3
"""Summarize this run's captured evidence; semantic findings require manual review."""
from pathlib import Path
import argparse, json


def load(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def cell(value):
    return str(value if value is not None else '—').replace('|', '\\|').replace('\n', ' ')


def main(run):
    if list(run.glob('*-sha256.json')):
        raise ValueError('This run is frozen. Preserve its existing report and results.')
    state = load(run/'state.json', {})
    cfg = load(run/'scenarios.json', {})
    metrics = load(run/'metrics.json', [])
    runtime = load(run/'runtime-configuration.json', {})
    summary = load(run/'benchmark-summary.json', {})
    evidence = load(run/'results/evidence.json', {})
    downloads = load(run/'download-verification.json', [])
    quality = load(run/'quality-review.json', {})
    version = cfg.get('benchmark_version', state.get('benchmark_version', 'unknown'))
    lines = [f'# Legal workflow results — {version}', '',
        f"Status: **{state.get('status','unknown')}**. Application commit `{state.get('head','unknown')}`.",
        f"Provider: {runtime.get('provider','unknown')}; controller: {runtime.get('controller_model','unknown')}; effort: {runtime.get('effective_effort','unknown')}; preparation: {runtime.get('preparation_model','unknown')}.", '',
        'Execution completion, download integrity and substantive quality are separate. This report uses only this run’s saved evidence and manual review; it does not inherit findings from another benchmark.', '',
        f"Recorded: {summary.get('completed_turns',0)}/{summary.get('turns_recorded',len(metrics))} completed turns; {summary.get('agent_wall_seconds','unknown')} summed agent seconds; {summary.get('llm_calls','unknown')} recorded LLM calls; {summary.get('total_tokens','unknown')} reported tokens; {summary.get('tool_errors','unknown')} tool errors.", '',
        '## Workflows', '',
        '| Workflow | Project | Completed turns | Agent seconds | LLM calls | Tool errors | Whole-run retries |',
        '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for name, case in state.get('cases', {}).items():
        rows = [m for m in metrics if m['workflow'] == name]
        totals = [round(sum(m.get(key) or 0 for m in rows), 3) for key in
                  ('agent_wall_seconds','llm_calls','tool_errors','explicit_run_retries')]
        lines.append('| ' + ' | '.join(map(cell, [name, case.get('project_id'),
            sum(m['status']=='completed' for m in rows), *totals])) + ' |')
    lines += ['', '## Per-turn results', '',
        '| Turn | State | Agent seconds | LLM calls | Tokens | Tool errors | Output words | Confirmed version |',
        '| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |']
    for m in metrics:
        lines.append('| ' + ' | '.join(map(cell, [f"[{m['turn']}](outputs/{m['turn']}.md)",
            m['status'], m.get('agent_wall_seconds'), m.get('llm_calls'), m.get('total_tokens'),
            m.get('tool_errors'), m.get('output_words'),
            f"{m.get('draft_version_before')} → {m.get('draft_version_after')}"])) + ' |')
    lines += ['', '## Quality review', '',
        'Evaluate original findings, follow-up resolution, revised text and final verification separately. Use inspected source passages, not completion status or keyword presence. Evaluator references are selected checks; a difference from counsel’s wording requires checking the underlying authority.', '']
    workflows = quality.get('workflows', quality)
    reviewed = False
    for name, review in workflows.items():
        if name.startswith('_') or not isinstance(review, dict):
            continue
        reviewed = True
        lines += [f'### {name}', '', f"Status: {review.get('status','see recorded findings')}.",
                  review.get('summary',''), '']
        checks = review.get('checks', {})
        if isinstance(checks, dict):
            for check, value in checks.items():
                if isinstance(value, dict):
                    lines.append(f"- **{value.get('status','not reviewed')} — {check}:** {value.get('evidence','No evidence recorded.')}")
        for finding in review.get('findings', []):
            lines.append(f"- **{finding.get('status','not reviewed')} — {finding.get('check','finding')}:** {finding.get('evidence','No evidence recorded.')}")
        lines.append('')
    if not reviewed:
        lines += ['**Pending manual review.** No substantive pass/fail is inferred.', '']
    lines += ['## Errors, retries and research exposure', '',
        'The observations below are not automatically classified as system bugs or model mistakes. Inspect the supplied arguments, available evidence, provider response and subsequent recovery. Output defects can occur without a tool error.', '']
    found_error = False
    turns = {m['run_id']:m['turn'] for m in metrics}
    for r in evidence.get('runs', []):
        for error in r.get('tool_errors', []):
            found_error = True
            lines.append(f"- {turns.get(r['id'],r['id'])}, step {error['sequence']}, `{error['operation']}`: {error['message']}")
    if not found_error:
        lines.append('No error-flagged tool observations recorded in the captured set.')
    actions = [json.loads(line) for line in (run/'browser-actions.jsonl').read_text().splitlines() if line.strip()] if (run/'browser-actions.jsonl').exists() else []
    driver_errors = [x for x in actions if x.get('event')=='driver_error']
    lines += ['', f'Browser setup/driver failures recorded: {len(driver_errors)}. Inspect their events separately from agent errors.', '',
        '**Independent discovery needs an evidence check:** inspect saved search summaries and source reads for public correction letters, sanctions orders or other answer-revealing material. If present, record the turn/source and affected findings; do not score them as independent discovery. Exposure is not automatically inferred or ruled out by this report.', '',
        '## Preparation and downloads', '',
        '| File | Preparation seconds | State | Attempts |', '| --- | ---: | --- | ---: |']
    for p in evidence.get('preparations', []):
        lines.append('| ' + ' | '.join(map(cell,[p['filename'],p.get('preparation_seconds'),p['status'],p.get('attempts')])) + ' |')
    passed = sum(all(d.get(k) is True for k in ('zip_ok','has_document_text','docx_sha256_matches_capture','markdown_matches_confirmed_version')) for d in downloads)
    lines += ['', f'{passed}/{len(downloads)} captured deliveries passed the recorded DOCX integrity/hash and confirmed-Markdown checks. This does not verify every sentence or the complete visual layout.', '',
        '## Evidence and comparison limits', '',
        '- Exact prompts and allowed uploads: scenarios.json and prompts/. Captured answers/drafts/downloads: responses/ and outputs/.',
        '- Durable observations and sources: results/evidence.json. Tool choices without private reasoning: model-decisions.json. Confirmed draft history: confirmed-versions.json.',
        '- Per-turn timing/usage/errors: metrics.json and metrics.csv. Source/code/runtime identity: code-identity.json and runtime-configuration.json.',
        '- Compare the same pack version, input hashes, application source, model mix and tool availability. Version 2 is not directly comparable with the different version 1 questions and uploads.',
        '- Keep earlier frozen results unchanged. Fresh projects and conversations are required; never carry earlier answer-revealing uploads into a new version.',
        '- A single pass does not establish a performance distribution. Agent time excludes uploads/preparation, downloads and user idle time; step times include orchestration.',
        '- Token counters include repeated context and are not independently reconciled billing figures. Live sources and network conditions can change.',
        '- An explicit unresolved status is honest disclosure, not completed substantive verification. A failed search does not establish that a case is invalid.', '']
    (run/'REPORT.md').write_text('\n'.join(lines)+'\n')
    print(str(run/'REPORT.md'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    main(parser.parse_args().run.resolve())
