import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import axios from 'axios';
import PreparedDocumentSummary from './PreparedDocumentSummary';
jest.mock('axios');
beforeEach(() => jest.clearAllMocks());
jest.mock('../auth/useSetHeaders', () => ({ ensureHeaders: jest.fn().mockResolvedValue() }));

test('loads the detailed artifact only when expanded and reuses it on reopen', async () => {
  axios.get.mockResolvedValue({ data: { detailed_digest: 'Four sentences. Parties. Duties. Exceptions.', section_outline: [] } });
  const { container } = render(<PreparedDocumentSummary documentId="doc" artifactId="artifact"
    filename="same-name.pdf" metadata={{ headline: 'Payment agreement', preview: 'Compact overview.' }} />);
  expect(axios.get).not.toHaveBeenCalled();
  const details = container.querySelector('details');
  details.open = true;
  fireEvent(details, new Event('toggle'));
  await waitFor(() => expect(screen.getByText(/Duties. Exceptions./)).toBeInTheDocument());
  expect(axios.get.mock.calls[0][1].params.artifactId).toBe('artifact');
  details.open = false; fireEvent(details, new Event('toggle'));
  details.open = true; fireEvent(details, new Event('toggle'));
  expect(axios.get).toHaveBeenCalledTimes(1);
});

test('an old document response cannot replace the newly selected summary', async () => {
  let resolveOld;
  axios.get.mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve; }));
  const { container, rerender } = render(<PreparedDocumentSummary documentId="old" artifactId="old-artifact" filename="Old.pdf" />);
  container.querySelector('details').open = true;
  fireEvent(container.querySelector('details'), new Event('toggle'));
  await waitFor(() => expect(axios.get).toHaveBeenCalled());
  rerender(<PreparedDocumentSummary documentId="new" artifactId="new-artifact" filename="New.pdf" />);
  await act(async () => resolveOld({ data: { detailed_digest: 'STALE SECRET' } }));
  expect(screen.queryByText('STALE SECRET')).not.toBeInTheDocument();
});

test('shows sheet structure, source row locations and samples before loading a detailed summary', () => {
  render(<PreparedDocumentSummary documentId="workbook" artifactId="prepared-workbook" filename="Invoices.xlsx"
    metadata={{ spreadsheet: { format: 'xlsx', sheet_count: 2, sheets: [{
      name: 'Invoices', state: 'hidden', used_range: 'A3:B20', row_count: 18, column_count: 2,
      formula_count: 3, missing_formula_cache_count: 1,
      header_candidates: [{ row: 3, cells: [{ cell: 'A3', value: 'Invoice' }, { cell: 'B3', value: 'Amount' }] }],
      samples: [{ row: 4, cells: [{ cell: 'A4', value: '0012' }, { cell: 'B4', value: 120 }] }],
    }] } }} />);
  expect(screen.getByText(/2 sheet\(s\)/)).toBeInTheDocument();
  expect(screen.getByText('Invoices')).toBeInTheDocument();
  expect(screen.getByText(/Possible header, row 3: A3: Invoice \| B3: Amount/)).toBeInTheDocument();
  expect(screen.getByText('A4: 0012 | B4: 120')).toBeInTheDocument();
  expect(screen.getByText(/1 formula result\(s\) not saved/)).toBeInTheDocument();
  expect(axios.get).not.toHaveBeenCalled();
});
