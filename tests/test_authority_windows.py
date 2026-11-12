"""Source-preserving extraction windows and individual mention anchors."""
import pytest

from backend.domains.legal import authority_research as research


def mention(window, citation, context, *, index=1, name=None, position=None):
    position = window['text'].index(citation) + window['start'] if position is None else position
    unit = next(unit for unit in window['units'] if unit['start'] <= position < unit['end'])
    return {'kind': 'case', 'citation_as_written': citation, 'case_name': name,
            'context': context, 'proposition': None, 'unit_id': unit['id'], 'occurrence_index': index}


def test_window_defaults_are_explicit_and_empty_input_has_no_windows():
    assert research.EXTRACTION_CHARACTERS == 20_000
    assert research.CONTEXT_CHARACTERS == 1_500
    assert research.CHUNK_PLAN_VERSION == 2
    assert list(research._iter_lookup_windows('')) == []


@pytest.mark.parametrize('text', [
    '  Smith v. Jones, 123 U.S. 456.\n\nId. at 459.  ',
    'État v. X\r\n東京 § 2\r\n\r\nA second paragraph.\r\n',
    'a' * 113,
    'Long wrapped paragraph\n' * 10,
    'Sentence. Another. U.S. 456 and v. Smith. ' * 8,
])
def test_source_units_and_cores_partition_exact_original_without_normalizing(text):
    units = list(research._source_units(text, max_chars=37))
    assert ''.join(unit['text'] for unit in units) == text
    assert units[0]['start'] == 0 and units[-1]['end'] == len(text)
    assert all(left['end'] == right['start'] for left, right in zip(units, units[1:]))
    assert all(unit['text'] == text[unit['start']:unit['end']] and len(unit['text']) <= 37 for unit in units)
    assert units == list(research._source_units(text, max_chars=37))
    assert len({unit['id'] for unit in units}) == len(units)
    windows = list(research._iter_lookup_windows(text, max_chars=37, context_chars=9))
    assert ''.join(text[window['core_start']:window['core_end']] for window in windows) == text
    assert all(left['core_end'] == right['core_start'] for left, right in zip(windows, windows[1:]))
    for window in windows:
        assert window['text'] == text[window['start']:window['end']]
        assert 0 <= window['core_start'] - window['start'] <= 9
        assert 0 <= window['end'] - window['core_end'] <= 9


def test_whole_paragraphs_and_existing_source_blocks_are_preferred_boundaries():
    first, second, third = 'First v. Jones, 1 U.S. 2.\n\n', 'Second paragraph.\n\n', 'Third paragraph.'
    text = first + second + third
    windows = list(research._iter_lookup_windows(text, max_chars=len(first + second), context_chars=5))
    assert windows[0]['core_end'] == len(first + second)
    assert windows[1]['core_start'] == len(first + second)
    short = 'First block. Second block.'
    boundary = len('First block. ')
    blocks = [{'start': 100, 'end': 100 + boundary}, {'start': 100 + boundary, 'end': 100 + len(short)}]
    units = list(research._source_units(short, blocks=blocks, source_start=100))
    assert boundary in {unit['end'] for unit in units}
    assert ''.join(unit['text'] for unit in units) == short


def test_default_cores_do_not_overlap_but_context_does():
    text = 'x' * 45_000
    windows = list(research._iter_lookup_windows(text))
    assert [(item['core_start'], item['core_end']) for item in windows] == [
        (0, 20_000), (20_000, 40_000), (40_000, 45_000)]
    assert [(item['start'], item['end']) for item in windows] == [
        (0, 21_500), (18_500, 41_500), (38_500, 45_000)]


def test_repeated_identical_reference_resolves_only_its_requested_occurrence():
    text = '123 U.S. 456 supports standing; 123 U.S. 456 supports remedies.'
    window = next(research._iter_lookup_windows(text))
    row = mention(window, '123 U.S. 456', text, index=2)
    result = research._resolve_mention(row, text, window)
    assert result['start'] == text.rindex('123 U.S. 456')
    assert text[result['start']:result['end']] == row['citation_as_written']


@pytest.mark.parametrize('change', [
    {'occurrence_index': 0}, {'occurrence_index': 2}, {'occurrence_index': True},
    {'unit_id': 'invented-unit'}, {'context': 'A fabricated context containing 123 U.S. 456.'},
    {'case_name': 'A corrected caption absent from the document'},
    {'citation_as_written': '123 U.S. 999'},
])
def test_bad_or_ambiguous_model_anchors_are_not_saved_as_precise_locations(change):
    text = 'Smith v. Jones, 123 U.S. 456 applies.'
    window = next(research._iter_lookup_windows(text))
    row = mention(window, '123 U.S. 456', text)
    row.update(change)
    with pytest.raises(ValueError):
        research._resolve_mention(row, text, window)


def test_reference_can_extend_into_context_but_belongs_only_to_its_starting_core():
    text = 'Background\nSmith v.\nJones, 123 U.S. 456 supports the point.'
    windows = list(research._iter_lookup_windows(text, max_chars=20, context_chars=100))
    citation = 'Smith v.\nJones, 123 U.S. 456'
    position = text.index(citation)
    owner = next(window for window in windows if window['core_start'] <= position < window['core_end'])
    row = mention(owner, citation, text, position=position)
    resolved = research._resolve_mention(row, text, owner)
    assert resolved['start'] == position and resolved['end'] > owner['core_end']
    assert text[resolved['start']:resolved['end']] == citation
    for window in windows:
        if window is not owner:
            assert research._resolve_mention(row, text, window) is None


def test_a_reference_clipped_by_context_is_a_limitation_instead_of_a_fragment():
    text = 'Smith v.\nJones, 123 U.S. 456 is discussed.'
    window = next(research._iter_lookup_windows(text, max_chars=9, context_chars=2))
    row = {'kind': 'case', 'citation_as_written': 'Smith v.\nJones, 123 U.S. 456',
           'case_name': 'Smith v.\nJones', 'context': text, 'proposition': None,
           'unit_id': window['units'][0]['id'], 'occurrence_index': 1}
    with pytest.raises(ValueError):
        research._resolve_mention(row, text, window)
