#!/usr/bin/env python3
"""Freeze a fresh legal-workflow comparison run without making provider calls."""
from pathlib import Path
from datetime import datetime,timezone
import argparse,hashlib,importlib.metadata,json,os,platform,re,shutil,subprocess,sys

ROOT=Path(__file__).resolve().parents[3]
PACK=Path(__file__).resolve().parent

def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--label',default='openai');p.add_argument('--cdp-port',type=int,default=64954)
 p.add_argument('--validate-only',action='store_true');a=p.parse_args()
 cfg=json.loads((PACK/'scenarios.json').read_text())
 for item in cfg['files'].values():
  path=(ROOT/item['path']).resolve()
  if not path.is_relative_to(ROOT/'instance/evaluation-inputs') or digest(path)!=item['sha256']:
   raise ValueError('Private benchmark input is missing or changed: '+item['filename'])
 for w in cfg['workflows']:
  for t in w['turns']:
   if digest(PACK/t['prompt_file'])!=t['prompt_sha256']:raise ValueError('Prompt changed: '+t['id'])
 if a.validate_only:
  print(json.dumps({'valid':True,'files':len(cfg['files']),'turns':sum(len(w['turns']) for w in cfg['workflows'])}));return
 if not re.fullmatch(r'[a-zA-Z0-9_-]+',a.label) or not 1024<=a.cdp_port<=65535:p.error('Use a plain label and valid local CDP port.')
 out=ROOT/'artifacts'/('model-benchmark-'+a.label+'-'+datetime.now().strftime('%Y%m%d-%H%M%S'));out.mkdir(parents=True)
 for name in ('scenarios.json','reviewer-checks.json'):shutil.copyfile(PACK/name,out/name)
 shutil.copytree(PACK/'prompts',out/'prompts')
 names=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard'],cwd=ROOT,text=True).splitlines()
 hashes={n:digest(ROOT/n) for n in names if n.startswith(('backend/','frontend/src/','config/','scripts/')) and (ROOT/n).is_file()}
 head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
 identity={'head':head,'application_source_sha256':hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest(),'files':hashes,'python':sys.version,'platform':platform.platform(),'packages':{d.metadata['Name']:d.version for d in importlib.metadata.distributions() if d.metadata['Name']},'runner_hashes':{str(p.relative_to(PACK)):digest(p) for p in PACK.rglob('*') if p.is_file() and '__pycache__' not in p.parts}}
 (out/'code-identity.json').write_text(json.dumps(identity,indent=2)+'\n')
 (out/'state.json').write_text(json.dumps({'created_at':datetime.now(timezone.utc).isoformat(),'status':'prepared','benchmark_version':cfg['benchmark_version'],'head':head,'cases':{},'turns':[]},indent=2)+'\n')
 (out/'controlled-browser.json').write_text(json.dumps({'port':a.cdp_port})+'\n')
 sys.path.insert(0,str(ROOT))
 from scripts import dev
 os.environ.update(dev.environment())
 from backend.ai import model_policy,base
 from backend.util.preparation_summary import MODEL
 mode=os.getenv('WORKSPACE_DEFAULT_MODE','fast')
 runtime={'provider':os.getenv('MODEL_PROVIDER','openai'),'new_run_policy':mode,'controller_model':model_policy._assistant_model(mode),'configured_effort':model_policy._output_effort(mode),'effective_effort':base._effective_reasoning_effort(model_policy._assistant_model(mode),model_policy._output_effort(mode)),'preparation_model':MODEL,'base_default_model':base.DEFAULT_MODEL,'service_records':dev.read_state(),'hardware':{'logical_cpus':os.cpu_count()}}
 (out/'runtime-configuration.json').write_text(json.dumps(runtime,indent=2)+'\n')
 print(str(out))

if __name__=='__main__':main()
