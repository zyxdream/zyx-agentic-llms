"""One bounded, identity-preserving document catalogue for agent decisions."""
import copy

from sqlalchemy import func

from backend.database import db
from backend.models import UploadedFiles
from backend.util.document_preparation import validate_snapshot
from backend.util.preparation_summary import token_count

CARD_TOKENS = 1200
CATALOGUE_TOKENS = 64_000


def _shrink(card):
    """Reduce navigation detail, never the accepted identity or source version."""
    metadata = card.get('metadata') or {}
    spreadsheet = metadata.get('spreadsheet') or {}
    for sheet in reversed(spreadsheet.get('sheets', [])):
        for key in ('samples', 'header_candidates', 'named_tables'):
            if sheet.get(key):
                sheet[key] = sheet[key][:-1]
                spreadsheet['preview_truncated'] = True
                return True
    for key in ('key_points', 'topics', 'parties', 'dates'):
        if metadata.get(key):
            metadata[key] = metadata[key][:-1]
            return True
    for key in ('preview', 'summary', 'headline'):
        value = metadata.get(key)
        if isinstance(value, str) and len(value) > 120:
            # Count the ellipsis inside the target length. A 121-character
            # string must shrink too, or an oversized card can loop forever.
            target = max(120, len(value) // 2)
            metadata[key] = value[:target - 1] + '…'
            return True
    if len(spreadsheet.get('sheets', [])) > 1:
        spreadsheet['sheets'] = spreadsheet['sheets'][:-1]
        spreadsheet['sheets_omitted'] = max(0, spreadsheet.get('sheet_count', 0) - len(spreadsheet['sheets']))
        spreadsheet['preview_truncated'] = True
        return True
    return False


def documents(row):
    """Serialize exactly the frozen files, including matter/current distinctions."""
    import json
    snapshot = json.loads(row.data_json)['document_snapshot']
    available = validate_snapshot(row.user_id, row.chat_thread_id, snapshot)
    from backend.util.prepared_retrieval import authorized_artifacts
    authorized_artifacts(snapshot)
    ids = [entry['file_id'] for entry in snapshot['files']]
    lengths = dict(db.session.query(UploadedFiles.unique_id, func.length(UploadedFiles.content))
                   .filter(UploadedFiles.unique_id.in_(ids)).all()) if ids else {}
    result = []
    for entry in snapshot['files']:
        info = available[entry['file_id']]
        metadata = copy.deepcopy(info.get('metadata') or {})
        card = {**entry, 'filename': info.get('filename'), 'relative_path': info.get('relative_path'),
                'metadata': metadata, 'coverage': info.get('coverage'), 'warning': info.get('warning'),
                'characters': lengths.get(entry['file_id']) or 0}
        card['prepared_artifact_id'] = card.pop('artifact_id')
        while token_count(card) > CARD_TOKENS and _shrink(card):
            pass
        result.append(card)
    # Large workbooks must not crowd other file identities out of the first call.
    while token_count(result) > CATALOGUE_TOKENS:
        if not any(_shrink(card) for card in sorted(result, key=token_count, reverse=True)):
            break
    return result
