"""Explicit maintenance for duplicate original-range results, never startup work.

Only the old {format: original-ranges-v1, range: {start, end}} records are
eligible. Real summaries and generated findings remain in SummarySegment.
Deploy compatible readers before applying this cleanup. Dry-run is the default.
"""
import json
from collections import Counter

from flask import current_app
from sqlalchemy.orm import load_only

from backend.database import db
from backend.models import CaseAssistantWork as Work, CaseAssistantWorkflow as Workflow, DocumentSummarySegment as Segment
from backend.util.source_context import SOURCE, VERSION, source_span

TERMINAL = ('completed', 'failed', 'cancelled')
MAX_REFERENCES = 1000


def _range_value(saved):
    """Require exactly the known old format, including integer boundaries."""
    value = json.loads(saved.result_json)
    if (saved.kind != 'query' or saved.state != 'ready' or not isinstance(value, dict)
            or set(value) != {'format', 'range'} or value['format'] != SOURCE
            or not isinstance(value['range'], dict) or set(value['range']) != {'start', 'end'}):
        raise ValueError('unexpected_format')
    source_span({**value['range'], 'context_method': 'source', 'format_version': VERSION})
    return value


def _clean_one(segment_id, *, apply):
    """Return reference count/reason, releasing the transaction on every path.

    Workflow-before-Work locking matches cancellation/Retry. A second reference
    check prevents deleting a record whose owners changed while locks were being
    acquired. SQLite needs a short write first because FOR UPDATE is ignored.
    """
    try:
        if apply and db.engine.dialect.name == 'sqlite':
            Segment.query.filter_by(id=segment_id).update({Segment.id: Segment.id}, synchronize_session=False)
        references = db.session.query(Work.id, Work.workflow_run_id).filter_by(result_id=segment_id).limit(MAX_REFERENCES + 1).all()
        if len(references) > MAX_REFERENCES:
            db.session.rollback()
            return 0, 'too_many_references'
        workflow_ids = sorted({item.workflow_run_id for item in references})
        # End the discovery read on MySQL before locking current owner rows.
        # SQLite keeps its write lock until this candidate commits/rolls back.
        if apply and db.engine.dialect.name != 'sqlite':
            db.session.commit()
        workflows = Workflow.query.options(load_only(Workflow.id, Workflow.status)).filter(
            Workflow.id.in_(workflow_ids)).order_by(Workflow.id).populate_existing()
        if apply:
            workflows = workflows.with_for_update()
        workflows = workflows.all()
        if len(workflows) != len(workflow_ids) or any(row.status not in TERMINAL for row in workflows):
            db.session.rollback()
            return 0, 'active_or_unknown_request'
        work = Work.query.filter_by(result_id=segment_id).order_by(Work.id).populate_existing()
        if apply:
            work = work.with_for_update()
        work = work.limit(MAX_REFERENCES + 1).all()
        saved = Segment.query.filter_by(id=segment_id).populate_existing()
        if apply:
            saved = saved.with_for_update()
        saved = saved.first()
        if saved is None:
            db.session.rollback()
            return 0, 'already_removed'
        # The parent lock also prevents new FK references while deletion commits.
        current_refs = db.session.query(Work.id, Work.workflow_run_id).filter_by(result_id=segment_id).limit(MAX_REFERENCES + 1)
        if apply:
            current_refs = current_refs.with_for_update()
        expected = {(ref.id, ref.workflow_run_id) for ref in references}
        if (expected != {(ref.id, ref.workflow_run_id) for ref in work}
                or expected != {(ref.id, ref.workflow_run_id) for ref in current_refs.all()}):
            db.session.rollback()
            return 0, 'references_changed'
        try:
            value = _range_value(saved)
        except (ValueError, TypeError):
            db.session.rollback()
            return 0, 'unexpected_format'
        for item in work:
            try:
                material = json.loads(item.material_json)
                matches = (item.kind == 'query' and item.document_id == saved.document_id
                    and bool(item.artifact_id) and source_span(material) == value['range'])
            except (ValueError, TypeError):
                matches = False
            if not matches:
                db.session.rollback()
                return 0, 'mismatched_reference'
        count = len(work)
        if apply:
            for item in work:
                item.result_id = None
            db.session.flush()  # Clear references before deleting their old result.
            db.session.delete(saved)
            db.session.commit()
        else:
            db.session.rollback()
        return count, None
    except Exception:
        db.session.rollback()
        raise


def cleanup_source_ranges(*, apply=False, batch_size=100):
    """Scan bounded pages; apply each eligible deletion atomically and safely."""
    if type(batch_size) is not int or not 1 <= batch_size <= 1000:
        raise ValueError('batch_size must be between 1 and 1000')
    after, eligible, cleared = 0, 0, 0
    skipped = Counter()
    while True:
        ids = [uid for uid, in db.session.query(Segment.id).filter(Segment.id > after,
            Segment.kind == 'query', Segment.result_json.contains(SOURCE)).order_by(Segment.id).limit(batch_size).all()]
        db.session.commit()
        if not ids:
            break
        for uid in ids:
            count, reason = _clean_one(uid, apply=apply)
            if reason:
                skipped[reason] += 1
                current_app.logger.warning('Skipped range-only result %s: %s', uid, reason)
            else:
                eligible += 1
                cleared += count
        after = ids[-1]
    return {'apply': apply, 'eligible_records': eligible, 'eligible_references': cleared,
            'deleted_records': eligible if apply else 0, 'cleared_references': cleared if apply else 0,
            'skipped': dict(skipped)}
