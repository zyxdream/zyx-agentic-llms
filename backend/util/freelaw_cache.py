import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.database import db
from backend.models import FreelawClusterCache

logger = logging.getLogger(__name__)


def _normalize_cluster_id(cluster_id: Any) -> int | None:
    try:
        return int(cluster_id)
    except (TypeError, ValueError):
        return None


def _extract_opinion_id(opinion_url: str | None) -> int | None:
    if not opinion_url:
        return None
    match = re.search(r"/opinions/(\d+)/?$", opinion_url)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _snapshot_from_cache_row(cache_row: FreelawClusterCache) -> dict[str, Any] | None:
    if not cache_row or not cache_row.payload_json:
        return None
    try:
        snapshot = json.loads(cache_row.payload_json)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(snapshot, dict):
        return None
    # Links are citation-string-specific; the DB cache is cluster-specific.
    snapshot.pop("href", None)
    return snapshot


def build_freelaw_cluster_cache_snapshot(
    *,
    cluster_id: Any,
    citation: str,
    cluster: dict[str, Any] | None,
    opinion_url: str | None = None,
    docket_url: str | None = None,
    llm_holding: str | None = None,
    freelaw_url: str | None = None,
    court: str | None = None,
    source: str,
) -> dict[str, Any] | None:
    normalized_id = _normalize_cluster_id(cluster_id)
    if normalized_id is None or not isinstance(cluster, dict) or not cluster:
        return None

    meaningful_fields = (
        cluster.get("case_name"),
        cluster.get("absolute_url"),
        cluster.get("docket"),
        cluster.get("sub_opinions"),
        cluster.get("citations"),
    )
    if not any(meaningful_fields):
        return None

    if opinion_url is None:
        sub_opinions = cluster.get("sub_opinions") or []
        opinion_url = sub_opinions[0] if sub_opinions else None

    if docket_url is None:
        docket_url = cluster.get("docket")

    if freelaw_url is None:
        abs_url = cluster.get("absolute_url")
        freelaw_url = f"https://www.courtlistener.com{abs_url}" if abs_url else None

    normalized_holding = llm_holding or None

    return {
        "clusterId": normalized_id,
        "cluster_id": normalized_id,
        "id": normalized_id,
        "citation": citation,
        "case_name": cluster.get("case_name") or "",
        "opinion_id": _extract_opinion_id(opinion_url),
        "opinion_url": opinion_url,
        "docket_url": docket_url,
        "llm_holding": normalized_holding,
        "freelaw_url": freelaw_url,
        "court": court,
        "source": source,
    }


def build_caseviewer_freelaw_cluster_cache_snapshot(
    *,
    cluster_id: Any,
    citation: str,
    response: dict[str, Any],
    opinion_url: str | None = None,
    docket_url: str | None = None,
    court: str | None = None,
    source: str = "case_view",
) -> dict[str, Any] | None:
    if not isinstance(response, dict):
        return None

    cluster = response.get("cluster")
    if not isinstance(cluster, dict) or not cluster:
        return None

    snapshot = build_freelaw_cluster_cache_snapshot(
        cluster_id=cluster_id,
        citation=citation,
        cluster=cluster,
        opinion_url=opinion_url,
        docket_url=docket_url,
        llm_holding=response.get("llm_holding"),
        freelaw_url=response.get("freelaw_link"),
        court=court,
        source=source,
    )
    if not snapshot:
        return None

    snapshot["cache_schema_version"] = 2
    snapshot["llm_hist"] = response.get("llm_hist") or ""
    has_current_synopsis = response.get("caseviewer_synopsis_version") == 1
    if has_current_synopsis:
        snapshot["caseviewer_synopsis_version"] = 1
    snapshot["caseviewer_response"] = {
        "cluster": cluster,
        "opinion": response.get("opinion"),
        "opinions": response.get("opinions") or [],
        "llm_holding": response.get("llm_holding") or "",
        "llm_hist": response.get("llm_hist") or "",
        "casetext": response.get("casetext"),
        "freelaw_link": response.get("freelaw_link"),
    }
    if has_current_synopsis:
        snapshot["caseviewer_response"]["caseviewer_synopsis_version"] = 1
    return snapshot


def _has_cache_display_value(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (dict, list, tuple)):
        return bool(value)
    return value is not None


