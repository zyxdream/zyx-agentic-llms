"""The application selects one continuous working draft within a saved run.

Artifacts remain immutable. Pointer changes are staged with the successful tool
observation and committed under the worker's existing lease fence.
"""
import json

from backend.models import AgentArtifact, AgentStep
from backend.util import workspace_service as service

WRITERS = {'create_draft', 'revise_draft', 'restore_draft'}
REFERENCE = 'working_draft'


def ensure(row):
    payload = json.loads(row.payload_json)
    context = payload['agent_context']
    if 'working_candidate_id' not in context:
        # Upgrade an incomplete old run from successful step order, never from
        # artifact timestamps or the model's remembered candidate selection.
        head = None
        for step in AgentStep.query.filter_by(run_id=row.id, kind='tool', state='ready').filter(
                AgentStep.name.in_(WRITERS)).order_by(AgentStep.sequence.desc()):
            result = json.loads(step.result_json or '{}')
            if result.get('error'):
                continue
            uid = result.get('artifact_id')
            saved = AgentArtifact.query.filter_by(id=uid, run_id=row.id, step_id=step.id, kind='draft').first() if uid else None
            if saved:
                head = saved.id
                break
        context['working_candidate_id'] = head
        row.payload_json = service.encode(payload)
    return context['working_candidate_id']


def resolve(row, reference):
    return (ensure(row) or 'current_draft') if reference == REFERENCE else reference


def edit_target(row, reference=None, *, create=False):
    head = ensure(row)
    if head and (create or reference not in (REFERENCE, head)):
        raise ValueError('The working draft has advanced. Revise working_draft with general_revision '
                         f'(current candidate: {head}); older drafts and uploads cannot be edit bases. '
                         'Use restore_draft only for an explicit user rollback request.')
    return resolve(row, reference) if reference is not None else None


def publication_target(row, reference):
    head = ensure(row)
    if reference is None:
        return None
    if reference == REFERENCE:
        return head
    if reference != head:
        raise ValueError('Only the working draft can be published. Use candidate_id="working_draft" '
                         'or its exact current candidate ID; null leaves Output unchanged.')
    return head


def promote(row, step, result):
    if step.name not in WRITERS or result.get('error') or not result.get('artifact_id'):
        return
    uid = result['artifact_id']
    saved = AgentArtifact.query.filter_by(id=uid, run_id=row.id, step_id=step.id, kind='draft').first()
    if not saved:
        raise ValueError('The new working draft was not saved by this operation.')
    payload = json.loads(row.payload_json)
    payload['agent_context']['working_candidate_id'] = uid
    row.payload_json = service.encode(payload)
    result.update(working_draft=REFERENCE, working_candidate_id=uid)


def describe(row):
    head = ensure(row)
    context = json.loads(row.payload_json)['agent_context']
    return {'reference': REFERENCE, 'candidate_id': head,
            'resolved_target': head or 'current_draft',
            'initial_editor_version': context.get('state', {}).get('draft_version'),
            'rule': 'Edit and publish only working_draft. Older drafts may be read or verified, '
                    'but only an explicit user rollback request permits restore_draft.'}
