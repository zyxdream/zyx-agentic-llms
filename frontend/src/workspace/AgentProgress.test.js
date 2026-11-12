import React from 'react';
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react';
import axios from 'axios';
import AgentProgress from './AgentProgress';
import RequestUserInputPanel from './RequestUserInputPanel';
import { ensureHeaders } from '../auth/useSetHeaders';

jest.mock('axios');
jest.mock('marked', () => jest.requireActual('../../node_modules/marked/lib/marked.umd.js'));
jest.mock('../auth/useSetHeaders', () => ({ ensureHeaders: jest.fn().mockResolvedValue(undefined) }));

beforeEach(() => jest.clearAllMocks());

test('shows completed steps and opens a saved candidate without modifying the editor', async () => {
  axios.get.mockResolvedValue({ data: { kind: 'draft', content: { draft: {
    title: 'Candidate', sections: [{ section_id: '1', section_name: 'Facts', section_content: 'Supported facts.' }],
  } } } });
  render(<AgentProgress workflowId="wf_test" value={{ steps: [
    { id: 's', operation: 'create_draft', status: 'ready', result: { artifact_id: 'a' } },
  ] }} />);
  expect(screen.getByText('create draft — Completed')).toBeTruthy();
  fireEvent.click(screen.getByText('View saved result'));
  expect(await screen.findByText('Supported facts.')).toBeTruthy();
  expect(axios.get).toHaveBeenCalledWith(expect.stringContaining('wf_test/artifacts/a'));
  fireEvent.click(screen.getByText('Close saved result'));
  expect(screen.queryByText('Supported facts.')).toBeNull();
});

test('a saved error observation is an operation error rather than a completed operation', () => {
  render(<AgentProgress workflowId="wf_error" value={{ steps: [
    { id: 's', operation: 'revise_draft', status: 'ready', result: {
      error: true,
      message: 'Use general_revision for a workspace candidate.',
    } },
  ] }} />);
  expect(screen.getByText('revise draft — Operation error')).toBeTruthy();
  expect(screen.getByText('Use general_revision for a workspace candidate.')).toBeTruthy();
  expect(screen.queryByText('revise draft — Completed')).toBeNull();
  expect(screen.queryByText(/recovered/i)).toBeNull();
});

test('reloads a standalone web search in the existing saved-results viewer', async () => {
  const text = 'Generated search synthesis, not directly read source text.\nHearing scheduled — https://court.example/news/hearing';
  axios.get.mockResolvedValue({ data: { id: 'web-result', kind: 'web_search', content: {
    kind: 'web_search', query: 'Recent case developments', text,
    generated_summary: 'A hearing was scheduled.', summary_is_source_text: false,
    results: [{ title: 'Hearing scheduled', url: 'https://court.example/news/hearing' }],
  } } });
  const value = { steps: [{ id: 's', operation: 'web_search', status: 'ready', result: {
    artifact_id: 'web-result', kind: 'web_search',
  } }] };
  const view = render(<AgentProgress workflowId="wf_web" value={value} />);
  expect(screen.getByText('web search — Completed')).toBeInTheDocument();
  fireEvent.click(screen.getByText('View saved result'));
  expect(await screen.findByText(/Generated search synthesis, not directly read source text/)).toBeInTheDocument();
  expect(screen.getByText(/https:\/\/court.example\/news\/hearing/)).toBeInTheDocument();
  expect(axios.get).toHaveBeenLastCalledWith(expect.stringContaining('wf_web/artifacts/web-result'));
  expect(screen.queryByRole('region', { name: 'Saved authority research' })).toBeNull();
  view.unmount();
  render(<AgentProgress workflowId="wf_web" value={value} />);
  fireEvent.click(screen.getByText('View saved result'));
  expect(await screen.findByText(/Generated search synthesis, not directly read source text/)).toBeInTheDocument();
  expect(axios.get).toHaveBeenCalledTimes(2);
});

