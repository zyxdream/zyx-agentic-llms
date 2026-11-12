
import pytest
from types import SimpleNamespace

from backend.util import db as db_module
from backend.util.db import DBMemory
from backend.util.db import DiskMemory


def test_create(tmp_path):
    db = DiskMemory(tmp_path)

    test_str = "this is a test"
    fn = 'test_file'
    db[fn] = test_str

    text_from_fileopen = (tmp_path / fn).read_text()

    assert text_from_fileopen == test_str


def test_get_returns_default_for_missing_key(tmp_path):
    db = DiskMemory(tmp_path)

    assert db.get('missing-file', 'fallback value') == 'fallback value'


def test_overwrite_updates_existing_file(tmp_path):
    db = DiskMemory(tmp_path)

    db['draft'] = 'first value'
    db['draft'] = 'second value'

    assert db['draft'] == 'second value'


def test_missing_key_raises_key_error(tmp_path):
    db = DiskMemory(tmp_path)

    with pytest.raises(KeyError):
        _ = db['missing']


def test_dbmemory_delete_existing_key(monkeypatch):
    deleted = []
    commit_calls = []
    entry = object()

    class FakeQuery:
        def filter_by(self, **kwargs):
            assert kwargs == {"chat_thread_id": 42, "textName": "drafted_sections/intro"}
            return self

        def first(self):
            return entry

    fake_session = SimpleNamespace(
        delete=lambda item: deleted.append(item),
        commit=lambda: commit_calls.append(True),
    )
    fake_db = SimpleNamespace(session=fake_session)
    mem = DBMemory.__new__(DBMemory)
    mem.chat_thread_id = 42
    mem.db = fake_db
    monkeypatch.setattr(db_module, "TextEntry", SimpleNamespace(query=FakeQuery()))

    del mem["drafted_sections/intro"]

    assert deleted == [entry]
    assert commit_calls == [True]


def test_dbmemory_delete_missing_key_raises(monkeypatch):
    class FakeQuery:
        def filter_by(self, **kwargs):
            return self

        def first(self):
            return None

    fake_db = SimpleNamespace(session=SimpleNamespace(delete=lambda _item: None, commit=lambda: None))
    mem = DBMemory.__new__(DBMemory)
    mem.chat_thread_id = 42
    mem.db = fake_db
    monkeypatch.setattr(db_module, "TextEntry", SimpleNamespace(query=FakeQuery()))

    with pytest.raises(KeyError):
        del mem["missing"]
