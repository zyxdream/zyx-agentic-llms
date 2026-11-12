import React from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
import AuthorityResearchResult, { AuthoritySourceResult, authorityLink } from './AuthorityResearchResult';

const research = {
  kind: 'authority_research', purpose: 'Research the cited authorities without rewriting the brief.',
  coverage: { complete: false, completed_chunks: ['brief:0'],
    failed_chunks: [{ target_id: 'brief', start: 100, end: 200, error: 'Provider unavailable' }] },
  authorities: [{ id: 'authority-1', kind: 'case', citation_as_written: '123 F.3d 456', case_name: 'Example v. Agency',
    occurrences: [
      { context: 'The brief says the case established notice.', proposition: 'Notice was required.',
        source_location: { file_id: 'brief', block_ids: ['block-2'], location: 'Page 2' } },
      { context: 'The brief also cites it for a remedy.', proposition: 'A remedy was available.',
        source_location: { file_id: 'brief', location: 'Page 5' }, location_uncertain: true },
    ],
    candidates: [
      { source_id: 'source-1', provider: 'courtlistener', status: 200, case_name: 'Different v. Agency',
        court: 'Court of Appeals', date: '2001-01-01', cluster_id: 12,
        url: 'https://www.courtlistener.com/opinion/12/', viewer_url: 'https://www.courtlistener.com/c/12/' },
      { source_id: 'source-2', provider: 'web', case_name: 'Example v. Agency',
        url: 'https://court.example/opinion.pdf' },
    ], checking: { identity: 'not_assessed', opinion_read: true, proposition: 'not_assessed' },
  }],
  sources: [{ id: 'source-2', provider: 'web', title: 'Official opinion', url: 'https://court.example/opinion.pdf',
    passage_artifact_ids: ['passage-1'] }],
  assessments: [{ artifact_id: 'assessment-1', text: 'The reporter citation and drafted name conflict.' }],
};

test('retains separate uses, conflicting candidates, coverage and generated assessments', () => {
  render(<AuthorityResearchResult content={research} onRead={jest.fn()} />);
  const result = screen.getByRole('region', { name: 'Saved authority research' });
  expect(within(result).getByText('Notice was required.')).toBeInTheDocument();
  expect(within(result).getByText('A remedy was available.')).toBeInTheDocument();
  expect(within(result).getByText('Page 2')).toBeInTheDocument();
  expect(within(result).getByText('Page 5')).toBeInTheDocument();
  expect(within(result).getByText(/Provider unavailable/)).toBeInTheDocument();
  expect(within(result).getByRole('link', { name: 'Different v. Agency' })).toHaveAttribute('href', 'https://www.courtlistener.com/c/12/');
  expect(within(result).getByRole('link', { name: 'Example v. Agency' })).toHaveAttribute('href', 'https://court.example/opinion.pdf');
  expect(within(result).getByRole('heading', { name: 'Generated assessments' })).toBeInTheDocument();
  expect(within(result).getByText('The reporter citation and drafted name conflict.')).toBeInTheDocument();
  expect(within(result).getAllByText('not assessed')).toHaveLength(2);
  expect(within(result).queryByText(/invalid|verified/i)).not.toBeInTheDocument();
});

test('source and assessment buttons use saved artifact IDs', () => {
  const onRead = jest.fn();
  render(<AuthorityResearchResult content={research} onRead={onRead} />);
  fireEvent.click(screen.getByRole('button', { name: 'Read source passage 1 — Official opinion' }));
  expect(onRead).toHaveBeenLastCalledWith('passage-1');
  fireEvent.click(screen.getByRole('button', { name: 'Read generated assessment 1' }));
  expect(onRead).toHaveBeenLastCalledWith('assessment-1');
});

test('distinguishes reading a web source from reading a CourtListener opinion', () => {
  const content = { ...research, authorities: [{ ...research.authorities[0],
    checking: { source_read: true, opinion_read: false },
  }] };
  render(<AuthorityResearchResult content={content} />);
  expect(screen.getByText('source read').nextElementSibling).toHaveTextContent('read');
  expect(screen.getByText('CourtListener opinion read').nextElementSibling).toHaveTextContent('not read');
});