test('opens saved authority research and reads its source passage through authenticated artifact access', async () => {
  axios.get.mockResolvedValueOnce({ data: { id: 'research-1', kind: 'authority_research', content: {
    kind: 'authority_research', purpose: 'Research the brief authorities.',
    coverage: { complete: false, failed_chunks: [{ error: 'CourtListener unavailable for one range.' }] },
    authorities: [{ id: 'a', citation_as_written: 'Example v. Agency', occurrences: [], candidates: [],
      checking: { identity: 'not_assessed', opinion_read: true, proposition: 'not_assessed' } }],
    sources: [{ id: 'source-1', title: 'Court opinion', url: 'https://court.example/opinion',
      passage_artifact_ids: ['passage-1'] }], assessments: [],
  } } }).mockResolvedValueOnce({ data: { id: 'passage-1', kind: 'authority_source', content: {
    kind: 'authority_source', role: 'passage', title: 'Court opinion',
    url: 'https://court.example/opinion', text: 'Actual retrieved opinion text.', opinion_type: 'lead-opinion',
    locations: [{ page: 2 }],
  } } });
  render(<AgentProgress workflowId="wf_research" value={{ steps: [
    { id: 's', operation: 'lookup_authorities', status: 'ready', result: { artifact_id: 'research-1' } },
  ] }} />);
  fireEvent.click(screen.getByText('View saved result'));
  expect(await screen.findByRole('region', { name: 'Saved authority research' })).toBeInTheDocument();
  expect(screen.getByText(/CourtListener unavailable for one range/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Read source passage 1 — Court opinion' }));
  expect(await screen.findByText('Actual retrieved opinion text.')).toBeInTheDocument();
  expect(screen.getByText('lead-opinion')).toBeInTheDocument();
  expect(axios.get).toHaveBeenLastCalledWith(expect.stringContaining('wf_research/artifacts/passage-1'));
  expect(ensureHeaders).toHaveBeenCalledTimes(2);
});

test('preserves access to saved research checkpoints when an operation was interrupted', async () => {
  axios.get.mockResolvedValue({ data: { id: 'checkpoint', kind: 'authority_research', content: {
    purpose: 'Completed research remains available.', coverage: { complete: false }, authorities: [],
  } } });
  render(<AgentProgress workflowId="wf_cancelled_research" value={{
    steps: [{ id: 's', operation: 'lookup_authorities', status: 'cancelled' }],
    artifacts: [{ id: 'checkpoint', kind: 'authority_research' }],
  }} />);
  fireEvent.click(screen.getByRole('button', { name: 'View saved research 1' }));
  expect(await screen.findByText('Completed research remains available.')).toBeInTheDocument();
  expect(screen.getByText('lookup authorities — cancelled')).toBeInTheDocument();
});

test('refreshing historical steps preserves their error and successful result distinction', async () => {
  axios.get.mockResolvedValue({ data: { info: { agent: { steps: [
    { id: 'failed', operation: 'analyze_material', status: 'ready', result: {
      error: true, message: 'Read original source ranges to obtain a saved material ID.',
    } },
    { id: 'succeeded', operation: 'analyze_material', status: 'ready', result: {
      artifact_id: 'analysis', message: 'Read the saved findings with read_sources.',
    } },
  ] } } } });
  render(<AgentProgress workflowId="wf_history" />);
  fireEvent.click(screen.getByText('Refresh steps'));
  expect(await screen.findByText('analyze material — Operation error')).toBeTruthy();
  expect(screen.getByText('analyze material — Completed')).toBeTruthy();
  expect(screen.getByText('Read original source ranges to obtain a saved material ID.')).toBeTruthy();
  expect(screen.getAllByText('View saved result')).toHaveLength(1);
  expect(axios.get).toHaveBeenCalledWith(expect.stringContaining('runs/wf_history'));
});

test.each(['queued', 'running', 'waiting'])('keeps the %s execution status unchanged', (status) => {
  render(<AgentProgress workflowId="wf_pending" value={{ steps: [
    { id: 's', operation: 'read_sources', status },
  ] }} />);
  expect(screen.getByText(`read sources — ${status}`)).toBeTruthy();
});

test.each(['failed', 'cancelled'])('shows the interrupted %s state and saved reason', (status) => {
  render(<AgentProgress workflowId="wf_interrupted" value={{ steps: [
    { id: 's', operation: 'verify_draft', status, result: { message: 'The worker stopped before verification finished.' } },
  ] }} />);
  expect(screen.getByText(`verify draft — ${status}`)).toBeTruthy();
  expect(screen.getByText('The worker stopped before verification finished.')).toBeTruthy();
  expect(screen.queryByText('verify draft — Completed')).toBeNull();
});

test('citation audit execution errors preserve access to saved substantive findings', async () => {
  axios.get.mockResolvedValue({ data: { kind: 'verification', content: { text: 'Substantive findings remain available.' } } });
  render(<AgentProgress workflowId="wf_audit_error" value={{ steps: [
    { id: 's', operation: 'verify_draft', status: 'ready', result: {
      error: true, artifact_id: 'verification-report',
      message: 'Citation audit unavailable; substantive findings saved.',
    } },
  ] }} />);
  expect(screen.getByText('verify draft — Operation error')).toBeTruthy();
  expect(screen.queryByText('verify draft — Completed')).toBeNull();
  fireEvent.click(screen.getByText('View saved result'));
  expect(await screen.findByText('Substantive findings remain available.')).toBeTruthy();
});

test('a free-text question submits the answer on the existing confirmation callback', () => {
  const submit = jest.fn();
  render(<RequestUserInputPanel request={{ kind: 'agent_question', message: 'Which exhibit?', options: [] }} onSubmit={submit} />);
  fireEvent.change(screen.getByLabelText('Your answer'), { target: { value: 'Exhibit B' } });
  fireEvent.click(screen.getByText('Continue'));
  expect(submit).toHaveBeenCalledWith({ answer: 'Exhibit B' });
});

test('suggested answers can be edited before submitting', () => {
  const submit = jest.fn();
  render(<RequestUserInputPanel request={{ kind: 'agent_question', message: 'Which exhibit?', options: [{ id: '1', label: 'A' }] }} onSubmit={submit} />);
  fireEvent.click(screen.getByText('A'));
  fireEvent.change(screen.getByLabelText('Your answer'), { target: { value: 'A, limited to page 3' } });
  fireEvent.click(screen.getByText('Continue'));
  expect(submit.mock.calls[0][0].answer).toBe('A, limited to page 3');
});

test('history failures remain visible and can be refreshed', async () => {
  axios.get.mockRejectedValue(new Error('Unavailable'));
  render(<AgentProgress workflowId="wf_failed" value={{ steps: [], stopping_reason: 'Context exhausted' }} />);
  expect(screen.getByText('Context exhausted')).toBeTruthy();
  fireEvent.click(screen.getByText('Refresh steps'));
  await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('Could not load saved steps'));
});

test('opens saved Python code, execution feedback and structured results', async () => {
  axios.get.mockResolvedValue({ data: { id: 'execution', kind: 'python_execution', content: {
    status: 'success', code: 'result = {"balance": 1090}', stdout: '28 invoices examined',
    result: { balance: 1090 }, text: 'Analysis saved.',
  } } });
  render(<AgentProgress workflowId="wf_python" value={{ steps: [
    { id: 's', operation: 'python_run', status: 'ready', result: { artifact_id: 'execution' } },
  ] }} />);
  fireEvent.click(screen.getByText('View saved result'));
  expect(await screen.findByText('Python execution — success')).toBeInTheDocument();
  expect(screen.getByText('result = {"balance": 1090}')).toBeInTheDocument();
  expect(screen.getByText('28 invoices examined')).toBeInTheDocument();
  expect(screen.getByText(/^\{\s*"balance": 1090\s*\}$/)).toBeInTheDocument();
});

test('Python errors preserve their traceback for inspection', async () => {
  axios.get.mockResolvedValue({ data: { id: 'execution', kind: 'python_execution', content: {
    status: 'error', code: 'total = table["Amount"].sum()', stderr: 'Column not found', traceback: "KeyError: 'Amount'",
  } } });
  render(<AgentProgress workflowId="wf_python_error" value={{ steps: [
    { id: 's', operation: 'python_run', status: 'ready', result: { artifact_id: 'execution', error: true } },
  ] }} />);
  fireEvent.click(screen.getByText('View saved result'));
  expect(await screen.findByText("KeyError: 'Amount'")).toBeInTheDocument();
  expect(screen.getByText('python run — Operation error')).toBeInTheDocument();
});

test('downloads a derived file through the authenticated artifact route and releases its object URL', async () => {
  const createObjectURL = jest.fn().mockReturnValue('blob:derived-file');
  const revokeObjectURL = jest.fn();
  Object.defineProperty(window.URL, 'createObjectURL', { configurable: true, value: createObjectURL });
  Object.defineProperty(window.URL, 'revokeObjectURL', { configurable: true, value: revokeObjectURL });
  const click = jest.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  const blob = new Blob(['Invoice,Balance\n12,1090'], { type: 'text/csv' });
  axios.get.mockResolvedValueOnce({ data: { id: 'execution', kind: 'python_execution', content: {
    status: 'success', files: [{ id: 'derived', filename: 'balances.csv', download_url: 'https://untrusted.invalid/' }],
  } } }).mockResolvedValueOnce({ data: blob });
  render(<AgentProgress workflowId="wf_download" value={{ steps: [
    { id: 's', operation: 'python_run', status: 'ready', result: { artifact_id: 'execution' } },
  ] }} />);
  fireEvent.click(screen.getByText('View saved result'));
  fireEvent.click(await screen.findByText('Download balances.csv'));
  await waitFor(() => expect(click).toHaveBeenCalledTimes(1));
  expect(ensureHeaders).toHaveBeenCalledTimes(2);
  expect(axios.get).toHaveBeenLastCalledWith(expect.stringContaining('wf_download/artifacts/execution/files/derived'), { responseType: 'blob' });
  expect(createObjectURL).toHaveBeenCalledWith(blob);
  expect(revokeObjectURL).toHaveBeenCalledWith('blob:derived-file');
  expect(document.querySelector('a[download="balances.csv"]')).toBeNull();
  click.mockRestore();
});

test('Refresh replaces a stale supplied running snapshot and new live progress remains current', async () => {
  axios.get.mockResolvedValue({ data: { info: { agent: { steps: [
    { id: 'python', operation: 'python_run', status: 'cancelled', result: { message: 'Request cancelled.' } },
  ] } } } });
  const { rerender } = render(<AgentProgress workflowId="wf_saved" value={{ steps: [
    { id: 'python', operation: 'python_run', status: 'running' },
  ] }} />);
  expect(screen.getByText('python run — running')).toBeInTheDocument();
  fireEvent.click(screen.getByText('Refresh steps'));
  expect(await screen.findByText('python run — cancelled')).toBeInTheDocument();
  expect(screen.queryByText('python run — running')).not.toBeInTheDocument();
  rerender(<AgentProgress workflowId="wf_saved" value={{ steps: [
    { id: 'python', operation: 'python_run', status: 'ready', result: { artifact_id: 'saved-analysis' } },
  ] }} />);
  expect(await screen.findByText('python run — Completed')).toBeInTheDocument();
  expect(screen.getByText('View saved result')).toBeInTheDocument();
});

test('an expanded viewer reloads cancelled steps when confirmed Stop drops stale live progress', async () => {
  axios.get.mockResolvedValue({ data: { info: { agent: { steps: [
    { id: 'read', operation: 'read_sources', status: 'ready', result: { artifact_id: 'saved-source' } },
    { id: 'python', operation: 'python_run', status: 'cancelled' },
  ] } } } });
  const { container, rerender } = render(<AgentProgress workflowId="wf_saved" value={{ steps: [
    { id: 'read', operation: 'read_sources', status: 'ready', result: { artifact_id: 'saved-source' } },
    { id: 'python', operation: 'python_run', status: 'running' },
  ] }} />);
  const details = container.querySelector('details');
  details.open = true;
  fireEvent(details, new Event('toggle'));
  expect(axios.get).not.toHaveBeenCalled();
  rerender(<AgentProgress workflowId="wf_saved" />);
  expect(await screen.findByText('python run — cancelled')).toBeInTheDocument();
  expect(screen.getByText('View saved result')).toBeInTheDocument();
  expect(screen.queryByText('python run — running')).not.toBeInTheDocument();
});

test('a delayed Refresh cannot replace newer completed live progress', async () => {
  let resolveOld;
  axios.get.mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve; }));
  const { rerender } = render(<AgentProgress workflowId="wf_saved" value={{ steps: [
    { id: 'python', operation: 'python_run', status: 'running' },
  ] }} />);
  fireEvent.click(screen.getByText('Refresh steps'));
  await waitFor(() => expect(axios.get).toHaveBeenCalledTimes(1));
  rerender(<AgentProgress workflowId="wf_saved" value={{ steps: [
    { id: 'python', operation: 'python_run', status: 'ready', result: { artifact_id: 'saved-result' } },
  ] }} />);
  await act(async () => resolveOld({ data: { info: { agent: { steps: [
    { id: 'python', operation: 'python_run', status: 'running' },
  ] } } } }));
  expect(screen.getByText('python run — Completed')).toBeInTheDocument();
  expect(screen.getByText('View saved result')).toBeInTheDocument();
  expect(screen.queryByText('python run — running')).not.toBeInTheDocument();
});

