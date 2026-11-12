"""Shared web-search provider contract without network calls."""
from datetime import datetime, timezone
from types import SimpleNamespace

from billiard.exceptions import SoftTimeLimitExceeded
from openai import OpenAIError
import pytest
import requests

from backend.ai import base
from backend.util import web_search
from backend.util.case_assistant_workflow import WorkflowStopped


def test_general_search_supplies_current_utc_date_and_existing_model_settings(monkeypatch):
    class FixedDatetime:
        @classmethod
        def now(cls, tz):
            assert tz is timezone.utc
            return datetime(2026, 11, 10, 20, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(web_search, 'datetime', FixedDatetime)
    calls = []
    def search(query, **kwargs):
        calls.append((query, kwargs))
        return 'A generated update.', {'sources': [{'url': 'https://example.com/update', 'title': 'Update'}]}
    monkeypatch.setattr(base, 'web_search_completion', search)

    result = web_search.search('Recent product updates', model='assigned-model', reasoning_effort='medium')

    query, options = calls[0]
    assert query == 'Recent product updates'
    assert options['model'] == 'assigned-model' and options['reasoning_effort'] == 'medium'
    assert options['return_meta'] is True
    assert 'Current UTC date: 2026-11-10.' in options['sys_prompt']
    assert 'publication or event dates from retrieval time' in options['sys_prompt']
    assert 'search synthesis from directly retrieved page or opinion text' in options['sys_prompt']
    assert result['retrieved_at'] == '2026-11-10T20:00:00+00:00'
    assert result['generated_summary'] == 'A generated update.'
    assert result['summary_is_source_text'] is False
    assert result['error'] is None and 'limitations' not in result


def test_caller_instructions_are_passed_unchanged(monkeypatch):
    seen = []
    def search(query, **kwargs):
        seen.append(kwargs['sys_prompt'])
        return 'Legal discovery summary.', {'sources': []}
    monkeypatch.setattr(base, 'web_search_completion', search)

    web_search.search('legal issue', model='assigned-model', reasoning_effort=None,
                      sys_prompt='Existing legal instructions.')

    assert seen == ['Existing legal instructions.']


def test_source_records_use_observed_metadata_and_preserve_distinct_urls(monkeypatch):
    metadata = SimpleNamespace(
        sources=[SimpleNamespace(url='https://example.com/a', title='Source A', snippet='A preview'),
                 {'url': 'https://[broken'}, {'url': 'https://user:password@example.com/private'},
                 {'url': 'javascript:alert(1)'}, {'url': '/relative'}],
        annotations=[{'url_citation': {'url': 'https://example.com/a', 'title': 'Duplicate'}},
                     SimpleNamespace(url_citation=SimpleNamespace(url='https://example.com/b', title='Source B'))])
    monkeypatch.setattr(base, 'web_search_completion', lambda *a, **kw:
                        ('Generated prose mentioning https://invented.example/fake', metadata))

    result = web_search.search('Updates', model='assigned-model', reasoning_effort=None)

    assert [source['url'] for source in result['results']] == ['https://example.com/a', 'https://example.com/b']
    assert result['results'][0]['snippet'] == 'A preview'
    assert result['results'][1]['snippet'] == ''
    assert all(source['snippet_is_source_text'] is False for source in result['results'])
    assert result['summary_is_source_text'] is False


@pytest.mark.parametrize('summary,sources,missing', [
    ('A synthesis without sources.', [], ['No source URLs']),
    ('', [{'url': 'https://example.com/a'}], ['no generated summary']),
    ('', [], ['No source URLs', 'no generated summary']),
])
def test_empty_outputs_are_explicit_observations(monkeypatch, summary, sources, missing):
    calls = []
    def search(*args, **kwargs):
        calls.append(1)
        return summary, {'sources': sources}
    monkeypatch.setattr(base, 'web_search_completion', search)

    result = web_search.search('Updates', model='assigned-model', reasoning_effort=None)

    assert len(calls) == 1 and result['error'] is None
    assert result['generated_summary'] == summary
    assert len(result['limitations']) == len(missing)
    assert all(any(fragment in value for value in result['limitations']) for fragment in missing)


@pytest.mark.parametrize('failure', [RuntimeError('failed'), ValueError('failed'),
                                  requests.ConnectionError('failed'), OpenAIError('failed')])
def test_provider_errors_return_observations_without_adapter_retries(monkeypatch, failure):
    calls = []
    def fail(*args, **kwargs):
        calls.append(1)
        raise failure
    monkeypatch.setattr(base, 'web_search_completion', fail)

    result = web_search.search('Updates', model='assigned-model', reasoning_effort=None)

    assert calls == [1]
    assert result == {'results': [], 'error': f'Web search unavailable ({type(failure).__name__}).',
                      'provider': 'web', 'query': 'Updates'}


@pytest.mark.parametrize('query', ['', ' \n ', None])
def test_empty_query_does_not_call_provider(monkeypatch, query):
    monkeypatch.setattr(base, 'web_search_completion', lambda *a, **kw: pytest.fail('Provider called'))
    assert web_search.search(query, model='assigned-model', reasoning_effort=None)['error']


def test_cancellation_before_search_prevents_provider_call(monkeypatch):
    monkeypatch.setattr(base, 'web_search_completion', lambda *a, **kw: pytest.fail('Provider called'))
    def stopped():
        raise WorkflowStopped('cancelled')
    with pytest.raises(WorkflowStopped, match='cancelled'):
        web_search.search('Updates', model='assigned-model', reasoning_effort=None, check_execution=stopped)


@pytest.mark.parametrize('failure', [WorkflowStopped('cancelled'), SoftTimeLimitExceeded()])
def test_cancellation_or_deadline_after_provider_propagates(monkeypatch, failure):
    provider_calls, checks = [], []
    def search(*args, **kwargs):
        provider_calls.append(1)
        return 'Generated update.', {'sources': []}
    def check():
        checks.append(1)
        if len(checks) == 2:
            raise failure
    monkeypatch.setattr(base, 'web_search_completion', search)

    with pytest.raises(type(failure)):
        web_search.search('Updates', model='assigned-model', reasoning_effort=None, check_execution=check)
    assert provider_calls == [1] and checks == [1, 1]


@pytest.mark.parametrize('failure', [WorkflowStopped('cancelled'), SoftTimeLimitExceeded()])
def test_provider_execution_termination_is_not_returned_as_search_error(monkeypatch, failure):
    def stop(*args, **kwargs):
        raise failure
    monkeypatch.setattr(base, 'web_search_completion', stop)
    with pytest.raises(type(failure)):
        web_search.search('Updates', model='assigned-model', reasoning_effort=None)
