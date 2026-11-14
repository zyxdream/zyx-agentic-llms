"""Stage synthetic inputs for a manually initiated, normally authenticated live run."""
import argparse,json,shutil
from pathlib import Path
root=Path(__file__).resolve().parents[3]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--live',action='store_true',help='Acknowledge real provider calls when starting a case in the browser')
args=parser.parse_args()
if not args.live:parser.error('Use --live to stage the real-provider acceptance page; no model calls are made by staging alone.')
source=Path(__file__).parent;dest=root/'frontend/public/_live_acceptance_legal'
if dest.exists():raise SystemExit('Staging directory already exists. Finish or remove that test staging directory first.')
dest.mkdir()
cfg=json.loads((source/'fixtures/scenario-prompts.json').read_text())
for case in cfg.values():
 for rel in case['project_files']+case['prompt_files']:
  target=dest/rel;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source/'fixtures'/rel,target)
(dest/'scenarios.json').write_text(json.dumps(cfg))
shutil.copy2(source/'browser-runner.js',dest/'runner.js')
(dest/'index.html').write_text('''<!doctype html><meta charset="utf-8"><title>Live synthetic acceptance tests</title><h1>Live acceptance tests</h1><p>Use your normal signed-in Workspace account. Starting a case creates a project and calls real paid providers. The 40-file case uses short synthetic documents, not large PDFs.</p><button data-case="commercial">Start 40-file contract case</button><button data-case="employment">Start payroll case</button><button data-case="research">Start jurisdiction case</button><pre id="status"></pre><script type="module">import {connect,startCase,snapshot} from './runner.js';const out=document.querySelector('#status');try{await connect();for(const button of document.querySelectorAll('button'))button.onclick=()=>{button.disabled=true;startCase(button.dataset.case);};setInterval(async()=>{out.textContent=JSON.stringify(await snapshot(),null,2)},5000);}catch(e){out.textContent=String(e)}</script>''')
print('Staged only synthetic upload lists and prompts; expected.json was not served.')
print('Open http://localhost:3010/_live_acceptance_legal/ and choose a case to start.')
