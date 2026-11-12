import ast
import inspect
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend import (
    answer_drafter,
    complaint_drafter,
    dismiss_drafter,
    element_checker,
    rfa_drafter,
    rfp_drafter,
    rfp_response_drafter,
    rog_drafter,
    rog_response_drafter,
    views,
)
from backend.util import analytics as analytics_util


GUIDED_DRAFTER_STEPS = (
    (answer_drafter, {0, 1, 2, 3, 4}, 5),
    (complaint_drafter, {0, 1, 2, 3}, 4),
    (dismiss_drafter, {0, 1, 2, 3, 4}, 5),
    (element_checker, {0, 1, 2, 3}, 4),
    (rfa_drafter, {0, 1, 2}, 3),
    (rfp_drafter, {0, 1, 2}, 3),
    (rog_drafter, {0, 1, 2}, 3),
    (rfp_response_drafter, {0, 1, 2}, 3),
    (rog_response_drafter, {0, 1, 2}, 3),
)

AUXILIARY_OPERATIONS = (
    (complaint_drafter, complaint_drafter.regenerate_citations_complaint, 0, False),
    (element_checker, element_checker.regenerate_citations, 1, False),
    (dismiss_drafter, dismiss_drafter.route_motion_dismiss_update_background, 0, True),
    (dismiss_drafter, dismiss_drafter.motion_dismiss_expand_argument, 2, False),
    (rfa_drafter, rfa_drafter.further_reduce_rfa_task, 0, False),
    (rfp_drafter, rfp_drafter.further_reduce_rfp_task, 0, False),
    (rog_drafter, rog_drafter.further_reduce_task, 0, False),
)


def _literal_decorator_steps(module, decorator_name):
    tree = ast.parse(Path(module.__file__).read_text())
    steps = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            if not isinstance(decorator.func, ast.Name) or decorator.func.id != decorator_name:
                continue
            if len(decorator.args) >= 2 and isinstance(decorator.args[1], ast.Constant):
                steps.add(decorator.args[1].value)
    return steps


@pytest.mark.parametrize(
    ('module', 'primary_steps', 'download_step'),
    GUIDED_DRAFTER_STEPS,
    ids=lambda value: getattr(value, '__name__', str(value)).split('.')[-1],
)
def test_guided_drafters_have_complete_primary_step_mappings(
    module,
    primary_steps,
    download_step,
):
    assert _literal_decorator_steps(module, 'guided_step_operation') == primary_steps
    assert module.DOWNLOAD_STEP == download_step


def test_no_reserved_auxiliary_step_constants_or_recorders_remain():
    for module, _operation, _outer_step, _uses_request in AUXILIARY_OPERATIONS:
        source = Path(module.__file__).read_text()
        assert 'STEP = 100' not in source
        assert 'STEP = 101' not in source
        assert 'analytics_record_fail_open' not in source


class DownstreamFailure(RuntimeError):
    pass


@pytest.mark.parametrize(
    ('module', 'operation', 'outer_step', 'uses_request'),
    AUXILIARY_OPERATIONS,
    ids=[
        'complaint-regenerate-citations',
        'analyzer-regenerate-citations',
        'dismiss-update-background',
        'dismiss-expand-argument',
        'rfa-further-reduce',
        'rfp-further-reduce',
        'rog-further-reduce',
    ],
)
def test_auxiliary_operations_attach_to_outer_step_without_creating_a_row(
    monkeypatch,
    module,
    operation,
    outer_step,
    uses_request,
):
    # This analytics unit test deliberately replaces product storage entirely.
    from backend.util import account_access
    monkeypatch.setattr(account_access, 'chat_account_id', lambda _id: 1)
    monkeypatch.setattr(account_access, 'account_execution', lambda _id: nullcontext())
    attached = []
    monkeypatch.setattr(
        analytics_util,
        'latest_or_create_step_analytics',
        lambda draft_id, doc_type, step: attached.append(
            (draft_id, doc_type, step)
        ) or 99,
    )
    monkeypatch.setattr(
        module,
        'DBMemory',
        lambda **kwargs: (_ for _ in ()).throw(DownstreamFailure()),
    )
    monkeypatch.setattr(module.Drafts, 'resolve_id', lambda _draft_id: 42)

    payload = {
        'draftId': 'public-draft',
        'coa_index': 0,
        'coa_id': '0',
        'element_index': 0,
    }
    with pytest.raises(DownstreamFailure):
        if uses_request:
            operation(SimpleNamespace(form=payload))
        else:
            operation.run(payload)

    assert attached == [('public-draft', module.DOC_TYPE, outer_step)]


def test_motion_dismiss_download_tracks_step_once(monkeypatch):
    draft = SimpleNamespace(id=42, public_id='public-draft', display_name='Draft')

    def ownership_check(*args, **kwargs):
        return None

    ownership_check._last_draft = draft
    recorded = []

    @contextmanager
    def fake_guided_step_run(draft_id, doc_type, step):
        recorded.append((draft_id, doc_type, step))
        yield 123

    monkeypatch.setattr(views, 'current_user', SimpleNamespace(id=17))
    monkeypatch.setattr(views, 'ownership_check', ownership_check)
    monkeypatch.setattr(views, 'guided_step_run', fake_guided_step_run)
    monkeypatch.setattr(views, 'DBMemory', lambda **kwargs: object())
    monkeypatch.setattr(views, 'draft_full_dismiss_word', lambda *args: None)
    monkeypatch.setattr(views, 'send_file', lambda *args, **kwargs: 'download')

    result = views.download_word_dismiss.__wrapped__('public-draft')

    assert result == 'download'
    assert recorded == [(42, dismiss_drafter.DOC_TYPE, dismiss_drafter.DOWNLOAD_STEP)]


@pytest.mark.parametrize(
    'parse_task',
    (
        answer_drafter.answer_parse_complaint_task,
        rfa_drafter.rfa_parse_complaint_task,
        rfp_drafter.rfp_parse_complaint_task,
        rog_drafter.rog_parse_complaint_task,
        rfp_response_drafter.rfp_response_parse_document_task,
        rog_response_drafter.rog_response_parse_document_task,
    ),
)
def test_upload_parse_tasks_do_not_start_a_step_directly(parse_task):
    source = inspect.getsource(parse_task.run)
    assert 'guided_step_operation' not in source


@pytest.mark.parametrize(
    'selection_update',
    (
        complaint_drafter.update_citation_selections_complaint,
        element_checker.update_citation_selections,
        dismiss_drafter.update_citation_selections_dismiss,
    ),
)
def test_routine_citation_selection_updates_do_not_record_a_step(selection_update):
    source = inspect.getsource(selection_update)
    assert 'guided_step_operation' not in source
    assert 'attach_step_usage' not in source
