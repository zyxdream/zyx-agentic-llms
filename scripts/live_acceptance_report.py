#!/usr/bin/env python3
"""Collect explicitly scoped live acceptance evidence without changing SQLite.

Example:
  .venv/bin/python scripts/live_acceptance_report.py --project-ids 2,3,4

This is evidence collection, not a semantic legal-quality verdict. Only IDs
explicitly supplied by the caller are exported. Authentication/account tables,
model checkpoints and raw browser state are never exported.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = ROOT / 'instance/zyx-agentic-llms.db'
DEFAULT_OUTPUT = ROOT / 'artifacts/live-legal-20261113/results'
SECRET_KEYS = {'password', 'password_hash', 'secret', 'secret_key', 'api_key',
               'access_token', 'refresh_token', 'csrf', 'csrf_token', 'cookie',
               'authorization', 'encrypted_content'}


def decoded(value, fallback=None):
    if value is None:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def sanitized(value):
    if isinstance(value, dict):
        return {key: sanitized(item) for key, item in value.items()
                if key.lower() not in SECRET_KEYS}
    if isinstance(value, list):
        return [sanitized(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r'\bsk-[A-Za-z0-9_-]{15,}', '[REDACTED]', value)
        return re.sub(r'(?i)([?&](?:api_key|access_token|token)=)[^&\s]+', r'\1[REDACTED]', value)
    return value


def seconds(start, end):
    try:
        return round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 3)
    except (TypeError, ValueError):
        return None


def rows(connection, statement, params=()):
    return [dict(row) for row in connection.execute(statement, params)]


def write_json(path, value):
    path.write_text(json.dumps(sanitized(value), indent=2, ensure_ascii=False, default=str) + '\n')


def collect(database, project_ids, run_ids, output, state_file=None):
    output.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    connection.execute('BEGIN')
    marks = ','.join('?' for _ in project_ids)
    projects = rows(connection, f'SELECT id,name,date FROM projects WHERE id IN ({marks}) ORDER BY id', project_ids)
    if len(projects) != len(set(project_ids)):
        raise ValueError('At least one explicitly selected project does not exist.')
    conversations = rows(connection, f'SELECT id,public_id,projectId,display_name,date FROM chat_threads WHERE projectId IN ({marks}) ORDER BY id', project_ids)
    preparations = rows(connection, f'''SELECT p.id,p.filename,p.file_size,p.scope_key,p.status,p.checkpoint,
        p.attempts,p.error_code,p.error_message,p.warning,p.created_at,p.parsed_at,p.indexed_at,p.ready_at,
        p.updated_at,p.first_attached_at,p.artifact_id,g.project_id,g.chat_thread_id
        FROM document_preparations p JOIN document_upload_groups g ON g.id=p.group_id
        WHERE g.project_id IN ({marks}) ORDER BY p.created_at,p.id''', project_ids)
    for item in preparations:
        item['preparation_seconds'] = seconds(item['created_at'], item['ready_at'])
    runs = rows(connection, f'''SELECT r.* FROM workspace_runs r JOIN chat_threads c ON c.id=r.chat_thread_id
        WHERE c.projectId IN ({marks}) ORDER BY r.created_at,r.id''', project_ids)
    if run_ids:
        requested = set(run_ids)
        found = {run['id'] for run in runs}
        if requested - found:
            raise ValueError('A requested run does not belong to the explicitly selected projects.')
        runs = [run for run in runs if run['id'] in requested]
    documents = rows(connection, f'''SELECT d.id,d.public_id,d.chat_thread_id,d.draft_version,d.updated_at,
        d.draft_json,d.draft_before_json FROM draft_documents d JOIN chat_threads c ON c.id=d.chat_thread_id
        WHERE c.projectId IN ({marks})''', project_ids)
    for document in documents:
        document['current'] = decoded(document.pop('draft_json'), {})
        document['previous'] = decoded(document.pop('draft_before_json'), {})
    usage = rows(connection, f'''SELECT u.chat_thread_id,u.promptsSent,u.answersReturned,u.llmCallCount,
        u.totalTokens,u.downloadAttempts,u.downloadSuccesses,u.downloadFailures,u.downloadLlmCallCount,
        u.downloadTokens FROM workspace_usage u JOIN chat_threads c ON c.id=u.chat_thread_id
        WHERE c.projectId IN ({marks}) ORDER BY u.chat_thread_id''', project_ids)
    report_runs = []
    for run in runs:
        rid = run['id']
        steps = rows(connection, '''SELECT id,sequence,kind,name,state,arguments_json,result_json,summary_json,
            created_at,updated_at FROM agent_steps WHERE run_id=? ORDER BY sequence''', (rid,))
        errors = []
        for step in steps:
            step['elapsed_seconds'] = seconds(step['created_at'], step['updated_at'])
            if step['kind'] == 'tool':
                for key in ('arguments', 'result', 'summary'):
                    step[key] = decoded(step.pop(key + '_json'), {})
                result = step.get('result')
                if isinstance(result, dict) and result.get('error'):
                    errors.append({'sequence':step['sequence'], 'operation':step['name'],
                                   'message':result.get('message') or result.get('error')})
            else:
                for key in ('arguments_json', 'result_json', 'summary_json'):
                    step.pop(key, None)
        artifacts = rows(connection, 'SELECT id,step_id,kind,content_json,created_at FROM agent_artifacts WHERE run_id=? ORDER BY created_at,id', (rid,))
        for artifact in artifacts:
            artifact['content'] = decoded(artifact.pop('content_json'), {})
        data = decoded(run['data_json'], {})
        request = {key:data[key] for key in ('query','assistant_mode','model','selected_model','document_snapshot','excludedDocumentIds','promptAttachments','chatFiles') if key in data}
        snapshot = request.get('document_snapshot') or {}
        summary = {'id':rid,'chat_thread_id':run['chat_thread_id'],'status':run['status'],
            'created_at':run['created_at'],'updated_at':run['updated_at'],
            'wall_seconds':seconds(run['created_at'],run['updated_at']),
            'wall_seconds_note':'For active requests this is elapsed to last persisted update, not completion latency.',
            'attempts':run['attempts'],'error_message':run['error_message'],
            'base_draft_version':run['base_draft_version'], 'saved_policy':request.get('assistant_mode'),
            'snapshot_file_count':len(snapshot.get('files',[])),
            'snapshot_scope_counts':dict(Counter(item.get('scope','unknown') for item in snapshot.get('files',[]))),
            'step_count':len(steps),'tool_counts':dict(Counter(step['name'] for step in steps if step['kind']=='tool')),
            'step_state_counts':dict(Counter(step['state'] for step in steps)), 'tool_error_count':len(errors),
            'tool_errors':errors,'artifact_kind_counts':dict(Counter(artifact['kind'] for artifact in artifacts)),
            'request':request,'result':decoded(run['result_json'],{}),'steps':steps,'artifacts':artifacts}
        report_runs.append(summary)
    connection.close()
    state_metrics = {}
    if state_file and state_file.exists():
        state = json.loads(state_file.read_text())
        # Never export the full client state: it may contain session material.
        if isinstance(state, dict):
            for key in ('timings','metrics','events','started_at','finished_at','download_results','version_checks'):
                if key in state:
                    state_metrics[key] = sanitized(state[key])
    report = {'collected_at':datetime.now(timezone.utc).isoformat(), 'database_read_only':True,
              'project_ids':project_ids,'projects':projects,'conversations':conversations,
              'preparations':preparations,'preparation_status_counts':dict(Counter(p['status'] for p in preparations)),
              'runs':report_runs,'documents':documents,'usage_by_conversation':usage,'client_metrics':state_metrics,
              'limitations':['No semantic pass/fail is inferred from a completed run.',
                  'Usage rows are conversation aggregates, including prior prompts and any concurrent work.',
                  'Run/step timestamps include orchestration and waiting; they are not provider-only latency.',
                  'Current and previous drafts do not themselves prove the full immutable revision chain. Inspect saved artifacts.']}
    write_json(output / 'evidence.json', report)
    lines = ['# Live legal acceptance evidence','',f'Collected: {report["collected_at"]}',
             '',f'Projects: {", ".join(str(p) for p in project_ids)}',
             f'Preparation states: {report["preparation_status_counts"]}',
             '', '| Run | State | Elapsed to last update (s) | Files | Tool steps | Tool errors |',
             '| --- | --- | ---: | ---: | ---: | ---: |']
    for run in report_runs:
        lines.append(f'| {run["id"]} | {run["status"]} | {run["wall_seconds"]} | {run["snapshot_file_count"]} | {sum(run["tool_counts"].values())} | {run["tool_error_count"]} |')
        filename = re.sub(r'[^A-Za-z0-9_.-]', '_', run['id'])
        write_json(output / f'{filename}.json', run)
        result = run['result'] if isinstance(run['result'], dict) else {}
        body = result.get('response') or result.get('error_message') or '(No completed response yet.)'
        (output / f'{filename}.md').write_text(f'# {run["id"]}\n\nStatus: {run["status"]}\n\n{body}\n')
    lines += ['', '## Usage by conversation', '', 'These totals are cumulative, not per-run billing records.', '']
    for item in usage:
        lines.append(f'- Conversation {item["chat_thread_id"]}: {item["llmCallCount"]} model calls, {item["totalTokens"]} tokens; {item["downloadSuccesses"]} successful downloads.')
    lines += ['', '## Interpretation limits', ''] + [f'- {item}' for item in report['limitations']]
    (output / 'summary.md').write_text('\n'.join(lines) + '\n')
    return {'projects':len(projects),'conversations':len(conversations),'runs':len(report_runs),
            'preparations':len(preparations),'output':str(output)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-ids', required=True, help='Explicit comma-separated project IDs to export.')
    parser.add_argument('--run-ids', default='', help='Optional comma-separated subset of runs in selected projects.')
    parser.add_argument('--database', type=Path, default=DEFAULT_DATABASE)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--state', type=Path)
    args = parser.parse_args()
    project_ids = list(dict.fromkeys(int(value) for value in args.project_ids.split(',') if value.strip()))
    if not project_ids:
        parser.error('At least one project ID is required.')
    print(json.dumps(collect(args.database,project_ids,[v for v in args.run_ids.split(',') if v],args.output,args.state)))


if __name__ == '__main__':
    main()