test('keeps provider lookup limitations and later conflicting metadata visible', () => {
  render(<AuthorityResearchResult content={{ ...research,
    authorities: [{ ...research.authorities[0], provider_outcomes: [
      { citation: '123 F.3d 456', status: 404, error_message: 'Parsed citation not found in the database.' },
    ] }],
    sources: [{ ...research.sources[0], observations: [
      { case_name: 'Another provider name', date: '2002-02-02', url: 'https://court.example/updated.pdf' },
    ], read_errors: [{ error: 'Source temporarily unavailable.' }] }],
  }} />);
  expect(screen.getByText('Provider lookup outcomes')).toBeInTheDocument();
  expect(screen.getByText('Parsed citation not found in the database.')).toBeInTheDocument();
  expect(screen.getByText('Another provider name')).toBeInTheDocument();
  expect(screen.getByText(/Source temporarily unavailable/)).toBeInTheDocument();
  expect(screen.getAllByRole('link', { name: 'Source website' }).map(link => link.getAttribute('href')))
    .toContain('https://court.example/updated.pdf');
});

test('renders provider and source text literally and rejects unsafe links', () => {
  const text = '<script>window.sourceExecuted = true</script>';
  const { container } = render(<AuthorityResearchResult content={{
    purpose: text, coverage: { complete: true }, authorities: [{
      id: 'a', citation_as_written: text, occurrences: [], candidates: [
        { case_name: 'Unsafe source', url: 'javascript:run()' },
        { case_name: 'File source', url: 'file:///private/source.pdf' },
      ],
    }], sources: [],
  }} />);
  expect(container.querySelector('script')).toBeNull();
  expect(container.querySelector('a')).toBeNull();
  expect(screen.getByText('Unsafe source')).toBeInTheDocument();
  expect(window.sourceExecuted).toBeUndefined();
});

test.each(['javascript:run()', 'data:text/html,source', '//untrusted.example/opinion',
  'https://user:password@example.test/case', 'not a URL'])('does not turn %s into a clickable authority source', value => {
  expect(authorityLink(value)).toBeNull();
});