def _freelaw_link_from_absolute_url(absolute_url: Any) -> str | None:
    if not isinstance(absolute_url, str) or not absolute_url.strip():
        return None
    if absolute_url.startswith("http://") or absolute_url.startswith("https://"):
        return absolute_url
    return f"https://www.courtlistener.com{absolute_url}"


def _caseviewer_response_has_display_fields(response: dict[str, Any]) -> bool:
    cluster = response["cluster"]
    opinion = response.get("opinion")
    opinions = response.get("opinions") or []

    values = [
        response.get("freelaw_link"),
        cluster.get("absolute_url"),
        cluster.get("citations"),
        cluster.get("date_filed"),
        opinions if isinstance(opinions, list) and opinions else None,
    ]
    if isinstance(opinion, dict):
        values.extend([opinion.get("html_with_citations"), opinion.get("html")])

    return any(_has_cache_display_value(value) for value in values)


def _normalize_caseviewer_cached_response(
    response: dict[str, Any],
    snapshot: dict[str, Any],
    cache_row: FreelawClusterCache,
) -> dict[str, Any]:
    cluster = dict(response["cluster"])
    if not cluster.get("court_citation_string") and snapshot.get("court"):
        cluster["court_citation_string"] = snapshot.get("court")

    opinions = response.get("opinions")
    if not isinstance(opinions, list):
        opinions = []

    opinion = response.get("opinion")
    if not isinstance(opinion, dict):
        opinion = opinions[0] if opinions and isinstance(opinions[0], dict) else None

    freelaw_link = response.get("freelaw_link") or snapshot.get("freelaw_url")
    if not freelaw_link:
        freelaw_link = _freelaw_link_from_absolute_url(cluster.get("absolute_url"))

    return {
        "cluster": cluster,
        "opinion": opinion,
        "opinions": opinions,
        "llm_holding": (
            response.get("llm_holding")
            or snapshot.get("llm_holding")
            or getattr(cache_row, "llm_holding", None)
            or ""
        ),
        "llm_hist": response.get("llm_hist") or snapshot.get("llm_hist") or "",
        "caseviewer_synopsis_version": (
            response.get("caseviewer_synopsis_version")
            or snapshot.get("caseviewer_synopsis_version")
        ),
        "casetext": response.get("casetext"),
        "freelaw_link": freelaw_link,
    }


