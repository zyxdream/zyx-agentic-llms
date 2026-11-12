import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("flask_sqlalchemy")

from backend.util.freelaw_cache import (
    build_caseviewer_freelaw_cluster_cache_snapshot,
    build_freelaw_cluster_cache_snapshot,
    get_caseviewer_response_from_cache_row,
)


def test_build_freelaw_cluster_cache_snapshot_matches_hg_payload_shape():
    cluster = {
        "case_name": "Valid Case",
        "absolute_url": "/opinion/123/valid-case/",
        "docket": "https://www.courtlistener.com/api/rest/v4/dockets/44/",
        "sub_opinions": ["https://www.courtlistener.com/api/rest/v4/opinions/456/"],
    }

    snapshot = build_freelaw_cluster_cache_snapshot(
        cluster_id="123",
        citation="123 F.2d 456",
        cluster=cluster,
        llm_holding="The holding controls.",
        court="9th Cir.",
        source="case_view",
    )

    assert snapshot == {
        "clusterId": 123,
        "cluster_id": 123,
        "id": 123,
        "citation": "123 F.2d 456",
        "case_name": "Valid Case",
        "opinion_id": 456,
        "opinion_url": "https://www.courtlistener.com/api/rest/v4/opinions/456/",
        "docket_url": "https://www.courtlistener.com/api/rest/v4/dockets/44/",
        "llm_holding": "The holding controls.",
        "freelaw_url": "https://www.courtlistener.com/opinion/123/valid-case/",
        "court": "9th Cir.",
        "source": "case_view",
    }
    assert "href" not in snapshot
    assert "llm_hist" not in snapshot


def test_build_freelaw_cluster_cache_snapshot_rejects_invalid_or_empty_data():
    assert build_freelaw_cluster_cache_snapshot(
        cluster_id="not-an-id",
        citation="bad cite",
        cluster={"case_name": "Bad Case"},
        source="case_view",
    ) is None

    assert build_freelaw_cluster_cache_snapshot(
        cluster_id=123,
        citation="bad cite",
        cluster={},
        source="case_view",
    ) is None


def test_build_caseviewer_freelaw_cluster_cache_snapshot_stores_full_response():
    cluster = {
        "case_name": "Valid Case",
        "absolute_url": "/opinion/123/valid-case/",
        "docket": "https://www.courtlistener.com/api/rest/v4/dockets/44/",
        "sub_opinions": ["https://www.courtlistener.com/api/rest/v4/opinions/456/"],
        "date_filed": "2007-05-21",
        "precedential_status": "Published",
        "citations": [
            {"volume": "550", "reporter": "U.S.", "page": "123"},
            {"volume": "127", "reporter": "S. Ct.", "page": "456"},
        ],
        "judges": "Justice Example",
    }
    opinion = {
        "html_with_citations": "<p>Opinion text</p>",
        "author": "Justice Example",
        "attorneys": "Counsel Example",
        "joined_by": "Justice Another",
    }
    response = {
        "cluster": cluster,
        "opinion": opinion,
        "opinions": [opinion],
        "llm_holding": "The holding controls.",
        "llm_hist": "Generated history.",
        "caseviewer_synopsis_version": 1,
        "casetext": None,
        "freelaw_link": "https://www.courtlistener.com/opinion/123/valid-case/",
    }

    snapshot = build_caseviewer_freelaw_cluster_cache_snapshot(
        cluster_id="123",
        citation="123 F.2d 456",
        response=response,
        court="9th Cir.",
    )

    assert snapshot["cache_schema_version"] == 2
    assert snapshot["clusterId"] == 123
    assert snapshot["citation"] == "123 F.2d 456"
    assert snapshot["llm_hist"] == "Generated history."
    assert snapshot["caseviewer_synopsis_version"] == 1
    assert snapshot["caseviewer_response"]["cluster"] == cluster
    assert snapshot["caseviewer_response"]["cluster"]["date_filed"] == "2007-05-21"
    assert snapshot["caseviewer_response"]["opinion"] == opinion
    assert snapshot["caseviewer_response"]["opinions"] == [opinion]
    assert snapshot["caseviewer_response"]["llm_holding"] == "The holding controls."
    assert snapshot["caseviewer_response"]["llm_hist"] == "Generated history."
    assert snapshot["caseviewer_response"]["caseviewer_synopsis_version"] == 1
    assert snapshot["caseviewer_response"]["freelaw_link"] == "https://www.courtlistener.com/opinion/123/valid-case/"
    assert "href" not in snapshot


