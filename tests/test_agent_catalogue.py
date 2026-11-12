"""Catalogue compaction must terminate without losing document identity."""
import copy

import pytest

from backend.util.agent_catalogue import _shrink


@pytest.mark.parametrize('length', [121, 122, 240, 241, 1000])
def test_navigation_text_always_shrinks_including_ellipsis(length):
    card = {'file_id': 'source-1', 'prepared_artifact_id': 'version-1',
            'filename': 'original.xlsx', 'metadata': {'preview': 'x' * length}}
    assert _shrink(card)
    assert len(card['metadata']['preview']) < length
    while _shrink(card):
        pass
    assert len(card['metadata']['preview']) <= 120
    assert card['file_id'] == 'source-1' and card['prepared_artifact_id'] == 'version-1'
    assert card['filename'] == 'original.xlsx'


def test_long_workbook_profile_reaches_a_finite_minimum():
    card = {'file_id': 'source-2', 'prepared_artifact_id': 'version-2',
            'filename': 'ledger.xlsx', 'relative_path': 'Invoices/ledger.xlsx',
            'metadata': {'preview': 'p' * 121, 'summary': 's' * 121, 'headline': 'h' * 121,
                'spreadsheet': {'sheet_count': 12, 'sheets': [
                    {'name': f'Sheet {index}', 'samples': [{'row': 1}],
                     'header_candidates': [{'row': 1}], 'named_tables': [{'name': 'Invoices'}]}
                    for index in range(12)]}}}
    identity = {key: card[key] for key in ('file_id', 'prepared_artifact_id', 'filename', 'relative_path')}
    for _ in range(200):
        before = copy.deepcopy(card)
        if not _shrink(card):
            break
        assert card != before, 'Every successful compaction step must change the card.'
    else:
        pytest.fail('Catalogue compaction failed to terminate')
    assert {key: card[key] for key in identity} == identity
    assert card['metadata']['spreadsheet']['sheets_omitted'] == 11
