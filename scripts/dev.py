"""Start/stop only Workspace's own local processes and data directories."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
RUNTIME=ROOT/'instance/dev'
STATE=RUNTIME/'processes.json'
PUBLIC=json.loads((ROOT/'config/dev-monica.json').read_text())
PYTHON=ROOT/'.venv/bin/python'

def stamp(pid):
    result=subprocess.run(['ps','-p',str(pid),'-o','lstart='],text=True,capture_output=True)
    return result.stdout.strip() if result.returncode==0 else None

def alive(record):return bool(record and stamp(record['pid'])==record['started'])
def read_state():return json.loads(STATE.read_text()) if STATE.exists() else {}
def save_state(value):STATE.write_text(json.dumps(value,indent=2)+'\n')
def open_port(port):
    with socket.socket() as client:
        client.settimeout(.2)
        return client.connect_ex(('127.0.0.1',port))==0

def environment():
    from dotenv import dotenv_values
    env=os.environ.copy()
    inherited_prefixes=[env.get(name) for name in ('CONDA_PREFIX','VIRTUAL_ENV') if env.get(name)]
    host_path=[entry for entry in env.get('PATH','').split(os.pathsep)
        if entry and not any(Path(entry).is_relative_to(prefix) for prefix in inherited_prefixes)]
    # Discard inherited credentials/settings so startup always uses this project copy.
    for key in list(env):
        if key.startswith(('AWS_','COGNITO_','CONDA_','E2E_','REACT_APP_','OPENAI_','CELERY_','AGENT_PYTHON_','FLASK_','WORKSPACE_','ZYX_','MODEL_','DOCUMENT_','TIKTOKEN_')) or key in {
            'OPENAI_API_KEY','FREE_LAW_API_TOKEN','REDIS_URL','DATABASE_URL','JWT_SECRET_KEY','FLASK_SECRET_KEY','PYTHONPATH',
            'VIRTUAL_ENV','PORT','HOST','CELERY_QUEUE','WORKSPACE_DATABASE_URL','WORKSPACE_CELERY_QUEUE',
            'PYTHONHOME','PYTHONUSERBASE','DYLD_LIBRARY_PATH','DYLD_FALLBACK_LIBRARY_PATH','LD_LIBRARY_PATH'}:env.pop(key,None)
    env.update({key:value for key,value in dotenv_values(ROOT/'.env').items() if value is not None})
    env.update(ENVIRONMENT='dev-monica',ZYX_E2E='0',PYTHONPATH=str(ROOT),VIRTUAL_ENV=str(ROOT/'.venv'),
        PATH=os.pathsep.join([str(ROOT/'.venv/bin'),*host_path]),PYTHONUNBUFFERED='1',PYTHONNOUSERSITE='1',
        AWS_EC2_METADATA_DISABLED='true',AWS_SHARED_CREDENTIALS_FILE=str(ROOT/'.private/aws/credentials'),
        AWS_CONFIG_FILE=str(ROOT/'.private/aws/config'),AWS_PROFILE='workspace',
        # macOS Objective-C libraries can abort forked Celery children; this is
        # the documented local-development workaround for prefork workers.
        OBJC_DISABLE_INITIALIZE_FORK_SAFETY='YES',
        TIKTOKEN_CACHE_DIR=str(ROOT/'instance/cache/tiktoken'))
    return env

def wait_port(name,record,port,timeout=45):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if not alive(record):raise RuntimeError(f'{name} exited. See {RUNTIME/name}.log')
        if open_port(port):return
        time.sleep(.2)
    raise RuntimeError(f'{name} did not listen on port {port}. See {RUNTIME/name}.log')

def launch(name,command,env,state):
    with (RUNTIME/(name+'.log')).open('a') as output:
        process=subprocess.Popen(command,cwd=ROOT,env=env,stdin=subprocess.DEVNULL,
            stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
    record={'pid':process.pid,'started':stamp(process.pid),'command':command}
    if not record['started']:raise RuntimeError(f'{name} could not start')
    state[name]=record;save_state(state)
    return record

def start():
    RUNTIME.mkdir(parents=True,exist_ok=True)
    state=read_state()
    if any(alive(record) for record in state.values()):
        raise RuntimeError('Workspace processes are already running. Use status; stop before restarting.')
    state={};save_state(state)
    for port in [PUBLIC['frontendPort'],PUBLIC['backendPort'],PUBLIC['redisPort']]:
        if open_port(port):raise RuntimeError(f'Port {port} belongs to another process. It has been left untouched.')
    if not PYTHON.is_file():raise RuntimeError('Create the project .venv before starting Workspace')
    env=environment()
    queue=env.get('ZYX_CELERY_QUEUE','zyx-agentic-llms.dev')
    # Migrate only this application's configured, project-local database.
    subprocess.run([str(PYTHON),'-m','flask','--app','backend.app:create_app','db','upgrade'],cwd=ROOT,env=env,check=True)
    redis=shutil.which('redis-server',path=env['PATH']);npm=shutil.which('npm',path=env['PATH'])
    if not redis or not npm:raise RuntimeError('Install redis-server and Node/npm on the host')
    redis_dir=ROOT/'instance/redis';redis_dir.mkdir(exist_ok=True)
    logs=ROOT/'instance/workspace/logs';logs.mkdir(parents=True,exist_ok=True)
    try:
        record=launch('redis',[redis,'--bind','127.0.0.1','--port',str(PUBLIC['redisPort']),
            '--protected-mode','yes','--appendonly','yes','--dir',str(redis_dir)],env,state)
        wait_port('redis',record,PUBLIC['redisPort'],10)
        record=launch('backend',[str(PYTHON),str(ROOT/'scripts/serve_backend.py')],env,state)
        wait_port('backend',record,PUBLIC['backendPort'],20)
        launch('agent',[str(PYTHON),'-m','celery','--app','backend.make_celery:celery_app','worker',
            '--hostname','zyx-workspace-agent@%h','--queues',queue,'--concurrency','2',
            '--prefetch-multiplier','1','--loglevel','INFO'],env,state)
        launch('documents',[str(PYTHON),'-m','backend.document_preparation_worker',
            '--hostname','zyx-workspace-documents@%h','--loglevel','INFO'],env,state)
        launch('beat',[str(PYTHON),'-m','celery','--app','backend.make_celery:celery_app','beat',
            '--schedule',str(RUNTIME/'celerybeat-schedule'),'--pidfile',str(RUNTIME/'celerybeat.pid'),'--loglevel','INFO'],env,state)
        record=launch('frontend',[npm,'--prefix',str(ROOT/'frontend'),'start'],env,state)
        wait_port('frontend',record,PUBLIC['frontendPort'],90)
        time.sleep(1)
        if not all(alive(record) for record in state.values()):raise RuntimeError('A Workspace worker exited. Check project-local logs.')
    except Exception:
        stop();raise
    status()

def stop():
    state=read_state()
    for name in ['frontend','beat','documents','agent','backend','redis']:
        record=state.get(name)
        if not alive(record):continue
        # Each process was started in its own group. The start-time check prevents PID reuse.
        try:os.killpg(record['pid'],signal.SIGTERM)
        except ProcessLookupError:continue
    deadline=time.monotonic()+20
    while time.monotonic()<deadline and any(alive(record) for record in state.values()):time.sleep(.1)
    for name,record in state.items():
        if alive(record):
            try:os.killpg(record['pid'],signal.SIGKILL)
            except ProcessLookupError:pass
    if STATE.exists():save_state({})
    print('Stopped only the recorded Workspace processes.')

def status():
    state=read_state()
    print('Workspace frontend:',PUBLIC['appUrl'])
    print('Workspace backend:',PUBLIC['apiOrigin'])
    print('Workspace Redis: 127.0.0.1:'+str(PUBLIC['redisPort']))
    for name,record in state.items():print(name,'running' if alive(record) else 'stopped')
    print('Database:',ROOT/'instance/zyx-agentic-llms.db')

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['start','stop','status'])
    args=parser.parse_args()
    try:{'start':start,'stop':stop,'status':status}[args.action]()
    except Exception as exc:raise SystemExit(str(exc))