test('shows actual passage text, opinion attribution and continuation without certifying support', () => {
  const onRead = jest.fn();
  render(<AuthoritySourceResult content={{ kind: 'authority_source', role: 'passage', title: 'Read opinion',
    text: 'Actual source language.\nA second paragraph.', url: 'https://court.example/opinion.pdf',
    opinion_id: 5, opinion_type: 'dissent', locations: [{ page: 3, start: 100, end: 150 }],
    start: 100, end: 150, total_characters: 1000, next_start: 150, source_artifact_id: 'cache-1',
  }} onRead={onRead} />);
  expect(screen.getByRole('region', { name: 'Saved authority passage' })).toBeInTheDocument();
  expect(screen.getByText('Actual source language. A second paragraph.')).toBeInTheDocument();
  expect(screen.getByText('dissent')).toBeInTheDocument();
  expect(screen.getByText(/page: 3/)).toBeInTheDocument();
  expect(screen.getByText('Additional source text is available after character 150.')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Open saved source' }));
  expect(onRead).toHaveBeenCalledWith('cache-1');
  expect(screen.queryByText(/verified/i)).not.toBeInTheDocument();
});

test('source caches are distinguished from passages supplied for reading', () => {
  render(<AuthoritySourceResult content={{ role: 'source_cache', text: 'Retrieved body.' }} />);
  expect(screen.getByRole('region', { name: 'Saved authority source' })).toBeInTheDocument();
  expect(screen.getByText('Saved source text; retrieval alone does not establish legal support.')).toBeInTheDocument();
  expect(screen.queryByText('Source passage supplied for reading.')).not.toBeInTheDocument();
});

test('overview labels retained uses as examples and displays the exact selected scope', () => {
  render(<AuthorityResearchResult content={{ ...research, discovery_mode: 'overview',
    authorities: [{ ...research.authorities[0], occurrences: research.authorities[0].occurrences.map(
      occurrence => ({ ...occurrence, discovery_mode: 'overview', span_verified: true })) }],
    coverage: { ...research.coverage, complete: true, failed_chunks: [], discovery_mode: 'overview',
      scope: [{ target_id: 'brief', filename: 'Selected brief.docx', start: 100, end: 200 }] },
  }} />);
  expect(screen.getByText(/Latest discovery: overview\. Recorded examples may consolidate repeated uses/)).toBeInTheDocument();
  expect(screen.getByText('2 recorded examples and attributed propositions')).toBeInTheDocument();
  const scope = within(screen.getByText('Selected discovery scope').closest('details'));
  expect(scope.getByText('Selected brief.docx')).toBeInTheDocument();
  expect(scope.getByText('100')).toBeInTheDocument();
  expect(scope.getByText('200')).toBeInTheDocument();
  expect(screen.getByText(/Completion describes processing coverage, not a guarantee that every reference was recognized/)).toBeInTheDocument();
  expect(screen.queryByText(/Latest discovery: audit/)).not.toBeInTheDocument();
});

test('audit labels individual occurrences while retaining partial coverage and separate propositions', () => {
  render(<AuthorityResearchResult content={{ ...research, discovery_mode: 'audit',
    authorities: [{ ...research.authorities[0], occurrences: research.authorities[0].occurrences.map(
      occurrence => ({ ...occurrence, discovery_mode: 'audit', span_verified: true })) }],
    coverage: { ...research.coverage, discovery_mode: 'audit',
      scope: [{ target_id: 'brief', start: 0, end: 200 }] },
  }} />);
  expect(screen.getByText('Latest discovery: audit. Every reference occurrence was requested within the selected scope.')).toBeInTheDocument();
  expect(screen.getByText('2 individual occurrences and attributed propositions')).toBeInTheDocument();
  expect(screen.getByText(/Research coverage is partial/)).toBeInTheDocument();
  expect(screen.getByText('Notice was required.')).toBeInTheDocument();
  expect(screen.getByText('A remedy was available.')).toBeInTheDocument();
  expect(screen.queryByText(/The selected discovery scope was processed/)).not.toBeInTheDocument();
});

test('legacy records remain readable without certifying a discovery mode', () => {
  render(<AuthorityResearchResult content={{ ...research,
    coverage: { ...research.coverage, complete: true, failed_chunks: [] },
  }} />);
  expect(screen.getByText('2 saved uses and attributed propositions')).toBeInTheDocument();
  expect(screen.getAllByText('Saved use (discovery mode not recorded).')).toHaveLength(2);
  expect(screen.getAllByText('Exact source anchoring was not recorded for this saved use.')).toHaveLength(2);
  expect(screen.getByText('The selected target text was processed. Source reading and substantive assessment are recorded separately.')).toBeInTheDocument();
  expect(screen.queryByText(/Latest discovery|Individual occurrence \(audit discovery\)|recorded examples/)).not.toBeInTheDocument();
});

test('a narrow later audit does not relabel historical overview examples', () => {
  render(<AuthorityResearchResult content={{ ...research, discovery_mode: 'audit',
    coverage: { complete: true, discovery_mode: 'audit',
      scope: [{ target_id: 'brief', start: 100, end: 200 }] },
    authorities: [{ ...research.authorities[0], occurrences: [
      { ...research.authorities[0].occurrences[0], discovery_mode: 'overview', span_verified: true,
        source_location: { file_id: 'brief', start: 10, end: 20 } },
      { ...research.authorities[0].occurrences[1], discovery_mode: 'audit', span_verified: true,
        source_location: { file_id: 'brief', start: 120, end: 130 } },
    ] }],
  }} />);
  expect(screen.getByText(/Latest discovery: audit/)).toBeInTheDocument();
  expect(screen.getByText('2 saved uses and attributed propositions')).toBeInTheDocument();
  const historicalUse = within(screen.getByText('The brief says the case established notice.').closest('li'));
  expect(historicalUse.getByText('Recorded example (overview discovery).')).toBeInTheDocument();
  expect(historicalUse.queryByText('Individual occurrence (audit discovery).')).not.toBeInTheDocument();
  const auditedUse = within(screen.getByText('The brief also cites it for a remedy.').closest('li'));
  expect(auditedUse.getByText('Individual occurrence (audit discovery).')).toBeInTheDocument();
});

test('a later audit leaves retained legacy uses without an asserted discovery mode', () => {
  render(<AuthorityResearchResult content={{ ...research, discovery_mode: 'audit',
    coverage: { complete: true, discovery_mode: 'audit', scope: [{ target_id: 'other', start: 0, end: 50 }] },
  }} />);
  expect(screen.getByText(/Latest discovery: audit/)).toBeInTheDocument();
  expect(screen.getAllByText('Saved use (discovery mode not recorded).')).toHaveLength(2);
  expect(screen.queryByText('Individual occurrence (audit discovery).')).not.toBeInTheDocument();
  expect(screen.getByText('Notice was required.')).toBeInTheDocument();
  expect(screen.getByText('A remedy was available.')).toBeInTheDocument();
});

test('unanchored provider leads show lookup labels without asserting source wording', () => {
  render(<AuthorityResearchResult content={{ discovery_mode: 'overview',
    authorities: [{ id: 'lead', kind: 'case', citation_as_written: null,
      lookup_citation: '123 US 456', occurrences: [], candidates: [] }],
  }} />);
  expect(screen.getByRole('heading', { name: '123 US 456' })).toBeInTheDocument();
  expect(screen.getByText('Lookup citation').nextElementSibling).toHaveTextContent('123 US 456');
  expect(screen.queryByText(/As written:/)).not.toBeInTheDocument();
  expect(screen.queryByText(/individual occurrences and attributed propositions/)).not.toBeInTheDocument();
});