def test_build_caseviewer_freelaw_cluster_cache_snapshot_rejects_missing_cluster():
    assert build_caseviewer_freelaw_cluster_cache_snapshot(
        cluster_id=123,
        citation="123 F.2d 456",
        response={"cluster": None},
    ) is None


def test_build_caseviewer_snapshot_does_not_mark_legacy_synopsis_as_current():
    snapshot = build_caseviewer_freelaw_cluster_cache_snapshot(
        cluster_id=123,
        citation="123 F.2d 456",
        response={
            "cluster": {"case_name": "Valid Case", "absolute_url": "/opinion/123/valid-case/"},
            "llm_holding": "Old holding.",
            "llm_hist": "Citation-only summary.",
        },
    )

    assert "caseviewer_synopsis_version" not in snapshot
    assert "caseviewer_synopsis_version" not in snapshot["caseviewer_response"]


class _CachedRow:
    cluster_id = 123

    def __init__(self, payload, *, llm_holding=None):
        self.payload_json = json.dumps(payload) if not isinstance(payload, str) else payload
        self.llm_holding = llm_holding


def test_get_caseviewer_response_from_cache_row_normalizes_valid_payload():
    opinion = {"html_with_citations": "<p>Opinion text</p>", "author": "Justice Example"}
    row = _CachedRow({
        "clusterId": 123,
        "case_name": "Valid Case",
        "llm_holding": "Top-level holding.",
        "llm_hist": "Top-level history.",
        "caseviewer_synopsis_version": 1,
        "freelaw_url": "https://www.courtlistener.com/opinion/123/valid-case/",
        "court": "9th Cir.",
        "caseviewer_response": {
            "cluster": {
                "case_name": "Valid Case",
                "absolute_url": "/opinion/123/valid-case/",
            },
            "opinion": None,
            "opinions": [opinion],
            "caseviewer_synopsis_version": 1,
        },
    })

    response, details = get_caseviewer_response_from_cache_row(row)

    assert details["reason"] == "valid"
    assert details["has_caseviewer_response"] is True
    assert details["has_holding"] is True
    assert details["has_hist"] is True
    assert response == {
        "cluster": {
            "case_name": "Valid Case",
            "absolute_url": "/opinion/123/valid-case/",
            "court_citation_string": "9th Cir.",
        },
        "opinion": opinion,
        "opinions": [opinion],
        "llm_holding": "Top-level holding.",
        "llm_hist": "Top-level history.",
        "caseviewer_synopsis_version": 1,
        "casetext": None,
        "freelaw_link": "https://www.courtlistener.com/opinion/123/valid-case/",
    }


def test_get_caseviewer_response_from_cache_row_rejects_legacy_synopsis():
    row = _CachedRow({
        "clusterId": 123,
        "llm_holding": "Previously cached holding.",
        "llm_hist": "Citation-only case history.",
        "caseviewer_response": {
            "cluster": {
                "case_name": "Valid Case",
                "absolute_url": "/opinion/123/valid-case/",
            },
            "opinion": {"html_with_citations": "<p>Opinion text</p>"},
            "opinions": [],
            "llm_holding": "Previously cached holding.",
            "llm_hist": "Citation-only case history.",
        },
    })

    response, details = get_caseviewer_response_from_cache_row(row)

    assert response is None
    assert details["reason"] == "stale_synopsis"
    assert details["has_caseviewer_response"] is True
    assert details["has_holding"] is True
    assert details["has_hist"] is True