def get_caseviewer_response_from_cache_row(
    cache_row: FreelawClusterCache | None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    details = {
        "reason": "cache_miss",
        "has_caseviewer_response": False,
        "has_holding": bool(getattr(cache_row, "llm_holding", None)),
        "has_hist": False,
    }
    if cache_row is None:
        return None, details

    snapshot = _snapshot_from_cache_row(cache_row)
    if not snapshot:
        details["reason"] = "invalid_payload"
        return None, details

    response = snapshot.get("caseviewer_response")
    details.update({
        "has_caseviewer_response": isinstance(response, dict),
        "has_holding": bool(
            snapshot.get("llm_holding")
            or getattr(cache_row, "llm_holding", None)
            or (response.get("llm_holding") if isinstance(response, dict) else None)
        ),
        "has_hist": bool(
            snapshot.get("llm_hist")
            or (response.get("llm_hist") if isinstance(response, dict) else None)
        ),
    })

    if not isinstance(response, dict):
        details["reason"] = "missing_caseviewer_response"
        return None, details

    cluster = response.get("cluster")
    if not isinstance(cluster, dict) or not cluster:
        details["reason"] = "missing_cluster"
        return None, details
    if not cluster.get("case_name"):
        details["reason"] = "missing_case_name"
        return None, details

    normalized_response = _normalize_caseviewer_cached_response(response, snapshot, cache_row)
    if not _caseviewer_response_has_display_fields(normalized_response):
        details["reason"] = "missing_display_fields"
        return None, details

    if (
        snapshot.get("caseviewer_synopsis_version") != 1
        or response.get("caseviewer_synopsis_version") != 1
    ):
        details["reason"] = "stale_synopsis"
        return None, details

    details["reason"] = "valid"
    details["has_holding"] = bool(normalized_response.get("llm_holding"))
    details["has_hist"] = bool(normalized_response.get("llm_hist"))
    return normalized_response, details


def get_freelaw_cluster_cache(cluster_id: Any) -> FreelawClusterCache | None:
    normalized_id = _normalize_cluster_id(cluster_id)
    if normalized_id is None:
        return None
    try:
        return FreelawClusterCache.query.get(normalized_id)
    except Exception:
        logger.exception("freelaw_cluster_cache read failed cluster_id=%s", cluster_id)
        return None


def record_freelaw_cluster_cache_hit(cache_row: FreelawClusterCache) -> None:
    try:
        cache_row.hit_count = (cache_row.hit_count or 0) + 1
        cache_row.last_accessed_at = datetime.now(timezone.utc)
        db.session.add(cache_row)
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception(
            "freelaw_cluster_cache hit update failed cluster_id=%s",
            getattr(cache_row, "cluster_id", None),
        )


def upsert_freelaw_cluster_cache(snapshot: dict[str, Any]) -> None:
    cluster_id = _normalize_cluster_id(snapshot.get("clusterId") or snapshot.get("cluster_id") or snapshot.get("id"))
    if cluster_id is None:
        return

    now = datetime.now(timezone.utc)
    try:
        cache_row = FreelawClusterCache.query.get(cluster_id)
        action = "created" if cache_row is None else "updated"
        snapshot_to_save = dict(snapshot)
        incoming_holding = snapshot_to_save.get("llm_holding")
        is_current_caseviewer_snapshot = (
            snapshot_to_save.get("caseviewer_synopsis_version") == 1
            and isinstance(snapshot_to_save.get("caseviewer_response"), dict)
        )
        if (
            not incoming_holding
            and not is_current_caseviewer_snapshot
            and cache_row is not None
            and cache_row.llm_holding
        ):
            snapshot_to_save["llm_holding"] = cache_row.llm_holding
            caseviewer_response = snapshot_to_save.get("caseviewer_response")
            if isinstance(caseviewer_response, dict) and not caseviewer_response.get("llm_holding"):
                caseviewer_response["llm_holding"] = cache_row.llm_holding

        payload_json = json.dumps(snapshot_to_save)
        if cache_row is None:
            cache_row = FreelawClusterCache(cluster_id=cluster_id, hit_count=1, created_at=now)
        else:
            cache_row.hit_count = (cache_row.hit_count or 0) + 1

        cache_row.case_name = snapshot_to_save.get("case_name")
        cache_row.opinion_id = snapshot_to_save.get("opinion_id")
        cache_row.opinion_url = snapshot_to_save.get("opinion_url")
        cache_row.docket_url = snapshot_to_save.get("docket_url")
        cache_row.court = snapshot_to_save.get("court")
        cache_row.freelaw_url = snapshot_to_save.get("freelaw_url")
        cache_row.llm_holding = snapshot_to_save.get("llm_holding")
        cache_row.payload_json = payload_json
        cache_row.updated_at = now
        cache_row.last_accessed_at = now
        db.session.add(cache_row)
        db.session.commit()
        logger.info(
            "freelaw_cluster_cache upserted action=%s cluster_id=%s source=%s citation=%s case_name=%s opinion_id=%s court=%s has_holding=%s has_caseviewer_response=%s freelaw_url=%s",
            action,
            cluster_id,
            snapshot_to_save.get("source"),
            snapshot_to_save.get("citation"),
            snapshot_to_save.get("case_name"),
            snapshot_to_save.get("opinion_id"),
            snapshot_to_save.get("court"),
            bool(snapshot_to_save.get("llm_holding")),
            bool(snapshot_to_save.get("caseviewer_response")),
            snapshot_to_save.get("freelaw_url"),
        )
    except Exception:
        db.session.rollback()
        logger.exception("freelaw_cluster_cache upsert failed cluster_id=%s", cluster_id)


def prune_old_freelaw_cluster_cache_rows(cutoff_days: int = 180, max_hit_count: int = 1, batch_size: int = 1000) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=cutoff_days)
    try:
        rows = (
            FreelawClusterCache.query
            .filter(FreelawClusterCache.last_accessed_at < cutoff)
            .filter(FreelawClusterCache.hit_count <= max_hit_count)
            .order_by(FreelawClusterCache.last_accessed_at.asc())
            .limit(batch_size)
            .all()
        )
        count = len(rows)
        for row in rows:
            db.session.delete(row)
        db.session.commit()
        return count
    except Exception:
        db.session.rollback()
        logger.exception("freelaw_cluster_cache prune failed")
        return 0
