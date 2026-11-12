"""Compare matching deterministic agent workflows with an explicitly chosen Git baseline.

No paid providers, production services or original project checkout are used.
"""
import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time

CASES=[
 'test_result_driven_sequence_preserves_reasoning_and_publishes_once',
 'test_question_survives_reload_and_resumes_same_call',
 'test_retry_reuses_completed_results_and_reruns_only_interrupted_decision',
 'test_cancel_records_unfinished_step_state_and_returns_actual_outcome',
 'test_progress_and_artifact_route_omit_model_reasoning_and_enforce_owner',
 'test_publication_marker_recovers_worker_loss_without_duplicate_answer',
 'test_stale_worker_cannot_save_a_late_operation_result',
 'test_controller_has_no_fifty_call_cutoff',
]

def main():
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--baseline',required=True,help='Commit/ref containing the pre-refactor mocked controller tests')
 parser.add_argument('--repeat',type=int,default=5)
 parser.add_argument('--output',default='artifacts/workspace-replay/performance')
 args=parser.parse_args()
 if args.repeat<3:parser.error('Use at least three paired repetitions')
 root=Path(__file__).resolve().parents[2]
 output=(root/args.output).resolve();output.mkdir(parents=True,exist_ok=True)
 commit=subprocess.check_output(['git','rev-parse',args.baseline],cwd=root,text=True).strip()
 samples=[]
 with tempfile.TemporaryDirectory(prefix='workspace-replay-baseline-') as temporary:
  temporary=Path(temporary).resolve();baseline=temporary/'tree';baseline.mkdir();archive=temporary/'baseline.tar'
  with archive.open('wb') as stream:subprocess.run(['git','archive',commit],cwd=root,stdout=stream,check=True)
  with tarfile.open(archive) as bundle:
   members=bundle.getmembers()
   if any(not (baseline/member.name).resolve().is_relative_to(baseline) or member.issym() or member.islnk() for member in members):raise ValueError('Baseline contains unsupported archive paths/links')
   bundle.extractall(baseline,members=members)
  for repetition in range(args.repeat):
   pair=[('before',baseline),('after',root)]
   if repetition%2:pair.reverse()
   for label,folder in pair:
    metrics=output/f'{label}-{repetition}.json'
    env=os.environ.copy();env.update(LEXINTEL_E2E='1',ZYX_E2E='1',OPENAI_API_KEY='offline-replay-key',
      AWS_EC2_METADATA_DISABLED='true',REPLAY_METRICS_OUTPUT=str(metrics),PYTHONPATH=str(Path(__file__).parent))
    command=[sys.executable,'-m','pytest','-q','-p','tests.offline_guard','-p','replay_perf_plugin',
      *[f'tests/test_agent_controller.py::{case}' for case in CASES],'--tb=short']
    start=time.perf_counter()
    with (output/f'{label}-{repetition}.log').open('w') as stream:result=subprocess.run(command,cwd=folder,env=env,stdout=stream,stderr=subprocess.STDOUT)
    wall=time.perf_counter()-start
    if result.returncode:raise RuntimeError(f'{label} replay failed; inspect {output}/{label}-{repetition}.log')
    data=json.loads(metrics.read_text())
    samples.append(dict(version=label,repetition=repetition,wall_seconds=wall,
      call_seconds=sum(row['duration'] for row in data['rows'] if row['phase']=='call'),
      tests=sum(row['phase']=='call' for row in data['rows'])))
    print(f'{label} {repetition+1}/{args.repeat}: {wall:.3f}s',flush=True)
 summary={'baseline_commit':commit,'python':sys.version,'cases':CASES,'samples':samples,
    'scope':'Local deterministic controller replays. Excludes model/network latency and production load.'}
 summary['median']={label:{key:statistics.median(row[key] for row in samples if row['version']==label)
    for key in ['wall_seconds','call_seconds']} for label in ['before','after']}
 summary['change_percent']={key:100*(summary['median']['after'][key]/summary['median']['before'][key]-1)
    for key in ['wall_seconds','call_seconds']}
 (output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
 print(json.dumps(summary['change_percent'],indent=2))

if __name__=='__main__':main()
