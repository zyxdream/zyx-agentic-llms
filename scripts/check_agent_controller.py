"""Opt-in live smoke test against a disposable database and in-memory broker.

Run from the repo root in the lexintel Conda environment. Uses configured
OpenAI credentials and real selected models; never accesses production SQL or
Redis. Provider calls incur normal usage. No controller budget is imposed.
"""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from celery import Celery
from flask import Flask
from werkzeug.datastructures import FileStorage

from backend.database import db
from backend.models import User, Project, Drafts, TextEntry, AgentStep
from backend.util import document_preparation as prep, case_assistant_workflow as service
from backend import document_preparation_tasks, agent_tasks, everything


def main():
    with tempfile.TemporaryDirectory(prefix='lexintel-agent-smoke-') as temp:
        app = Flask(__name__)
        app.config.update(SQLALCHEMY_DATABASE_URI=f'sqlite:///{temp}/smoke.db',
            DOCUMENT_PREPARATION_SOURCE_DIR=f'{temp}/sources', CELERY={'task_default_queue': 'smoke'})
        local_queue = Celery('agent-smoke', broker='memory://', backend='cache+memory://')
        local_queue.set_default()
        db.init_app(app)
        with app.app_context():
            db.create_all()
            db.session.add(User(id=1, email='agent-smoke@example.test'))
            db.session.add(Project(id=1, name='Synthetic smoke test', userId=1))
            db.session.add(Drafts(id=1, public_id='smoke-draft', projectId=1, docCategory=69, display_name='Smoke test'))
            initial = everything.create_empty_state()
            initial['draft'] = {'title': 'Original', 'sections': []}
            db.session.add(TextEntry(draftId=1, textName='state', content=json.dumps(initial)))
            db.session.commit()
            content = (b'Order: Twelve chairs at $20 each, total $240. Delivery is due June 10.\n\n'
                       b'Invoice: Twelve chairs at $20 each, total $260. The invoice total is disputed; no tax or delivery fee is listed.')
            group = prep.admit_group(1, 'chat', 'smoke-draft', 'smoke-selection',
                [{'client_upload_id': 'source', 'filename': 'Order and invoice.txt', 'file_size': len(content)}])
            entry = prep.stage_transfer(prep.owned_group(group['group_id'], 1), 'source',
                FileStorage(stream=io.BytesIO(content), filename='Order and invoice.txt'))
            document_preparation_tasks.prepare_document.run(entry['document_id'], entry['generation'])
            doc = db.session.get(prep.DocumentPreparation, entry['document_id'])
            while doc.status in prep.ACTIVE:
                document_preparation_tasks.prepare_document.run(entry['document_id'], entry['generation'])
                db.session.refresh(doc)
            if doc.status != 'ready':
                raise RuntimeError('Synthetic source preparation failed: ' + str(doc.error_message))
            snapshot = prep.build_snapshot(1, 'smoke-draft', {'promptAttachments': [{'file_id': entry['document_id']}]})
            row = service.submit(1, {'draftId': 'smoke-draft', 'assistant_mode': 'fast', 'document_snapshot': snapshot,
                'query': 'Read the attached order and invoice. Analyze the discrepancy, then draft a short factual '
                'summary of at most two sentences. Verify the candidate against the source. If a finding requires '
                'a correction, revise it and then finish. Do not cite legal authorities; this is a synthetic arithmetic example.'})
            agent_tasks.dispatch(row)
            while row.status == 'running':
                agent_tasks.advance.push_request(id=row.planning_task_id)
                try:
                    agent_tasks.advance.run(row.id)
                finally:
                    agent_tasks.advance.pop_request()
                db.session.expire_all()
            history = [{'operation': s.name, 'state': s.state} for s in agent_tasks.steps(row).filter_by(kind='tool').all()]
            saved = everything.load_state_from_memory(everything.DBMemory(db, row.draft_id))
            report = {'status': row.status, 'stopping_reason': row.error_message, 'steps': history,
                'chat_turns': len(saved['chat_history']), 'draft_title': saved['draft'].get('title'),
                'final_response': saved['chat_history'][-1][1] if saved['chat_history'] else None}
            if row.status != 'completed' or not saved['draft'].get('sections') or len(saved['chat_history']) != 1:
                raise RuntimeError(json.dumps(report))
            return report


if __name__ == '__main__':
    # Existing generation helpers log prompt bodies. Keep the smoke report
    # compact; exceptions still surface a traceback and terminal status.
    with contextlib.redirect_stdout(io.StringIO()):
        report = main()
    print(json.dumps(report, indent=2))
