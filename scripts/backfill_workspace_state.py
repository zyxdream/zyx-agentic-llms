"""Reconcile legacy workspace data after the schema migration, with writers stopped.

Run from the repository root in Conda lexintel. The default is a rollback-only
dry run; --apply commits all imported workspaces in one transaction. This script
does not start workers, change application configuration, or contact providers.
"""
import argparse
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Commit the backfill; otherwise roll back after reconciliation.')
    parser.add_argument('--chat-thread-id', '--draft-id', dest='chat_ids', type=int, action='append',
                        help='Only reconcile these chat IDs; --draft-id remains a legacy alias.')
    parser.add_argument('--report', type=Path, required=True, help='Write the reconciliation report here.')
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from backend.app import create_app
    from backend.database import db
    from backend.models import ChatThread, ConversationTurn, DraftDocument
    from backend.util import workspace_store
    from sqlalchemy import func

    app = create_app()
    report = {'applied': False, 'identity_schema': 'chat_document_v1',
              'workspaces': [], 'unresolved': [], 'error': None}
    with app.app_context():
        try:
            query = ChatThread.query.order_by(ChatThread.id)
            if args.chat_ids:
                query = query.filter(ChatThread.id.in_(args.chat_ids))
            for chat in query.all():
                report['workspaces'].append(workspace_store.backfill_workspace(chat.id))
                last = db.session.query(func.max(ConversationTurn.position)).filter_by(chat_thread_id=chat.id).scalar()
                if last is not None and chat.next_turn_position <= last:
                    report['unresolved'].append({'chat_thread_id': chat.id,
                        'reason': 'Conversation ordering counter precedes saved turns.'})
            report['counts'] = {'chat_threads': ChatThread.query.count(),
                'draft_documents': DraftDocument.query.count(), 'conversation_turns': ConversationTurn.query.count()}
            if report['unresolved']:
                raise ValueError('Reconciliation has unresolved records; no import was committed.')
            if args.apply:
                db.session.commit()
                report['applied'] = True
            else:
                db.session.rollback()
        except Exception as exc:
            db.session.rollback()
            # Records and document contents stay out of operational reports.
            report['error'] = type(exc).__name__
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2))
            raise
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'applied': report['applied'], 'workspaces': len(report['workspaces']),
        'turns_imported': sum(r.get('turns_imported', 0) for r in report['workspaces']),
        'counts': report.get('counts', {}), 'unresolved': len(report['unresolved']),
        'recovered_workflow_turns': sum(r.get('recovered_workflow_turns', 0) for r in report['workspaces']),
        'ambiguities': sum(len(r.get('ambiguities', [])) for r in report['workspaces'])}))


if __name__ == '__main__':
    main()
