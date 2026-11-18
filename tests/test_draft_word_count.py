"""Visible-text counts are deterministic metadata, never edits or publication gates."""
import copy
import hashlib
import json
from pathlib import Path
import re

import pytest

from backend import agent_tasks as tasks
from backend.ai import base
from backend.database import db
from backend.models import AgentArtifact, AgentStep
from backend.util import document_drafts as drafts, workspace_service as service, workspace_store
from tests.test_agent_controller import agent, operation, last_result, state, tick, call
from tests.test_document_preparation import app
from tests.test_working_draft import write, manual


def draft(markdown, title='Metadata title is not body text'):
    return {'format_version':2,'source_format':'markdown','title':title,'markdown':markdown}


@pytest.mark.parametrize('markdown,expected', [
    ('',0), (' \n\t',0), ('# Heading\n\nOne paragraph.',3),
    ('[Mata](https://example.com/hidden/words/12345)',1),
    ('[**Mata** and *others*](https://example.com/case_(2020) "Hidden title")',3),
    ('[Visible label][case]\n\n[case]: https://example.com/hidden/many/words "Hidden title"',2),
    ('before ![ignored image words](https://example.com/image.png) after',2),
    ('co**oper**ation pre[fix](https://example.com/x)suffix',2),
    ('one\ntwo  \nthree',3),
    ('| A | B |\n| --- | --- |\n| 1 | 2 |',4),
    ('1. First\n2. Second\n\n- Third\n- Fourth',4),
    ('```python\nprint("hello world")\n```\n\n`two words`',5),
    ('    indented code\n',2),
    ('**bold** *italic* ~~deleted~~',3),
    ('Fish &amp; chips; don’t re-enter 2026.',5),
    ('https://example.com/visible/path',5),
    ('[unresolved][label]',2),
])
def test_visible_word_convention(markdown,expected):
    value=draft(markdown)
    original=copy.deepcopy(value)
    assert drafts.draft_word_count(value)==expected
    assert value==original


def test_destinations_never_change_count_but_labels_do():
    for destination in ('https://example.com/a','https://example.com/long/path_(with)/many/words?q=another-value#part'):
        assert drafts.draft_word_count(draft(f'Use [this label]({destination}).'))==3
        assert drafts.draft_word_count(draft(f'Use [this longer label]({destination}).'))==4


def test_legacy_sections_count_visible_headings_without_mutation():
    value={'title':'Report','sections':[{'section_name':'Facts','section_content':'A [case](https://example.com/hidden).'}]}
    original=copy.deepcopy(value)
    assert drafts.draft_word_count(value)==4
    assert value==original


def test_recorded_921_word_candidate_counts_855_without_modifying_benchmark():
    path=Path(__file__).resolve().parents[1]/'artifacts/model-benchmark-openai-sullivan-coomer-page-corrections-20261117-153455/results/evidence.json'
    if not path.exists():
        pytest.skip('Frozen private benchmark is available only in the local evaluation workspace.')
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    recorded=json.loads(path.read_text())
    saved=next(a['content'] for run in recorded['runs'] for a in run['artifacts']
               if a['id']=='4845b2d9-4e32-4c86-9fa0-1c6d9983e932')
    saved=json.loads(saved) if isinstance(saved,str) else saved
    original=copy.deepcopy(saved)
    assert len(re.findall(r"\b[\w’'-]+\b",saved['draft']['markdown']))==921
    assert drafts.draft_word_count(saved['draft'])==855
    assert saved==original and hashlib.sha256(path.read_bytes()).hexdigest()==before


def read(agent,monkeypatch,uid,*,start=0,end=None):
    operation(agent,monkeypatch,'read_sources',{'artifact_id':uid,'ranges':[],'start':start,'end':end})
    result=last_result(agent)
    assert not result.get('error'),result
    return result