def test_get_caseviewer_response_from_cache_row_rejects_summary_only_payload():
    row = _CachedRow({
        "clusterId": 123,
        "case_name": "Valid Case",
        "llm_holding": "Cached holding.",
    })

    response, details = get_caseviewer_response_from_cache_row(row)

    assert response is None
    assert details["reason"] == "missing_caseviewer_response"
    assert details["has_caseviewer_response"] is False
    assert details["has_holding"] is True


def test_get_caseviewer_response_from_cache_row_rejects_bad_payload_json():
    response, details = get_caseviewer_response_from_cache_row(_CachedRow("{not json"))

    assert response is None
    assert details["reason"] == "invalid_payload"


@pytest.mark.parametrize(
    ("cached_response", "reason"),
    [
        ({"cluster": {"absolute_url": "/opinion/123/valid-case/"}}, "missing_case_name"),
        ({"cluster": {"case_name": "Valid Case"}}, "missing_display_fields"),
    ],
)
def test_get_caseviewer_response_from_cache_row_rejects_incomplete_caseviewer_response(cached_response, reason):
    row = _CachedRow({
        "clusterId": 123,
        "caseviewer_synopsis_version": 1,
        "caseviewer_response": {**cached_response, "caseviewer_synopsis_version": 1},
    })

    response, details = get_caseviewer_response_from_cache_row(row)

    assert response is None
    assert details["reason"] == reason
    assert details["has_caseviewer_response"] is True


def _snapshot(**overrides):
    data = {
        "clusterId": 123,
        "cluster_id": 123,
        "id": 123,
        "citation": "123 F.2d 456",
        "case_name": "Valid Case",
        "opinion_id": 456,
        "opinion_url": "https://www.courtlistener.com/api/rest/v4/opinions/456/",
        "docket_url": "https://www.courtlistener.com/api/rest/v4/dockets/44/",
        "llm_holding": "Sensitive holding text should not be logged.",
        "freelaw_url": "https://www.courtlistener.com/opinion/123/valid-case/",
        "court": "9th Cir.",
        "source": "case_view",
    }
    data.update(overrides)
    return data


def _install_upsert_mocks(monkeypatch, *, existing_row=None, commit_side_effect=None):
    from backend.util import freelaw_cache

    created_rows = []

    class FakeQuery:
        def get(self, cluster_id):
            self.last_cluster_id = cluster_id
            return existing_row

    class FakeFreelawClusterCache:
        query = FakeQuery()

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            created_rows.append(self)

    session = SimpleNamespace(
        add=MagicMock(),
        commit=MagicMock(side_effect=commit_side_effect),
        rollback=MagicMock(),
        delete=MagicMock(),
    )
    monkeypatch.setattr(freelaw_cache, "FreelawClusterCache", FakeFreelawClusterCache)
    monkeypatch.setattr(freelaw_cache, "db", SimpleNamespace(session=session))
    return freelaw_cache, created_rows, session


def test_upsert_freelaw_cluster_cache_insert_logs_short_snapshot(monkeypatch, caplog):
    freelaw_cache, created_rows, session = _install_upsert_mocks(monkeypatch)
    caplog.set_level(logging.INFO, logger=freelaw_cache.__name__)

    freelaw_cache.upsert_freelaw_cluster_cache(_snapshot())

    assert len(created_rows) == 1
    assert created_rows[0].cluster_id == 123
    session.commit.assert_called_once()
    assert "freelaw_cluster_cache upserted action=created cluster_id=123" in caplog.text
    assert "source=case_view" in caplog.text
    assert "citation=123 F.2d 456" in caplog.text
    assert "case_name=Valid Case" in caplog.text
    assert "opinion_id=456" in caplog.text
    assert "court=9th Cir." in caplog.text
    assert "has_holding=True" in caplog.text
    assert "has_caseviewer_response=False" in caplog.text
    assert "Sensitive holding text" not in caplog.text


