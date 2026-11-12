import logging
from typing import Any

import redis as redis_lib
from billiard.exceptions import SoftTimeLimitExceeded
from celery import shared_task

from backend.caseviewer_service import (
    caseviewer_no_details_response,
    load_cached_caseviewer_response,
    build_live_caseviewer_payload,
    save_caseviewer_global_cache,
    save_caseviewer_validated_case,
    update_caseviewer_progress,
)
from backend.constants import REDIS_URL
from backend.util.account_access import AccountUnavailable, require_enabled_account

logger = logging.getLogger(__name__)

_caseviewer_redis = redis_lib.from_url(REDIS_URL)


def run_caseviewer_enrichment(payload: dict[str, Any], task: Any = None) -> dict[str, Any]:
    cluster_id = payload.get("cluster_id")
    citation_name_decoded = payload.get("citation_name_decoded") or payload.get("citation") or ""
    draft_id = payload.get("draft_id")
    user_id = payload.get("user_id")
    active_task_key = payload.get("active_task_key")
    task_id = getattr(getattr(task, "request", None), "id", None)

    try:
        require_enabled_account(user_id)
        logger.info(
            "caseviewer task start task_id=%s cluster_id=%s draft_id=%s user_id=%s",
            task_id,
            cluster_id,
            draft_id,
            user_id,
        )
        update_caseviewer_progress(task, "Checking saved case details...", 5)
        cached_response, cache_details, _cache_row = load_cached_caseviewer_response(cluster_id)
        if cached_response:
            logger.info(
                "caseviewer task cache hit task_id=%s cluster_id=%s reason=%s has_holding=%s has_hist=%s",
                task_id,
                cluster_id,
                cache_details.get("reason"),
                bool(cached_response.get("llm_holding")),
                bool(cached_response.get("llm_hist")),
            )
            update_caseviewer_progress(task, "Case details ready.", 100)
            return cached_response

        response, context = build_live_caseviewer_payload(
            citation_name_decoded=citation_name_decoded,
            cluster_id=cluster_id,
            task=task,
        )
        if response.get("error") or not response.get("cluster"):
            logger.info("caseviewer task no details task_id=%s cluster_id=%s", task_id, cluster_id)
            return response

        # An enabled account can be disabled while remote case details load.
        require_enabled_account(user_id)
        update_caseviewer_progress(task, "Saving case details...", 95)
        save_caseviewer_global_cache(
            cluster_id=cluster_id,
            citation_name_decoded=citation_name_decoded,
            response=response,
            context=context,
        )
        save_caseviewer_validated_case(
            draft_id=draft_id,
            user_id=user_id,
            cluster_id=cluster_id,
            citation_name_decoded=citation_name_decoded,
            response=response,
            context=context,
        )
        update_caseviewer_progress(task, "Case details ready.", 100)
        logger.info("caseviewer task done task_id=%s cluster_id=%s", task_id, cluster_id)
        return response
    except AccountUnavailable as exc:
        return caseviewer_no_details_response(
            cluster_id=cluster_id,
            citation=citation_name_decoded,
            message=str(exc),
        )
    except SoftTimeLimitExceeded:
        logger.exception("caseviewer task soft timeout task_id=%s cluster_id=%s", task_id, cluster_id)
        return caseviewer_no_details_response(
            cluster_id=cluster_id,
            citation=citation_name_decoded,
            message="Case details are taking too long to load. Please try again.",
        )
    except Exception:
        logger.exception("caseviewer task failed task_id=%s cluster_id=%s", task_id, cluster_id)
        return caseviewer_no_details_response(
            cluster_id=cluster_id,
            citation=citation_name_decoded,
            message="Unable to load case details. Please try again.",
        )
    finally:
        if active_task_key:
            try:
                _caseviewer_redis.delete(active_task_key)
            except Exception:
                logger.exception(
                    "caseviewer active task cleanup failed task_id=%s cluster_id=%s",
                    task_id,
                    cluster_id,
                )


@shared_task(bind=True, ignore_result=False, soft_time_limit=300, time_limit=360)
def caseviewer_enrichment_task(self, payload: dict[str, Any]) -> dict[str, Any]:
    return run_caseviewer_enrichment(payload, task=self)
