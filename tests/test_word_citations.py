from backend.word_addin.citations import review_group, short_forms, cornell_link
from backend.word_addin import formatting
import pytest

CONTEXT = 'Sanford v. MemberWorks, Inc., 625 F.3d 550 (9th Cir. 2010).\nKearns v. Ford Motor Co., 567 F.3d 1120 (9th Cir. 2009).'


def verifier(text):
    return {'text': 'Review', 'notes': [], 'findings': [{'title': text, 'anchor': text, 'detail': 'Matched opinion.', 'status': 'verified', 'href': '/CaseViewer/example&12'}]}


def test_short_form_uses_initial_page_from_another_batch():
    text = 'Kearns, 567 F.3d at 1126'
    sent = []
    result = review_group(text, CONTEXT, lambda value: sent.append(value) or verifier(value))
    assert sent == ['Kearns v. Ford Motor Co., 567 F.3d 1120, 1126']
    assert result['findings'][0]['anchor'] == text
    assert result['findings'][0]['status'] == 'verified'


def test_malformed_scanner_title_is_restored_to_exact_short_form():
    text = 'The claim fails as well. Sanford, 625 F.3d at 559'
    result = review_group(text, CONTEXT, verifier)
    assert result['findings'][0]['title'] == 'Sanford, 625 F.3d at 559'


def test_ambiguous_or_absent_antecedent_is_review_not_false_mismatch():
    text = 'Sanford, 625 F.3d at 559'
    for context in ['', CONTEXT + '\nSanford v. Other, 625 F.3d 540 (2010).', CONTEXT + '\nSanford v. Other, 625 F.3d 550 (2010).']:
        seen = []
        result = review_group(text, context, lambda value: seen.append(value) or {'text': 'No result', 'notes': [], 'findings': []})
        assert '625 F.3d at 559' not in seen[0]
        assert result['findings'][0]['status'] == 'review'
        assert result['findings'][0]['href'] == ''
        assert 'does not mean' in result['findings'][0]['detail']


def test_real_verifier_issue_survives_resolution():
    result = review_group('Kearns, 567 F.3d at 1126', CONTEXT, lambda text: {'text': '', 'notes': [], 'findings': [{'title': text, 'anchor': text, 'status': 'issue', 'detail': 'Quotation differs from the opinion.'}]})
    assert result['findings'][0]['status'] == 'issue'
    assert 'Quotation differs' in result['findings'][0]['detail']


def test_statute_links_include_subsection_and_are_not_verification():
    text = 'Under 18 U.S.C. § 1962(c) and 42 U.S.C. § 1983.'
    result = review_group(text, text, lambda _: {'text': '', 'notes': [], 'findings': []})
    assert result['findings'][0]['href'] == 'https://www.law.cornell.edu/uscode/text/18/1962#c'
    assert all(f['status'] == 'review' for f in result['findings'])
    assert cornell_link('18 U.S.C. § 1962(c)(1)') .endswith('#c_1')


def test_bluebook_request_allows_form_changes_but_not_new_numbers():
    request = {'instructions': 'Ensure every citation is properly bluebooked'}
    assert 'For this task only' in formatting.guidance(request)
    formatting.validate('Example, 123 F.3d 456.', 'Example, 123 F.3d 456', request)
    with pytest.raises(ValueError, match='numerical references'):
        formatting.validate('Example, 123 F.3d 456.', 'Example, 123 F.3d 457.', request)
    formatting.validate('556 U.S. 662, 676–679', '556 U.S. 662, 676–79', request)
    formatting.validate('Pages 1198–1203', 'Pages 1198–203', request)
    with pytest.raises(ValueError, match='numerical references'):
        formatting.validate('Pages 676–679', 'Pages 676–80', request)
    assert not formatting.guidance({'instructions': 'Tighten the argument'})


def test_named_supra_resolves_only_one_full_case():
    result = short_forms('Kearns, supra, at 1126', CONTEXT)
    assert result[0][1] == 'Kearns v. Ford Motor Co., 567 F.3d 1120, 1126'
    assert short_forms('Kearns, supra, at 1126', CONTEXT + '\nKearns v. Other, 222 F.3d 100.')[0][1] is None