test('a delayed older Refresh cannot replace the most recent Refresh result', async () => {
  let resolveOld;
  axios.get.mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve; }))
    .mockResolvedValueOnce({ data: { info: { agent: { steps: [
      { id: 'python', operation: 'python_run', status: 'cancelled' },
    ] } } } });
  render(<AgentProgress workflowId="wf_saved" value={{ steps: [
    { id: 'python', operation: 'python_run', status: 'running' },
  ] }} />);
  fireEvent.click(screen.getByText('Refresh steps'));
  await waitFor(() => expect(axios.get).toHaveBeenCalledTimes(1));
  fireEvent.click(screen.getByText('Refresh steps'));
  expect(await screen.findByText('python run — cancelled')).toBeInTheDocument();
  await act(async () => resolveOld({ data: { info: { agent: { steps: [
    { id: 'python', operation: 'python_run', status: 'running' },
  ] } } } }));
  expect(screen.getByText('python run — cancelled')).toBeInTheDocument();
  expect(screen.queryByText('python run — running')).not.toBeInTheDocument();
});

test('saved Markdown candidates render tables and code read-only without legacy section assumptions', async () => {
  axios.get.mockResolvedValue({ data: { id: 'candidate', kind: 'draft', content: { draft: {
    format_version: 2, source_format: 'markdown', title: 'Saved reconciliation', revision_id: 'candidate',
    markdown: '| Invoice | Balance |\n| --- | ---: |\n| 001 | 120 |\n\n```javascript\nconst balance = 120;\n```',
  } } } });
  render(<AgentProgress workflowId="wf_document" value={{ steps: [
    { id: 's', operation: 'create_draft', status: 'ready', result: { artifact_id: 'candidate' } },
  ] }} />);
  fireEvent.click(screen.getByText('View saved result'));
  expect(await screen.findByText('Saved reconciliation')).toBeInTheDocument();
  expect(screen.getByRole('table')).toBeInTheDocument();
  expect(screen.getByText('const balance = 120;')).toBeInTheDocument();
  expect(screen.queryByLabelText('Download format')).not.toBeInTheDocument();
  expect(screen.queryByText('Manual edit')).not.toBeInTheDocument();
});