def test_create_edit_unchanged_and_historical_reads_keep_exact_revision_counts(agent,monkeypatch):
    first=write(agent,monkeypatch,'One [link](https://example.com/long/path).')
    assert last_result(agent)['draft_word_count']==2
    original=db.session.get(AgentArtifact,first).content_json
    assert 'draft_word_count' not in json.loads(original)['draft']
    second=write(agent,monkeypatch,'One [link](https://example.com/long/path). Added three words.',target='working_draft')
    assert last_result(agent)['draft_word_count']==5
    second_original=db.session.get(AgentArtifact,second).content_json
    # An unchanged revision still reports the actual current document's count.
    monkeypatch.setattr(drafts,'write_document',lambda **kw: drafts.MarkdownDraftResult(
        title=kw['target']['title'],markdown=kw['target']['markdown'],update_court_format=False))
    operation(agent,monkeypatch,'revise_draft',{'target':'working_draft','revision_mode':'general_revision',
        'instructions':'Leave the current wording.','material_ids':[]})
    result=last_result(agent)
    assert result['unchanged'] and result['draft_word_count']==5
    assert AgentArtifact.query.filter_by(run_id=agent.id,kind='draft').count()==2
    db.session.expire_all()
    full=read(agent,monkeypatch,'working_draft')
    assert full['artifact_id']==second and full['draft_word_count']==5
    partial=read(agent,monkeypatch,second,start=2,end=40)
    assert partial['draft_word_count']==5 and not partial['complete']
    assert partial['text']==full['text'][2:40]
    assert partial['total_characters']==len(full['text'])
    assert json.loads(full['text'])==json.loads(second_original)
    assert read(agent,monkeypatch,first)['draft_word_count']==2
    assert db.session.get(AgentArtifact,first).content_json==original
    assert db.session.get(AgentArtifact,second).content_json==second_original


def test_initial_legacy_and_old_saved_drafts_get_counts_without_rewriting_history(agent,monkeypatch):
    original=copy.deepcopy(state(agent)['draft'])
    count=drafts.draft_word_count(original)
    assert read(agent,monkeypatch,'current_draft')['draft_word_count']==count
    assert read(agent,monkeypatch,'working_draft',start=1,end=25)['draft_word_count']==count
    assert state(agent)['draft']==original
    step=tasks.steps(agent).filter_by(kind='tool').first()
    uid='legacy-saved-draft'
    content=service.encode({'kind':'draft','draft':original})
    db.session.add(AgentArtifact(id=uid,run_id=agent.id,step_id=step.id,kind='draft',
        content_json=content,created_at=service.now()))
    db.session.commit()
    db.session.expire_all()
    observed=read(agent,monkeypatch,uid)
    assert observed['draft_word_count']==count and observed['text']==content
    assert db.session.get(AgentArtifact,uid).content_json==content


def test_restore_and_noop_restore_report_full_resolved_count(agent,monkeypatch):
    manual(agent.chat_thread_id,'One [link](https://example.com/hidden/path).')
    manual(agent.chat_thread_id,'A longer manual document here.')
    operation(agent,monkeypatch,'list_draft_versions',{})
    versions=last_result(agent)['versions']
    older=next(v for v in versions if v['draft_version']==2)
    operation(agent,monkeypatch,'restore_draft',{'version_id':older['version_id']})
    restored=last_result(agent)
    assert restored['draft_word_count']==2
    assert read(agent,monkeypatch,'working_draft')['draft_word_count']==2
    count=AgentArtifact.query.filter_by(run_id=agent.id,kind='draft').count()
    operation(agent,monkeypatch,'restore_draft',{'version_id':older['version_id']})
    unchanged=last_result(agent)
    assert unchanged['unchanged'] and unchanged['draft_word_count']==2
    assert AgentArtifact.query.filter_by(run_id=agent.id,kind='draft').count()==count


def test_non_draft_artifact_reads_do_not_gain_a_draft_count(agent,monkeypatch):
    operation(agent,monkeypatch,'list_documents',{})
    step=tasks.steps(agent).filter_by(kind='tool').first()
    uid='word-count-analysis'
    content=service.encode({'kind':'analysis','text':'Not a draft.'})
    db.session.add(AgentArtifact(id=uid,run_id=agent.id,step_id=step.id,kind='analysis',
        content_json=content,created_at=service.now()))
    db.session.commit()
    result=read(agent,monkeypatch,uid)
    assert result['text']==content and 'draft_word_count' not in result


def test_count_reaches_next_controller_without_another_counting_operation(agent,monkeypatch):
    uid=write(agent,monkeypatch,'[Mata](https://example.com/hidden/many/words) supports this claim.')
    def controller(request):
        outputs=[json.loads(item['output']) for item in request['input'] if item.get('type')=='function_call_output']
        assert outputs[-1]['artifact_id']==uid and outputs[-1]['draft_word_count']==4
        assert 'Use draft_word_count for draft-length checks' in request['input'][0]['content']
        return call('complete_task',{'response':'Draft ready.','candidate_id':'working_draft'})
    monkeypatch.setattr(base,'responses_completion',controller)
    tick(agent);tick(agent)
    assert agent.status=='completed'
    assert tasks.steps(agent).filter_by(name='python_run').count()==0
