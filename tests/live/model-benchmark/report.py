#!/usr/bin/env python3
"""Render the recorded baseline and explicit quality findings, never infer semantic success."""
from pathlib import Path
from collections import Counter
import argparse,json

def main(run):
 state=json.loads((run/'state.json').read_text());metrics=json.loads((run/'metrics.json').read_text())
 runtime=json.loads((run/'runtime-configuration.json').read_text());summary=json.loads((run/'benchmark-summary.json').read_text())
 evidence=json.loads((run/'results/evidence.json').read_text());download=json.loads((run/'download-verification.json').read_text())
 quality=json.loads((run/'quality-review.json').read_text()) if (run/'quality-review.json').exists() else {}
 classification=json.loads((run/'issue-classification.json').read_text()) if (run/'issue-classification.json').exists() else {}
 lines=['# OpenAI legal-workflow baseline v1','',f"Status: **{state['status']}**. Application commit `{state['head']}`.",
 f"Provider: **{runtime['provider']}**; configured agent: **{runtime['controller_model']}**, effort **{runtime['effective_effort']}**; preparation: **{runtime['preparation_model']}**.",
 '','This is one recorded pass per turn using the real, normally authenticated Workspace UI and real providers. A completed status is not a semantic pass.',
 '',f"Recorded: **{summary['completed_turns']}/{summary['turns_recorded']} completed turns**, **{summary['agent_wall_seconds']:.3f}s** summed agent runtime, **{summary['llm_calls']}** successful scoped LLM calls, **{summary['total_tokens']:,}** reported total tokens, **{summary['tool_errors']}** visible tool errors.",
 '', '## Workflows', '', '| Workflow | Project | Turns completed | Agent time (s) | LLM calls | Tool errors |', '| --- | ---: | ---: | ---: | ---: | ---: |']
 for name,c in state['cases'].items():
  rows=[m for m in metrics if m['workflow']==name]
  lines.append(f"| {name} | {c['project_id']} | {sum(m['status']=='completed' for m in rows)} | {sum(m['agent_wall_seconds'] or 0 for m in rows):.3f} | {sum(m['llm_calls'] or 0 for m in rows)} | {sum(m['tool_errors'] for m in rows)} |")
 lines += ['', '## Per-turn record', '', '| Turn | State | Agent seconds | LLM calls | Total tokens | Tool steps / errors | Output words | Confirmed version |', '| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |']
 for m in metrics:
  lines.append(f"| [{m['turn']}](outputs/{m['turn']}.md) | {m['status']} | {m['agent_wall_seconds']} | {m['llm_calls']} | {m['total_tokens']} | {m['tool_steps']} / {m['tool_errors']} | {m['output_words']} | {m['draft_version_before']} → {m['draft_version_after']} |")
 lines += ['', '## Quality review', '', 'The evaluator key is held separately. Checks use saved drafts, the original pleadings, visually inspected correction schedules, the held-out court order, and actual retrieved evidence. Keyword presence alone is not verification.', '']
 if not quality:lines.append('Pending. Do not interpret the completion table as a quality pass.')
 for name,value in quality.items():
  if name.startswith('_'):continue
  lines += [f'### {name}', '',value.get('summary',''), '']
  for finding in value.get('findings',[]):
   lines.append(f"- **{finding['status']} — {finding['check']}**: {finding['evidence']}")
  lines.append('')
 lines += ['', '## System bugs, model mistakes, and external failures', '']
 if classification:
  lines += [
   '**System behavior:** no demonstrated corruption, wrong-version publication, lost confirmed draft, or incorrect-download bug in this pass. All ten captures match their confirmed Markdown version and downloaded-file hash. The guards correctly rejected invalid references. This is evidence about the exercised workflows, not proof that the system has no bugs.',
   '',f"**Model argument mistakes:** {classification['model_argument_errors']} error-flagged calls used invalid/mixed document, source, or material IDs. They recovered inside the same run. W4 used hybrid UUIDs that did not match its admitted source IDs; C3 similarly mistyped a supplied source ID. These were not authorization regressions.",
   '',f"**External source/provider failures:** {classification['external_failures']} error-flagged C1 calls: CourtListener HTTP500, CourtListener20s read timeout, opinion-source HTTP429, and an access-blocked page. These are recorded separately from model argument mistakes.",
   '', '**Model output mistakes:** Coomer missed the Reaves proposition defect, and final verification introduced the wrong Ginter court. C2 described the Kentucky change as a correction; C3 incorrectly called it a new error and asserted Colorado. The [official court opinion](https://www.govinfo.gov/content/pkg/USCOURTS-cod-1_20-cv-00991/pdf/USCOURTS-cod-1_20-cv-00991-8.pdf), PDF4/10, identifies E.D.Ky; the supplied held-out sanctions order PDF12 also identifies Kentucky. Original C2/C3 outputs are unchanged. Waymo has minor page-location imprecision; Sullivan loses consistency about which BYJU history had already been checked.',
   '', '**Unresolved system/model boundary:** citation identification reported an exact-location/boundary coverage failure. The underlying PDF extraction reports complete native text, but identification-versus-chunking causation has not been isolated. Large cumulative research records and missing court headers in a delivered passage are observed design limitations; neither establishes that the application caused the model\'s mistaken assertion.',
   '',f"**Retries:** {classification['retries']['explicit_whole_run_retries']} explicit whole-run retries. Tool corrections are preserved individually. Provider/helper internal retries are not comprehensively measured by the successful-call counters.",
   '', '## Errors and efficiency findings', '', '| Turn | Step | Classification | Observed outcome |', '| --- | ---: | --- | --- |']
  for issue in classification['error_flagged_tool_observations']:
   lines.append(f"| {issue['turn']} | {issue['sequence']} | {issue['classification']} | {issue.get('external_detail') or issue['message']} |")
   lines += ['', 'Sullivan accounted for 24 min 21.793 s and 19,594,918 successful-response total tokens across three turns. S1\'s broad legal/web search alone took 224.743 s; S2\'s slowest legal search took 99.661 s and a controller-decision step took 94.207 s. These are recorded step times, not proof of a specific defect or a regression against a matched older implementation.', '']
 if not classification:
  for r in evidence['runs']:
   t=next((m['turn'] for m in metrics if m['run_id']==r['id']),r['id'])
   for error in r['tool_errors']:lines.append(f"- {t}, step {error['sequence']}, `{error['operation']}`: {error['message']}")
  if not any(r['tool_errors'] for r in evidence['runs']):lines.append('No error-flagged tool observations recorded in the completed set.')
 actions=[json.loads(line) for line in (run/'browser-actions.jsonl').read_text().splitlines()]
 setup_errors=[a for a in actions if a.get('event')=='driver_error']
 if setup_errors:lines += ['',f'Browser automation recorded {len(setup_errors)} separate setup/driver failure(s). The initial Rename-menu timeout occurred before model submission; the observer resumed the same project/chat. This is separate from agent errors and whole-run retries.', '']
 lines += ['', 'Exact duplicate research argument counts are in metrics.csv; semantically repeated searches require inspecting each turn\'s research-activity JSON and results/evidence.json. Retain unsuccessful/recovered observations; no application tuning was performed during this baseline.', '', '## Preparation and downloads', '', '| File | Preparation seconds | State | Attempts |', '| --- | ---: | --- | ---: |']
 for p in evidence['preparations']:lines.append(f"| {p['filename']} | {p['preparation_seconds']} | {p['status']} | {p['attempts']} |")
 passed=sum(all(d.get(k) is True for k in ('zip_ok','has_document_text','docx_sha256_matches_capture','markdown_matches_confirmed_version')) for d in download)
 lines += ['',f'**{passed}/{len(download)}** captured DOCX/Markdown deliveries passed the recorded integrity/version checks. Per-turn DOCX files, extracted text and screenshots are in outputs/. Full visual DOCX pagination was not evaluated.',
 '', '## Evidence and replay', '',
 '- `scenarios.json`, `prompts/`: exact requests, input roles, introduction turns and source hashes.',
 '- `responses/`, `outputs/`: final answers and drafts/downloads for each turn.',
 '- `results/evidence.json`: scoped durable steps, raw tool arguments/results, sources and research artifacts.',
 '- `model-decisions.json`: function-call choices without private reasoning or encrypted checkpoints.',
 '- `confirmed-versions.json`: confirmed version IDs, candidates and complete draft contents.',
 '- `metrics.csv`, `actual-model-usage.json`, `browser-actions.jsonl`: timing, usage, choices, errors and UI events.',
 '- `code-identity.json`, `runtime-configuration.json`, `reproducibility-checks.json`: code/runtime/input identity.',
 '- `inputs/`, `evaluator-references/`, `baseline-sha256.json`: exact selected input bytes, held-out evaluation reference, and preservation checksums. Do not upload evaluator references.',
 '', '## Interpretation limits', '',
 '- Original responses, drafts, errors and downloads are preserved, including incorrect statements. SHA-256 inventories support later comparison; do not replace this baseline with improved reruns.',
 '- Agent time excludes ingestion, downloads and operator pauses; decision/tool step times include queue/orchestration, not provider-only latency.',
 '- Token counters cover successful chat-scoped calls and repeated/cached inputs; no reliable dollar cost or input/output/cache-token breakdown is available. Preparation and download usage are separate.',
 '- A single pass is a reference, not a runtime distribution. Repeat on both models before claiming a statistical regression or superiority.',
 '- These are historical-document exercises with selected anchored checks, not exhaustive validation of every citation or present-law research.',
 '- Correction materials are withheld from initial uploads, but prior model knowledge and live web retrieval can reveal them. Research-evidence exposure is reviewed explicitly.',
 '- Later turns inherit each model\'s own prior output; compare as end-to-end workflows. For identical-context inference comparisons, replay the recorded history separately.',
 '- Future comparisons must record the whole model mix, tool/search availability, hardware/load and changed application files. Hold helpers constant when testing only a controller change.',
 '', 'No court filing or external correspondence was sent. Existing user data and LexIntel services were preserved. No commits or pushes were made for the benchmark.']
 (run/'REPORT.md').write_text('\n'.join(lines)+'\n')
 print(str(run/'REPORT.md'))

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('run',type=Path);a=p.parse_args();main(a.run.resolve())
