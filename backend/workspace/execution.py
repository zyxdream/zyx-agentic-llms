"""Atomic ownership and failure handling for the Workspace agent only."""
import uuid
from datetime import timedelta
from celery import shared_task
from flask import current_app
from backend.database import db
from backend.util import workspace_service as service
from backend.util.workspace_service import Workflow, WorkflowStopped, now
LEASE_SECONDS = 1320
TASK_OPTIONS = dict(bind=True, ignore_result=False, acks_late=False,
    reject_on_worker_lost=False, acks_on_failure_or_timeout=True,
    max_retries=0, soft_time_limit=1200, time_limit=1260)

def claim_workflow(workflow_id, *, task_id=None, task_field='expected_task_id'):
    row = db.session.get(Workflow, workflow_id)
    if row is None or not task_id or task_field != 'expected_task_id':
        return None
    service.check_failure(row)
    token = str(uuid.uuid4())
    claimed = Workflow.query.filter_by(id=workflow_id, status='running', lease_token=None,
        dispatch_pending=True, expected_task_id=task_id).update({
        'lease_token': token, 'lease_until': now() + timedelta(seconds=LEASE_SECONDS),
        'updated_at': now(), 'last_progress_at': now(), 'dispatch_pending': False}, synchronize_session=False)
    db.session.commit()
    return token if claimed else None

@shared_task(ignore_result=True)
def reconcile_runs():
    from backend.agent_tasks import dispatch
    ids = [uid for uid, in db.session.query(Workflow.id).filter(Workflow.status.in_(service.ACTIVE)).all()]
    for uid in ids:
        try:
            row = service.check_failure(db.session.get(Workflow, uid))
            if row.status == 'running':
                dispatch(row, recover=True)
        except Exception:
            db.session.rollback()
            current_app.logger.exception('Could not reconcile Workspace run %s', uid)


@shared_task(ignore_result=True, acks_late=False, reject_on_worker_lost=False, max_retries=0)
def prompt_task_failed(request, exc, traceback):
    """Celery invokes this errback directly, including after child-worker loss.

    Match the saved task ID in SQL. A delayed failure from before explicit Retry is ignored.
    Do not expose raw provider exceptions in the browser or retry this handler.
    """
    try:
        service.fail_task(request.id, 'Request task failed or timed out. Click Retry to resume saved work.')
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Could not save Workspace task failure; deadline checks remain active')


def locked_workflow(workflow_id, token):
    """Refresh/lock the workflow and reject an expired or replaced worker.

    The SQL row lock lasts only until commit/rollback. The saved lease token
    continues to identify the attempt after that short transaction has ended.
    """
    row = Workflow.query.filter_by(id=workflow_id).populate_existing().with_for_update().one()
    if row.lease_token != token or not row.lease_until or row.lease_until <= now():
        raise WorkflowStopped('This worker no longer owns the request.')
    service.validate(row)
    return row


def stop_owned_workflow(workflow_id, token, message):
    """A replaced or cancelled worker must not fail a newer attempt."""
    row = Workflow.query.filter_by(id=workflow_id).populate_existing().with_for_update().one()
    if row.status == 'running' and row.lease_token == token:
        service.stop(row, message)
    else:
        db.session.rollback()
