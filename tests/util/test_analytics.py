from types import SimpleNamespace

from backend.models import StepAnalytics
from backend.util import analytics as analytics_module


def test_analytics_record_creates_step_entry(monkeypatch, fake_db):
    fake_draft = SimpleNamespace(parent=SimpleNamespace(userId=17))
    fake_query = SimpleNamespace(
        filter_by=lambda **kwargs: SimpleNamespace(first=lambda: fake_draft)
    )
    monkeypatch.setattr(
        analytics_module,
        'ChatThread',
        SimpleNamespace(query=fake_query),
    )

    analytics_module.analytics_record(fake_db, 99, 'search', 3)

    added = fake_db.session.add.call_args.args[0]
    assert isinstance(added, StepAnalytics)
    assert added.userId == 17
    assert added.draftId == 99
    assert added.docType == 'search'
    assert added.step == 3
    fake_db.session.commit.assert_called_once()


def test_analytics_record_research_uses_sentinel_draft_id(fake_db):
    analytics_module.analytics_record_research(fake_db, 11)

    added = fake_db.session.add.call_args.args[0]
    assert isinstance(added, StepAnalytics)
    assert added.userId == 11
    assert added.draftId == 0
    assert added.docType == 'research'
    assert added.step == 0
    fake_db.session.commit.assert_called_once()


def test_analytics_record_fail_open_returns_true_on_success(monkeypatch, fake_db):
    recorded = []
    monkeypatch.setattr(
        analytics_module,
        'analytics_record',
        lambda db, draft_id, doc_type, step: recorded.append(
            (db, draft_id, doc_type, step)
        ),
    )

    result = analytics_module.analytics_record_fail_open(
        fake_db,
        99,
        'complaint',
        100,
    )

    assert result is True
    assert recorded == [(fake_db, 99, 'complaint', 100)]
    fake_db.session.rollback.assert_not_called()


def test_analytics_record_fail_open_rolls_back_and_swallows_failure(
    monkeypatch,
    fake_db,
):
    def fail_record(*args, **kwargs):
        raise RuntimeError('analytics unavailable')

    monkeypatch.setattr(analytics_module, 'analytics_record', fail_record)

    result = analytics_module.analytics_record_fail_open(
        fake_db,
        99,
        'complaint',
        100,
    )

    assert result is False
    fake_db.session.rollback.assert_called_once()
