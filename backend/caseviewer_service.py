import logging
from typing import Any

from backend.database import db
from backend.everything import load_state_from_memory, save_state_to_memory
from backend.models import ChatThread
from backend.util.db import DBMemory
from backend.util.freelaw_cache import (
    build_caseviewer_freelaw_cluster_cache_snapshot,
    get_caseviewer_response_from_cache_row,
    get_freelaw_cluster_cache,
    record_freelaw_cluster_cache_hit,
    upsert_freelaw_cluster_cache,
)
from backend.util.free_law_tools import (
    extract_holding_and_case_overview_from_opinion,
    get_cluster,
    get_cluster_opinions,
    get_court,
    get_docket,
)

logger = logging.getLogger(__name__)

CASEVIEWER_ACTIVE_TASK_TTL_SECONDS = 900


def caseviewer_no_details_response(
    *,
    cluster_id: Any = None,
    citation: str | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    identifier = cluster_id or citation or ""
    error_message = message or f"No details found for citation {identifier}".strip()
    return {
        "error": True,
        "not_found": True,
        "error_message": error_message,
        "cluster": None,
        "opinion": None,
        "opinions": [],
        "llm_holding": "",
        "llm_hist": "",
        "casetext": None,
        "freelaw_link": None,
    }


def caseviewer_active_task_key(user_id: Any, draft_id: str | None, cluster_id: Any) -> str:
    draft_part = draft_id or "no-draft"
    return f"caseviewer:active:{user_id}:{draft_part}:{cluster_id}"


def update_caseviewer_progress(task: Any, message: str, progress: int) -> None:
    if task is None:
        return
    try:
        task.update_state(
            state="PROGRESS",
            meta={"message": message, "status": message, "progress": progress},
        )
    except Exception:
        logger.exception("caseviewer progress update failed message=%s progress=%s", message, progress)


def load_cached_caseviewer_response(
    cluster_id: Any,
) -> tuple[dict[str, Any] | None, dict[str, Any], Any]:
    cache_row = get_freelaw_cluster_cache(cluster_id)
    cached_response, cache_details = get_caseviewer_response_from_cache_row(cache_row)
    if cached_response:
        logger.info(
            "caseviewer freelaw cache hit cluster_id=%s reason=%s has_caseviewer_response=%s has_holding=%s has_hist=%s",
            cluster_id,
            cache_details.get("reason"),
            cache_details.get("has_caseviewer_response"),
            cache_details.get("has_holding"),
            cache_details.get("has_hist"),
        )
        record_freelaw_cluster_cache_hit(cache_row)
    else:
        logger.info(
            "caseviewer freelaw cache %s cluster_id=%s reason=%s has_caseviewer_response=%s has_holding=%s has_hist=%s",
            "miss" if cache_row is None else "fallback",
            cluster_id,
            cache_details.get("reason"),
            cache_details.get("has_caseviewer_response"),
            cache_details.get("has_holding"),
            cache_details.get("has_hist"),
        )
    return cached_response, cache_details, cache_row


def build_live_caseviewer_payload(
    *,
    citation_name_decoded: str,
    cluster_id: Any,
    task: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not cluster_id:
        logger.info("caseviewer live lookup skipped reason=missing_cluster_id citation=%s", citation_name_decoded)
        return caseviewer_no_details_response(cluster_id=cluster_id, citation=citation_name_decoded), {}

    update_caseviewer_progress(task, "Fetching case cluster...", 20)
    logger.info("caseviewer live fetch cluster_id=%s citation=%s", cluster_id, citation_name_decoded)
    cluster = get_cluster(cluster_id)
    if not cluster:
        logger.info("caseviewer live no cluster cluster_id=%s citation=%s", cluster_id, citation_name_decoded)
        return caseviewer_no_details_response(cluster_id=cluster_id, citation=citation_name_decoded), {}

    court_cit_str = None
    docket_url = cluster.get("docket")
    if docket_url:
        update_caseviewer_progress(task, "Fetching court and docket details...", 35)
        logger.info("caseviewer live fetch docket cluster_id=%s", cluster_id)
        docket_raw = get_docket(docket_url)
        if docket_raw:
            court_url = docket_raw.get("court")
            if court_url:
                court_raw = get_court(court_url)
                if court_raw:
                    court_cit_str = court_raw.get("citation_string")
                    logger.info("caseviewer live court loaded cluster_id=%s court=%s", cluster_id, court_cit_str)

    absolute_url = cluster.get("absolute_url")
    freelaw_link = f"https://www.courtlistener.com{absolute_url}" if absolute_url else None
    if freelaw_link:
        logger.info("caseviewer live freelaw link cluster_id=%s freelaw_url=%s", cluster_id, freelaw_link)

    opinions = []
    opinion = None
    opinion_url = None
    llm_holding = ""
    llm_hist = ""
    synopsis_failed = False
    sub_opinions = cluster.get("sub_opinions") or []
    logger.info("caseviewer live opinions listed cluster_id=%s count=%s", cluster_id, len(sub_opinions))
    if sub_opinions:
        opinion_url = sub_opinions[0]
        update_caseviewer_progress(task, "Fetching opinions...", 50)
        opinions = get_cluster_opinions(cluster) or []
        opinion = opinions[0] if opinions else None
        if opinion:
            update_caseviewer_progress(task, "Extracting holding and case overview...", 70)
            logger.info("caseviewer live synopsis start cluster_id=%s opinion_url=%s", cluster_id, opinion_url)
            synopsis = extract_holding_and_case_overview_from_opinion(opinion)
            if synopsis is None:
                synopsis_failed = True
            else:
                llm_holding = synopsis.holding or ""
                llm_hist = synopsis.case_overview or ""
            logger.info(
                "caseviewer live synopsis done cluster_id=%s has_holding=%s has_overview=%s",
                cluster_id,
                bool(llm_holding),
                bool(llm_hist),
            )

    cluster["court_citation_string"] = court_cit_str
    response = {
        "cluster": cluster,
        "llm_holding": llm_holding,
        "opinion": opinion,
        "opinions": opinions,
        "llm_hist": llm_hist,
        "caseviewer_synopsis_version": 1,
        "casetext": None,
        "freelaw_link": freelaw_link,
    }
    context = {
        "opinion_url": opinion_url,
        "docket_url": docket_url,
        "court": court_cit_str,
        "synopsis_failed": synopsis_failed,
    }
    return response, context


def save_caseviewer_global_cache(
    *,
    cluster_id: Any,
    citation_name_decoded: str,
    response: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> bool:
    if not isinstance(response, dict) or response.get("error"):
        return False

    cluster = response.get("cluster")
    if not isinstance(cluster, dict) or not cluster:
        return False
    if not cluster.get("case_name"):
        return False

    context = context or {}
    if context.get("synopsis_failed"):
        logger.info("caseviewer global cache skipped cluster_id=%s reason=synopsis_failed", cluster_id)
        return False
    snapshot = build_caseviewer_freelaw_cluster_cache_snapshot(
        cluster_id=cluster_id,
        citation=citation_name_decoded,
        response=response,
        opinion_url=context.get("opinion_url"),
        docket_url=context.get("docket_url"),
        court=context.get("court"),
        source="case_view",
    )
    if not snapshot:
        logger.info("caseviewer global cache skipped cluster_id=%s reason=empty_snapshot", cluster_id)
        return False

    upsert_freelaw_cluster_cache(snapshot)
    logger.info(
        "caseviewer global cache save requested cluster_id=%s citation=%s case_name=%s has_holding=%s has_hist=%s",
        cluster_id,
        citation_name_decoded,
        snapshot.get("case_name"),
        bool(snapshot.get("llm_holding")),
        bool(snapshot.get("llm_hist")),
    )
    return True


def save_caseviewer_validated_case(
    *,
    draft_id: str | None,
    user_id: Any,
    cluster_id: Any,
    citation_name_decoded: str,
    response: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> bool:
    if not draft_id:
        return False
    if not isinstance(response, dict) or response.get("error"):
        return False

    cluster = response.get("cluster")
    if not isinstance(cluster, dict) or not cluster:
        return False

    llm_holding = response.get("llm_holding") or ""
    llm_hist_raw = response.get("llm_hist") or ""
    if not (llm_holding or llm_hist_raw):
        return False

    context = context or {}
    try:
        doc_id = ChatThread.resolve_id(draft_id)
        draft = ChatThread.query.get(doc_id)
        if not draft:
            logger.info("caseviewer per-draft cache skipped reason=draft_not_found draft_id=%s", draft_id)
            return False
        if user_id != draft.parent.userId:
            logger.info(
                "caseviewer per-draft cache skipped reason=wrong_owner draft_id=%s user_id=%s",
                draft_id,
                user_id,
            )
            return False

        mem = DBMemory(db=db, doc_id=draft.id)
        state = load_state_from_memory(mem)
        validated_cases = state.setdefault("validated_cases", {})
        entry = validated_cases.get(
            citation_name_decoded,
            {
                "citation": citation_name_decoded,
                "clusterId": cluster_id,
                "cluster_id": cluster_id,
                "id": cluster_id,
            },
        )
        entry.update({
            "llm_holding": llm_holding,
            "llm_hist": llm_hist_raw,
            "court": context.get("court"),
            "freelaw_url": response.get("freelaw_link"),
            "source": "case_view",
        })
        if cluster.get("case_name"):
            entry["case_name"] = cluster["case_name"]
        validated_cases[citation_name_decoded] = entry
        from backend.util.workspace_store import patch_auxiliary
        patch_auxiliary(draft_id, {'validated_cases': {citation_name_decoded: entry}},
                        merge=('validated_cases',))
        db.session.commit()
        logger.info(
            "caseviewer per-draft cache saved draft_id=%s cluster_id=%s citation=%s has_holding=%s has_hist=%s",
            draft_id,
            cluster_id,
            citation_name_decoded,
            bool(llm_holding),
            bool(llm_hist_raw),
        )
        return True
    except Exception:
        logger.exception(
            "caseviewer per-draft cache save failed draft_id=%s cluster_id=%s citation=%s",
            draft_id,
            cluster_id,
            citation_name_decoded,
        )
        return False
