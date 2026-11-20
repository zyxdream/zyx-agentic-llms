"""Read-only status for a live suite run directory.

The runner stores project/chat identity in state.json. This helper reads only
those projects and their saved runs; it never reads account/session rows.
"""
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
import json
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]


def snapshot(run):
    state = json.loads((run / 'state.json').read_text())
    connection = sqlite3.connect((ROOT / 'instance/zyx-agentic-llms.db').as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    connection.execute('BEGIN')
    out = {'at': datetime.now(timezone.utc).isoformat(), 'projects': {}, 'runs': {}}
    for key, case in state.get('projects', {}).items():
        project = case.get('project_id')
        item = {'project_id': project, 'preparations': [], 'chats': {}}
        if project:
            item['preparations'] = [dict(row) for row in connection.execute(
                '''SELECT p.id,p.filename,p.status,p.scope_key,p.error_code,p.error_message,p.created_at,p.ready_at,p.attempts
                   FROM document_preparations p JOIN document_upload_groups g ON g.id=p.group_id
                   WHERE g.project_id=?''', (project,))]
        for conversation, chat in case.get('chats', {}).items():
            public_id = chat.get('chat_id')
            chat_row = connection.execute(
                'SELECT id,public_id FROM chat_threads WHERE projectId=? AND public_id=?',
                (project, public_id)).fetchone() if project and public_id else None
            chat_item = {'chat_id': public_id, 'runs': {}}
            if chat_row:
                chat_item['chat_db_id'] = chat_row['id']
                usage = connection.execute(
                    'SELECT llmCallCount,totalTokens,downloadLlmCallCount,downloadTokens,downloadAttempts,downloadSuccesses '
                    'FROM workspace_usage WHERE chat_thread_id=?', (chat_row['id'],)).fetchone()
                chat_item['usage'] = dict(usage) if usage else {}
                draft = connection.execute(
                    'SELECT draft_version FROM draft_documents WHERE chat_thread_id=?', (chat_row['id'],)).fetchone()
                chat_item['draft_version'] = draft['draft_version'] if draft else None
                for row in connection.execute(
                        'SELECT id,status,phase,attempts,created_at,updated_at,error_message,result_json '
                        'FROM workspace_runs WHERE chat_thread_id=? ORDER BY created_at', (chat_row['id'],)):
                    value = dict(row)
                    result = value.pop('result_json', None)
                    value['result'] = json.loads(result) if result else {}
                    steps = [dict(step) for step in connection.execute(
                        'SELECT sequence,name,kind,state,result_json FROM agent_steps WHERE run_id=? ORDER BY sequence',
                        (row['id'],))]
                    value['tool_counts'] = dict(Counter(step['name'] for step in steps if step['kind'] == 'tool'))
                    value['last_step'] = ({key: val for key, val in steps[-1].items() if key != 'result_json'}
                                          if steps else None)
                    value['tool_errors'] = []
                    for step in steps:
                        result = json.loads(step['result_json'] or '{}')
                        if step['kind'] == 'tool' and isinstance(result, dict) and result.get('error'):
                            value['tool_errors'].append({
                                'sequence': step['sequence'], 'name': step['name'],
                                'message': result.get('message') or result.get('error')})
                    chat_item['runs'][row['id']] = value
                    out['runs'][row['id']] = value
            item['chats'][conversation] = chat_item
        out['projects'][key] = item
    connection.close()
    return out


if __name__ == '__main__':
    print(json.dumps(snapshot(Path(sys.argv[1]))))