test('saved document exports show their source revision and reuse the authenticated document download route', async () => {
  const blob = new Blob(['saved export bytes']);
  axios.get.mockResolvedValue({ data: { id: 'export-1', kind: 'document_export', content: {
    filename: 'Memo.pdf', format: 'pdf', preset: 'court', source_revision_id: 'older-revision',
  } } });
  axios.post.mockResolvedValue({ data: blob, headers: {} });
  const createObjectURL = jest.fn().mockReturnValue('blob:saved-export');
  const revokeObjectURL = jest.fn();
  Object.defineProperty(window.URL, 'createObjectURL', { configurable: true, value: createObjectURL });
  Object.defineProperty(window.URL, 'revokeObjectURL', { configurable: true, value: revokeObjectURL });
  const click = jest.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function () {
    expect(this.download).toBe('Memo.pdf');
  });
  render(<AgentProgress workflowId="wf_document" draftId="chat-public-id" value={{ steps: [], artifacts: [
    { id: 'export-1', kind: 'document_export' },
  ] }} />);
  fireEvent.click(screen.getByText('View document export 1'));
  expect(await screen.findByText('Memo.pdf')).toBeInTheDocument();
  expect(screen.getByText('Format: pdf; layout: court')).toBeInTheDocument();
  expect(screen.getByText('Source revision: older-revision')).toBeInTheDocument();
  fireEvent.click(screen.getByText('Download Memo.pdf'));
  await waitFor(() => expect(click).toHaveBeenCalledTimes(1));
  expect(axios.post).toHaveBeenCalledWith(expect.stringContaining('workspace/documents/chat-public-id/revisions/older-revision/download'), {
    revision_id: 'older-revision', format: 'pdf', preset: 'court',
  }, { responseType: 'blob' });
  expect(ensureHeaders).toHaveBeenCalledTimes(2);
  expect(revokeObjectURL).toHaveBeenCalledWith('blob:saved-export');
  click.mockRestore();
});