def test_statute_short_form_uses_document_context_without_guessing_code():
    def guard(_): return {'text': '', 'notes': [], 'findings': []}
    result = review_group('Under § 1962(c)', '18 U.S.C. § 1962(d)', guard)
    assert result['findings'][0]['href'].endswith('/18/1962#c')
    ambiguous = review_group('Under § 1962(c)', '18 U.S.C. § 1962; 42 U.S.C. § 1962', guard)
    assert not ambiguous['findings']
    state = review_group('Under § 1962', '18 U.S.C. § 1962; Civil Code § 1962', guard)
    assert not state['findings']


def test_short_form_expansion_supplies_both_parties_after_surrounding_prose():
    sent = []
    review_group('The Section 1962(d) claim therefore fails as well. Sanford, 625 F.3d at 559', CONTEXT,
                 lambda value: sent.append(value) or verifier(value))
    assert 'Sanford v. MemberWorks, Inc., 625 F.3d 550, 559' in sent[0]


def test_prakashpalan_selection_resolves_with_document_context():
    short = 'Prakashpalan, 223 Cal. App. 4th at 1132'
    context = 'Prakashpalan v. Engstrom, Lipscomb & Lack, 223 Cal.App.4th 1105 (2014).'
    result = review_group(short, context, verifier)
    assert result['findings'][0]['anchor'] == short
    assert result['findings'][0]['status'] == 'verified'


def test_short_lookup_uses_public_citation_only_and_requires_unique_case(monkeypatch):
    from backend.word_addin import research
    from backend.word_addin.citations import SHORT, lookup_short_case
    calls = []
    monkeypatch.setattr(research, 'search_cases', lambda q: calls.append(q) or [{'cluster_id': 12}])
    monkeypatch.setattr(research, '_get', lambda *_: {'case_name': 'Prakashpalan v. Engstrom', 'citations': [{'volume': 223, 'reporter': 'Cal. App. 4th', 'page': '1105'}]})
    short = SHORT.search('Prakashpalan, 223 Cal. App. 4th at 1132')
    assert lookup_short_case(short) == 'Prakashpalan v. Engstrom, 223 Cal. App. 4th 1105, 1132'
    assert calls == ['caseName:("prakashpalan") AND citation:("223 Cal. App. 4th")']
    monkeypatch.setattr(research, 'search_cases', lambda _: [{'cluster_id': 12}, {'cluster_id': 13}])
    assert lookup_short_case(short) is None
    monkeypatch.setattr(research, 'search_cases', lambda _: [{'cluster_id': i} for i in range(5)])
    assert lookup_short_case(short) is None


def test_lookup_is_not_verification_and_is_bounded():
    calls = []
    short = 'Prakashpalan, 223 Cal. App. 4th at 1132'
    result = review_group(short, '', lambda text: {'text': '', 'notes': [], 'findings': [{'title': text, 'status': 'issue', 'detail': 'Quote mismatch'}]},
                          lambda m: calls.append(m[0]) or 'Prakashpalan v. Engstrom, 223 Cal. App. 4th 1105, 1132')
    assert result['findings'][0]['status'] == 'issue'
    assert result['findings'][0]['anchor'] == short
    assert calls == [short]
    calls.clear()
    review_group(' '.join(f'Case{i}, 123 F.3d at 500' for i in range(10)), '', lambda _: {'text': '', 'notes': [], 'findings': []}, lambda m: calls.append(m[0]))
    assert len(calls) == 3


def test_ambiguous_document_reference_is_not_overridden_by_search():
    context = CONTEXT + '\nSanford v. Other, 625 F.3d 540 (2010).'
    result = review_group('Sanford, 625 F.3d at 559', context,
                          lambda _: {'text': '', 'notes': [], 'findings': []},
                          lambda _: pytest.fail('Must not override ambiguous document references'))
    assert result['findings'][0]['status'] == 'review'


def test_resolved_reference_missing_verifier_finding_remains_visible():
    result = review_group('Kearns, 567 F.3d at 1126', CONTEXT,
                          lambda _: {'text': '', 'notes': [], 'findings': []})
    assert result['findings'][0]['status'] == 'review'
    assert 'verifier did not return' in result['findings'][0]['detail']
