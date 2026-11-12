import { WORKSPACE_ACCEPT, isWorkspaceFilenameSupported } from './workspaceFileTypes';

test('Workspace accepts modern spreadsheets and existing documents across case and folder paths', () => {
  expect(WORKSPACE_ACCEPT.split(',')).toEqual(expect.arrayContaining(['.xlsx', '.csv', '.pdf', '.docx']));
  for (const filename of ['Invoice.XLSX', 'exports/payments.csv', 'folder\\agreement.PDF', 'notes.txt']) {
    expect(isWorkspaceFilenameSupported(filename)).toBe(true);
  }
});

test('folder selection skips office locks, legacy spreadsheets and unrelated files', () => {
  for (const filename of ['~$Invoice.xlsx', 'folder/~$Invoice.xlsx', 'old.xls', 'macro.xlsm', 'archive.zip', 'README']) {
    expect(isWorkspaceFilenameSupported(filename)).toBe(false);
  }
});
