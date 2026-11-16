"""Account for identified uses in declared audits without directing research actions."""
import copy
import hashlib
import json
import re
from types import SimpleNamespace

from backend.database import db
from backend.models import AgentArtifact, AgentStep
from backend.util import workspace_service as service


def _key(value):
    return hashlib.sha256(service.encode(value).encode()).hexdigest()[:24]


def _version(meta):
    return {key: meta[key] for key in ('kind', 'file_id', 'prepared_artifact_id',
            'revision_id', 'source_hash') if meta.get(key) is not None}


def start(ops, record, targets):
    selected = [{**_version(meta), **{key: meta[key] for key in
                 ('id', 'filename', 'start', 'end') if key in meta}}
                for meta, _, _ in targets]
    uid = 'review_' + _key({'run_id': ops.row.id, 'targets': selected})
    scopes = record.setdefault('review_scopes', {})
    previous = scopes.get(uid, {})
    scopes[uid] = {**previous, 'id': uid, 'run_id': ops.row.id,
        'sequence': ops.step.sequence, 'revision': previous.get('revision', 0) + 1,
        'targets': selected, 'windows': previous.get('windows', {}),
        'discovery_complete': False, 'limitations': ['Citation discovery has not completed.']}
    return uid


def _located_item(meta, authority, occurrence):
    location = copy.deepcopy(occurrence['source_location'])
    identity = {**_version(meta), 'start': location['start'], 'end': location['end']}
    return {'review_item_id': 'citation_' + _key(identity),
        'citation': occurrence['citation_as_written'], 'kind': authority['kind'],
        'case_name': occurrence.get('case_name_as_used') or authority.get('case_name'),
        'lookup_citation': authority.get('lookup_citation'),
        'proposition': occurrence.get('proposition'), 'context': occurrence.get('context'),
        'source_location': location, 'location_uncertain': bool(occurrence.get('location_uncertain'))}


def record_window(record, scope_id, meta, window, raw, unlocated=()):
    scope = record['review_scopes'][scope_id]
    lo, hi = meta['start'] + window['core_start'], meta['start'] + window['core_end']
    items, located_authorities = {}, set()
    for authority in record['authorities']:
        for occurrence in authority.get('occurrences', []):
            location = occurrence.get('source_location', {})
            if (occurrence.get('target_id') == meta['id'] and occurrence.get('span_verified')
                    and lo <= location.get('start', -1) < hi):
                item = _located_item(meta, authority, occurrence)
                items[item['review_item_id']] = item
                located_authorities.add(authority['id'])
    # Provider offsets are not canonical positions. Keep an unlocated lead tied
    # to the window that produced it instead of inventing an exact appearance.
    for outcome in [*raw, *unlocated]:
        label = (outcome.get('citation_as_written') or outcome.get('citation')
                 or next(iter(outcome.get('normalized_citations') or []), ''))
        if not label:
            continue
        from backend.domains.legal.authority_research import _find_source_phrase
        positions, offset = [], 0
        while (match := _find_source_phrase(window['text'], label, offset)) is not None:
            positions.append(window['start'] + match[0])
            offset = match[1]
        if positions and not any(window['core_start'] <= pos < window['core_end'] for pos in positions):
            continue  # This reference is owned by a neighboring window.
        authority = next((a for a in record['authorities'] if a.get('lookup_citation') == label), None)
        if authority and authority['id'] in located_authorities:
            continue
        identity = {**_version(meta), 'window_start': lo, 'window_end': hi, 'unlocated': label}
        uid = 'citation_' + _key(identity)
        lead = {'review_item_id': uid, 'citation': label, 'kind': outcome.get('kind', 'case'),
            'case_name': outcome.get('case_name'), 'proposition': outcome.get('proposition'),
            'context': outcome.get('context'), 'location_uncertain': True,
            'source_location': {**_version(meta), 'filename': meta['filename'],
                                'start': lo, 'end': hi}}
        if not any(_resolves_lead(lead, found) for found in items.values()
                   if not found.get('location_uncertain')):
            items[uid] = lead
    scope['windows'][_key({'target_id': meta['id'], 'start': lo, 'end': hi})] = list(items.values())
    scope['revision'] += 1


