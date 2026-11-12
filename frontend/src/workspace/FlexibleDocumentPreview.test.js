import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import axios from 'axios';
import { marked } from 'marked';
import FlexibleDocumentPreview from './FlexibleDocumentPreview';
import { isMarkdownDraft, renderDocumentMarkdown } from './documentMarkdown';
import { ensureHeaders } from '../auth/useSetHeaders';

jest.mock('marked', () => jest.requireActual('../../node_modules/marked/lib/marked.umd.js'));
jest.mock('axios');
jest.mock('../auth/useSetHeaders', () => ({ ensureHeaders: jest.fn().mockResolvedValue(undefined) }));

const markdown = '# Reconciliation\n\n| Invoice | Balance |\n| --- | ---: |\n| 001 | 120 |\n\n1. Review invoices\n2. Record payments\n\n```javascript\nconst balance = 120;\n```';
const draft = { format_version: 2, source_format: 'markdown', title: 'Reconciliation', markdown,
  revision_id: 'revision-1', parent_revision_id: null, export_settings: { court: {} } };

beforeEach(() => jest.clearAllMocks());

test('recognizes versioned Markdown while leaving legacy section drafts on their existing path', () => {
  expect(isMarkdownDraft(draft)).toBe(true);
  expect(isMarkdownDraft({ title: 'Legacy', sections: [{ section_content: 'Text' }] })).toBe(false);
  expect(isMarkdownDraft({ ...draft, source_format: 'html' })).toBe(false);
});

test('renders readable tables, lists and code examples without manual-edit controls', () => {
  const { container } = render(<FlexibleDocumentPreview draft={draft} draftId="doc-1" />);
  expect(screen.getByRole('table')).toBeInTheDocument();
  expect(screen.getByRole('columnheader', { name: 'Invoice' })).toBeInTheDocument();
  expect(screen.getByRole('cell', { name: '001' })).toBeInTheDocument();
  expect(container.querySelector('ol').children).toHaveLength(2);
  expect(container.querySelector('pre code.language-javascript')).toHaveTextContent('const balance = 120;');
  expect(screen.queryByText('Manual edit')).not.toBeInTheDocument();
  expect(container.querySelector('[contenteditable="true"]')).toBeNull();
});

test('source view preserves the exact canonical Markdown and stays read-only', () => {
  render(<FlexibleDocumentPreview draft={draft} draftId="doc-1" />);
  fireEvent.click(screen.getByRole('button', { name: 'Source' }));
  expect(screen.getByLabelText('Markdown source').textContent).toBe(markdown);
  expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Preview' }));
  expect(screen.getByRole('table')).toBeInTheDocument();
});

test('HTML and JavaScript examples remain text while executable raw markup and unsafe attributes are removed', () => {
  const source = '```html\n<script>window.previewExecuted = true</script>\n```\n\n<script>window.previewExecuted = true</script><img src=x onerror="window.previewExecuted=true"><p onclick="run()">Safe content</p><a href="javascript:run()">Unsupported link</a>';
  const html = renderDocumentMarkdown(source);
  const probe = document.createElement('div');
  probe.innerHTML = html;
  expect(probe.querySelector('script')).toBeNull();
  expect(probe.querySelector('img')).toBeNull();
  expect(probe.querySelector('[onclick]')).toBeNull();
  expect(probe.querySelector('a')).toBeNull();
  expect(probe.querySelector('pre code').textContent).toContain('<script>window.previewExecuted = true</script>');
  expect(probe.textContent).toContain('Safe content');
  expect(probe.textContent).toContain('Unsupported link');
  expect(window.previewExecuted).toBeUndefined();
});

