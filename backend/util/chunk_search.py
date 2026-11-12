"""
Unified chunk search abstraction layer.

Backend is selected based on ENVIRONMENT in backend/constants.py:
  - dev-* (SQLite)   → python_bm25  (in-memory, no FULLTEXT index needed)
  - all others        → mysql_fulltext (server-side BM25 via FULLTEXT index)
  - future            → opensearch (52.70.182.237:9200)

Public API:
    from backend.util.chunk_search import search_chunks
    results = search_chunks(draft_id, keywords, top_k=60, ...)
"""

from backend.constants import ENVIRONMENT

_BACKEND = 'python_bm25' if ENVIRONMENT.startswith('dev-') else 'mysql_fulltext'


def search_chunks(draft_id, keywords, top_k=60, project_id=None,
                  file_scope="all_matter_and_chat", file_unique_ids=None) -> list[dict]:
    """Unified chunk search. Backend selected by ENVIRONMENT in constants.py."""
    if _BACKEND == 'mysql_fulltext':
        return _search_mysql_fulltext(draft_id, keywords, top_k, project_id,
                                      file_scope, file_unique_ids)
    elif _BACKEND == 'opensearch':
        return _search_opensearch(draft_id, keywords, top_k, project_id,
                                  file_scope, file_unique_ids)
    else:
        return _search_python_bm25(draft_id, keywords, top_k, project_id,
                                   file_scope, file_unique_ids)


def _search_mysql_fulltext(draft_id, keywords, top_k, project_id,
                            file_scope, file_unique_ids) -> list[dict]:
    from sqlalchemy import text
    from backend.models import db

    kw_str = ' '.join(str(k) for k in keywords if k)
    if not kw_str.strip():
        return []

    SELECT = """
        SELECT id, uploaded_file_id, chunk_index, content, token_count,
               source_filename, source_relative_path, source_unique_id,
               MATCH(content) AGAINST(:kw IN BOOLEAN MODE) AS bm25_score
        FROM document_chunks
    """
    ORDER_LIMIT = "ORDER BY bm25_score DESC LIMIT :top_k"

    if file_unique_ids:
        placeholders = ', '.join(f':uid{i}' for i in range(len(file_unique_ids)))
        where = f"WHERE source_unique_id IN ({placeholders}) AND MATCH(content) AGAINST(:kw IN BOOLEAN MODE) > 0"
        params = {f'uid{i}': uid for i, uid in enumerate(file_unique_ids)}
    elif file_scope == "all_chat_files":
        where = "WHERE draft_id = :draft_id AND MATCH(content) AGAINST(:kw IN BOOLEAN MODE) > 0"
        params = {'draft_id': draft_id}
    else:  # all_matter_and_chat (default)
        if project_id is not None:
            where = ("WHERE (draft_id = :draft_id OR (project_id = :project_id AND draft_id IS NULL))"
                     " AND MATCH(content) AGAINST(:kw IN BOOLEAN MODE) > 0")
            params = {'draft_id': draft_id, 'project_id': project_id}
        else:
            where = "WHERE draft_id = :draft_id AND MATCH(content) AGAINST(:kw IN BOOLEAN MODE) > 0"
            params = {'draft_id': draft_id}

    params['kw'] = kw_str
    params['top_k'] = top_k
    rows = db.session.execute(text(f"{SELECT} {where} {ORDER_LIMIT}"), params).fetchall()

    # Fallback: if exact file_unique_ids had no FULLTEXT matches, try prefix match
    if not rows and file_unique_ids:
        like_clauses = ' OR '.join(f"source_unique_id LIKE :uid_like{i}"
                                   for i in range(len(file_unique_ids)))
        where2 = f"WHERE ({like_clauses}) AND MATCH(content) AGAINST(:kw IN BOOLEAN MODE) > 0"
        params2 = {f'uid_like{i}': f'{uid}%' for i, uid in enumerate(file_unique_ids)}
        params2['kw'] = kw_str
        params2['top_k'] = top_k
        rows = db.session.execute(text(f"{SELECT} {where2} {ORDER_LIMIT}"), params2).fetchall()

    return [dict(r._mapping) for r in rows]


def _search_python_bm25(draft_id, keywords, top_k, project_id,
                         file_scope, file_unique_ids) -> list[dict]:
    from sqlalchemy import or_
    from backend.models import db, DocumentChunk
    from backend.util.bm25_search import compute_bm25_scores

    if file_unique_ids:
        rows = db.session.query(DocumentChunk).filter(
            DocumentChunk.source_unique_id.in_(file_unique_ids)
        ).all()
        if not rows:
            prefix_conditions = [DocumentChunk.source_unique_id.like(f"{uid}%")
                                  for uid in file_unique_ids]
            rows = db.session.query(DocumentChunk).filter(or_(*prefix_conditions)).all()
    elif file_scope == "recent_chat_files":
        return []
    elif file_scope == "all_chat_files":
        rows = db.session.query(DocumentChunk).filter_by(draft_id=draft_id).all()
    else:
        conditions = [DocumentChunk.draft_id == draft_id]
        if project_id is not None:
            conditions.append(
                (DocumentChunk.project_id == project_id) & (DocumentChunk.draft_id.is_(None))
            )
        rows = db.session.query(DocumentChunk).filter(or_(*conditions)).all()

    if not rows:
        return []

    chunks = [{
        "id": row.id,
        "uploaded_file_id": row.uploaded_file_id,
        "chunk_index": row.chunk_index,
        "content": row.content,
        "token_count": row.token_count,
        "source_filename": row.source_filename,
        "source_relative_path": row.source_relative_path,
        "source_unique_id": row.source_unique_id,
    } for row in rows]

    scored = compute_bm25_scores(chunks, keywords)
    return scored[:top_k]


def _search_opensearch(draft_id, keywords, top_k, project_id,
                        file_scope, file_unique_ids) -> list[dict]:
    # TODO: implement OpenSearch backend
    # OpenSearch instance: 52.70.182.237:9200
    # Target index: "document_chunks"
    # Index document_chunks at upload time (views.py) using the same client pattern as case_index.py
    # Query: multi_match on content field with BM25 scoring, filtered by draft_id/project_id metadata
    raise NotImplementedError("OpenSearch backend not yet implemented")