def finish(record, scope_id):
    scope = record['review_scopes'][scope_id]
    coverage = record['coverage']
    limitations = copy.deepcopy(coverage.get('failed_chunks', []))
    for target in coverage.get('targets', []):
        if target.get('target_id') in {t['id'] for t in scope['targets']}:
            if target.get('extraction', {}).get('status') != 'complete':
                limitations.append({'target_id': target['target_id'], 'reason': 'Source extraction is incomplete or unverified.'})
    if any(item.get('location_uncertain') for item in _items(scope)):
        limitations.append({'reason': 'Some identified references have no confirmed original-text location.'})
    scope.update(discovery_complete=bool(coverage.get('complete')) and not limitations,
                 limitations=limitations, revision=scope['revision'] + 1)


def _items(scope):
    return [item for values in scope.get('windows', {}).values() for item in values]


def _describe(scopes):
    items, limitations = {}, []
    for scope in scopes:
        for item in _items(scope):
            items[item['review_item_id']] = copy.deepcopy(item)
        if not scope.get('discovery_complete'):
            limitations.append({'review_scope_id': scope['id'], 'targets': scope['targets'],
                                'details': scope.get('limitations') or ['Citation discovery is incomplete.']})
    return {'review_items': list(items.values()), 'review_limitations': limitations,
            'review_scope_ids': [scope['id'] for scope in scopes]}


def describe(record, run_id):
    return _describe([scope for scope in record.get('review_scopes', {}).values()
                      if scope.get('run_id') == run_id])


def _legacy_scope(record, row):
    """Derive items without rewriting artifacts created before this feature."""
    coverage = record.get('coverage', {})
    if coverage.get('discovery_mode', record.get('discovery_mode')) != 'audit':
        return None
    targets = {t['id']: t for t in record.get('targets', [])}
    selected = coverage.get('scope', [])
    if not selected:
        return None
    items = {}
    for authority in record.get('authorities', []):
        for occurrence in authority.get('occurrences', []):
            meta = targets.get(occurrence.get('target_id'))
            if not meta:
                continue
            position = occurrence.get('source_location', {}).get('start', -1)
            if any(s['target_id'] == meta['id'] and s['start'] <= position < s['end'] for s in selected):
                item = _located_item(meta, authority, occurrence)
                items[item['review_item_id']] = item
    return {'id': 'legacy_' + _key({'run': row.id, 'scope': selected}), 'run_id': row.id,
            'sequence': -1, 'revision': 0, 'targets': selected, 'windows': {'legacy': list(items.values())},
            'discovery_complete': bool(coverage.get('complete')),
            'limitations': copy.deepcopy(coverage.get('failed_chunks', []))}


def _resolves_lead(lead, located):
    origin, found = lead['source_location'], located['source_location']
    identity = lambda location: {key: value for key, value in _version(location).items() if key != 'kind'}
    if identity(origin) != identity(found) or not origin['start'] <= found['start'] < origin['end']:
        return False
    label = ' '.join(lead['citation'].split()).casefold()
    return any(re.search(r'(?<!\w)' + re.escape(label) + r'(?!\w)', ' '.join(value.split()).casefold())
               for value in (located['citation'], located.get('lookup_citation')) if value)


def for_run(row):
    # Only this request's declarations create obligations. Borrowed earlier
    # research and later ordinary searches do not create new review scopes.
    scopes, identified = {}, {}
    rows = AgentArtifact.query.filter_by(run_id=row.id, kind='authority_research').order_by(
        AgentArtifact.created_at, AgentArtifact.id).all()
    for saved in rows:
        record = json.loads(saved.content_json)
        values = record.get('review_scopes', {})
        step = db.session.get(AgentStep, saved.step_id)
        candidates = (list(values.values()) if values else
                      [_legacy_scope(record, row)] if step and step.name == 'lookup_authorities' else [])
        for scope in candidates:
            if not scope or scope.get('run_id') != row.id:
                continue
            known = identified.setdefault(scope['id'], {})
            for item in _items(scope):
                known[item['review_item_id']] = copy.deepcopy(item)
            old = scopes.get(scope['id'])
            if old is None or (scope['sequence'], scope['revision']) >= (old['sequence'], old['revision']):
                scopes[scope['id']] = scope
    required = []
    for uid, scope in scopes.items():
        scope = copy.deepcopy(scope)
        known = list(identified[uid].values())
        located = [item for item in known if not item.get('location_uncertain')]
        retained = [item for item in known if not item.get('location_uncertain')
                    or not any(_resolves_lead(item, found) for found in located)]
        # Replace the window projection with the accumulated items, so a
        # superseded unlocated lead is not reintroduced from an old window.
        scope['windows'] = {'previously_identified': retained}
        if any(item.get('location_uncertain') for item in retained):
            scope['discovery_complete'] = False
            reason = {'reason': 'Some identified references have no confirmed original-text location.'}
            if reason not in scope['limitations']:
                scope['limitations'].append(reason)
        required.append(scope)
    return _describe(required)