test('keeps safe citation links and table alignment in the sanitized preview', () => {
  const probe = document.createElement('div');
  probe.innerHTML = renderDocumentMarkdown('<a href="https://www.courtlistener.com/c/12/" onclick="run()">Case</a>\n\n| Amount |\n| ---: |\n| 120 |');
  expect(probe.querySelector('a').getAttribute('href')).toBe('https://www.courtlistener.com/c/12/');
  expect(probe.querySelector('a').getAttribute('rel')).toBe('noopener noreferrer');
  expect(probe.querySelector('a').hasAttribute('onclick')).toBe(false);
  expect(probe.querySelector('td').getAttribute('align')).toBe('right');
});

test('document links preserve Markdown semantics independently of global chat/research rendering', () => {
  const previous = { ...marked.defaults };
  const renderer = new marked.Renderer();
  renderer.link = () => 'Research-only label';
  marked.setOptions({ renderer });
  try {
    const probe = document.createElement('div');
    probe.innerHTML = renderDocumentMarkdown('[Source](https://example.test/source)');
    expect(probe.querySelector('a').getAttribute('href')).toBe('https://example.test/source');
    expect(probe.textContent).toBe('Source\n');
  } finally {
    marked.setOptions(previous);
  }
});

test.each([
  ['markdown', 'general'], ['html', 'general'], ['docx', 'court'], ['pdf', 'court'],
])('downloads %s from the exact document revision using the existing authenticated endpoint', async (format, expectedPreset) => {
  const blob = new Blob(['exported bytes']);
  axios.post.mockResolvedValue({ data: blob, headers: { 'content-disposition': 'attachment; filename="requested-file.ext"' } });
  const createObjectURL = jest.fn().mockReturnValue('blob:document');
  const revokeObjectURL = jest.fn();
  Object.defineProperty(window.URL, 'createObjectURL', { configurable: true, value: createObjectURL });
  Object.defineProperty(window.URL, 'revokeObjectURL', { configurable: true, value: revokeObjectURL });
  const click = jest.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function () {
    expect(this.download).toBe('requested-file.ext');
  });
  render(<FlexibleDocumentPreview draft={draft} draftId="doc-1" />);
  fireEvent.change(screen.getByLabelText('Layout'), { target: { value: 'court' } });
  fireEvent.change(screen.getByLabelText('Download format'), { target: { value: format } });
  if (format === 'markdown' || format === 'html') expect(screen.queryByLabelText('Layout')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Download' }));
  await waitFor(() => expect(click).toHaveBeenCalledTimes(1));
  expect(ensureHeaders).toHaveBeenCalledTimes(1);
  expect(axios.post).toHaveBeenCalledWith(expect.stringContaining('workspace/documents/doc-1/revisions/revision-1/download'), {
    revision_id: 'revision-1', format, preset: expectedPreset,
  }, { responseType: 'blob' });
  expect(createObjectURL).toHaveBeenCalledWith(blob);
  expect(revokeObjectURL).toHaveBeenCalledWith('blob:document');
  click.mockRestore();
});

test('surfaces revision conflicts without changing the document or retrying another revision', async () => {
  axios.post.mockRejectedValue({ response: { status: 409, data: { msg: 'The document revision changed. Refresh before downloading.' } } });
  render(<FlexibleDocumentPreview draft={draft} draftId="doc-1" />);
  fireEvent.click(screen.getByRole('button', { name: 'Download' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('The document revision changed. Refresh before downloading.');
  expect(axios.post).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole('button', { name: 'Source' }));
  expect(screen.getByLabelText('Markdown source').textContent).toBe(markdown);
});

test('saved candidates have no download controls and downloads require a saved revision', () => {
  const { rerender } = render(<FlexibleDocumentPreview draft={draft} showExport={false} />);
  expect(screen.queryByLabelText('Download format')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Download' })).not.toBeInTheDocument();
  rerender(<FlexibleDocumentPreview draft={{ ...draft, revision_id: undefined }} draftId="doc-1" />);
  expect(screen.getByRole('button', { name: 'Download' })).toBeDisabled();
  expect(axios.post).not.toHaveBeenCalled();
});