def test_upsert_freelaw_cluster_cache_update_logs_short_snapshot(monkeypatch, caplog):
    existing_row = SimpleNamespace(cluster_id=123, hit_count=2, llm_holding="Existing holding should not be logged.")
    freelaw_cache, created_rows, session = _install_upsert_mocks(monkeypatch, existing_row=existing_row)
    caplog.set_level(logging.INFO, logger=freelaw_cache.__name__)

    freelaw_cache.upsert_freelaw_cluster_cache(_snapshot(llm_holding=None))

    assert created_rows == []
    assert existing_row.hit_count == 3
    assert existing_row.llm_holding == "Existing holding should not be logged."
    session.commit.assert_called_once()
    assert "freelaw_cluster_cache upserted action=updated cluster_id=123" in caplog.text
    assert "has_holding=True" in caplog.text
    assert "has_caseviewer_response=False" in caplog.text
    assert "Existing holding" not in caplog.text


def test_upsert_current_caseviewer_snapshot_does_not_backfill_stale_holding(monkeypatch):
    existing_row = SimpleNamespace(cluster_id=123, hit_count=2, llm_holding="Old holding.")
    freelaw_cache, _created_rows, session = _install_upsert_mocks(monkeypatch, existing_row=existing_row)

    freelaw_cache.upsert_freelaw_cluster_cache(_snapshot(
        llm_holding=None,
        caseviewer_synopsis_version=1,
        caseviewer_response={
            "cluster": {"case_name": "Valid Case"},
            "llm_holding": "",
            "llm_hist": "",
            "caseviewer_synopsis_version": 1,
        },
    ))

    session.commit.assert_called_once()
    assert existing_row.llm_holding is None
    saved_payload = json.loads(existing_row.payload_json)
    assert saved_payload["caseviewer_response"]["llm_holding"] == ""


def test_upsert_freelaw_cluster_cache_caseviewer_response_logs_presence(monkeypatch, caplog):
    freelaw_cache, created_rows, session = _install_upsert_mocks(monkeypatch)
    caplog.set_level(logging.INFO, logger=freelaw_cache.__name__)

    freelaw_cache.upsert_freelaw_cluster_cache(_snapshot(
        cache_schema_version=2,
        caseviewer_response={
            "cluster": {"case_name": "Valid Case"},
            "opinion": {"html_with_citations": "<p>Opinion text</p>"},
            "opinions": [],
            "llm_holding": "Sensitive holding text should not be logged.",
            "llm_hist": "Sensitive history text should not be logged.",
            "casetext": None,
            "freelaw_link": "https://www.courtlistener.com/opinion/123/valid-case/",
        },
    ))

    assert len(created_rows) == 1
    session.commit.assert_called_once()
    assert "freelaw_cluster_cache upserted action=created cluster_id=123" in caplog.text
    assert "has_caseviewer_response=True" in caplog.text
    assert "Sensitive holding text" not in caplog.text
    assert "Sensitive history text" not in caplog.text


def test_upsert_freelaw_cluster_cache_invalid_cluster_id_logs_no_success(monkeypatch, caplog):
    freelaw_cache, _created_rows, session = _install_upsert_mocks(monkeypatch)
    caplog.set_level(logging.INFO, logger=freelaw_cache.__name__)

    freelaw_cache.upsert_freelaw_cluster_cache(_snapshot(clusterId="not-an-id", cluster_id="not-an-id", id="not-an-id"))

    session.add.assert_not_called()
    session.commit.assert_not_called()
    assert "freelaw_cluster_cache upserted" not in caplog.text


def test_upsert_freelaw_cluster_cache_failed_commit_logs_exception_not_success(monkeypatch, caplog):
    freelaw_cache, _created_rows, session = _install_upsert_mocks(
        monkeypatch,
        commit_side_effect=RuntimeError("commit failed"),
    )
    caplog.set_level(logging.INFO, logger=freelaw_cache.__name__)

    freelaw_cache.upsert_freelaw_cluster_cache(_snapshot())

    session.rollback.assert_called_once()
    assert "freelaw_cluster_cache upsert failed cluster_id=123" in caplog.text
    assert "freelaw_cluster_cache upserted" not in caplog.text