def preflight(row, supplied):
    from backend.domains.legal import authority_research as research
    coverage = for_run(row)
    expected = {item['review_item_id']: item for item in coverage['review_items']}
    outcomes = {}
    for result in supplied:
        value = result.model_dump() if hasattr(result, 'model_dump') else dict(result)
        uid = value['review_item_id']
        if uid in outcomes:
            raise ValueError('Duplicate citation review outcome: ' + uid)
        if uid not in expected:
            raise ValueError('Citation review item is outside this request\'s audit scopes: ' + uid)
        if expected[uid].get('location_uncertain') and value['status'] != 'unresolved':
            raise ValueError('A citation with an unconfirmed source location must remain unresolved: ' + uid)
        for evidence_id in value['evidence_ids']:
            saved = research._owned_row(row, evidence_id)
            if saved.kind not in {'source', 'authority_source', 'authority_research'}:
                raise ValueError('Citation evidence must reference saved original material or research, not a generated draft/assessment.')
            if saved.run_id != row.id:
                research.resolve_artifact(SimpleNamespace(row=row), evidence_id)
        outcomes[uid] = value
    missing = [item for uid, item in expected.items() if uid not in outcomes]
    if missing:
        raise ValueError('Citation review has unaccounted items. Investigate them or explicitly mark them unresolved. '
                         'Resubmit complete_task with outcomes for ALL review_items, including earlier entries. '
                         + service.encode({'missing_review_items': missing, 'review_limitations': coverage['review_limitations']}))
    return {**coverage, 'results': [outcomes[uid] for uid in expected]}


def _cell(value):
    return re.sub(r'([\\|`*_{}\[\]()!#<>])', r'\\\1', ' '.join(str(value or '').split()))


def render(coverage):
    if not coverage['review_scope_ids']:
        return ''
    lines = ['\n\n### Citation review coverage',
        'Assessed means a finding was recorded, not that the citation passed every check. '
        'These outcomes refer to the audited source versions; they do not certify a later rewrite. '
        'Reporting every identified item does not prove citation discovery was exhaustive.']
    if coverage['review_limitations']:
        lines.append('**Citation discovery is incomplete.** The table accounts for identified items only; '
                     'additional citations may remain unidentified.')
        for gap in coverage['review_limitations']:
            for target in gap['targets']:
                lines.append('- ' + _cell(target.get('filename')) + ': selected source characters '
                             + str(target['start']) + '–' + str(target['end']) + '; discovery incomplete.')
    if not coverage['review_items']:
        lines.append('No citation uses were identified in the declared audit scope. This is not proof that none exist.')
        return '\n'.join(lines)
    lines += ['', '| Source | Citation | Status | Finding / remaining work |', '| --- | --- | --- | --- |']
    outcomes = {r['review_item_id']: r for r in coverage['results']}
    for item in coverage['review_items']:
        location = item['source_location']
        source = location.get('filename', 'Selected source')
        if item.get('location_uncertain'):
            source += ' — exact location unconfirmed'
        elif location.get('page') is not None:
            source += ', PDF p. ' + str(location['page'])
        else:
            source += ', ' + (location.get('location') or f"characters {location['start']}–{location['end']}")
        value = outcomes[item['review_item_id']]
        citation = item['citation']
        if item.get('case_name') and item['case_name'] not in citation:
            citation = item['case_name'] + ' — ' + citation
        lines.append('| ' + ' | '.join(_cell(v) for v in (source, citation, value['status'], value['finding'])) + ' |')
    return '\n'.join(lines)
