#!/usr/bin/env python3
"""Freeze a live suite run directory without making provider calls."""
from pathlib import Path
from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
LIVE = Path(__file__).resolve().parent


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('suite', help='Suite directory, e.g. tests/live/excel-damages')
    parser.add_argument('--label', default='openai')
    parser.add_argument('--cdp-port', type=int, default=64954)
    args = parser.parse_args()
    pack = (ROOT / args.suite).resolve()
    cfg = json.loads((pack / 'scenarios.json').read_text())
    for project in cfg['projects']:
        for name in project['documents']:
            source = (ROOT / cfg['input_root'] / name).resolve()
            if not source.is_file():
                raise SystemExit('Missing input: ' + str(source))
        for turn in project['turns']:
            prompt = (pack / turn['prompt_file']).resolve()
            if not prompt.is_file() or digest(prompt) != turn['prompt_sha256']:
                raise SystemExit('Prompt changed or missing: ' + str(prompt))
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    out = ROOT / 'artifacts' / (cfg['suite'] + '-' + args.label + '-' + stamp)
    out.mkdir(parents=True)
    shutil.copyfile(pack / 'scenarios.json', out / 'scenarios.json')
    shutil.copytree(pack / 'prompts', out / 'prompts')
    if (pack / 'documents.json').is_file():
        shutil.copyfile(pack / 'documents.json', out / 'documents.json')
    if (pack / 'reviewer-checks.json').is_file():
        shutil.copyfile(pack / 'reviewer-checks.json', out / 'reviewer-checks.json')
    (out / 'controlled-browser.json').write_text(json.dumps({'port': args.cdp_port}, indent=2) + '\n')
    (out / 'state.json').write_text(json.dumps({
        'created_at': datetime.now(timezone.utc).isoformat(),
        'status': 'prepared',
        'suite': cfg['suite'],
        'projects': {},
        'turns': [],
    }, indent=2) + '\n')
    sys.path.insert(0, str(ROOT))
    from scripts import dev
    os.environ.update(dev.environment())
    from backend.ai import model_settings
    runtime = {
        'suite': cfg['suite'],
        'provider': os.getenv('MODEL_PROVIDER', 'openai'),
        'model_settings': model_settings.load_model_settings(),
        'service_records': dev.read_state(),
    }
    (out / 'runtime-configuration.json').write_text(json.dumps(runtime, indent=2) + '\n')
    print(out)


if __name__ == '__main__':
    main()
