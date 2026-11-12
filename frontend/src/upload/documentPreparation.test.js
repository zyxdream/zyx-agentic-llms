import { act, renderHook, waitFor } from '@testing-library/react';
import axios from 'axios';
import { buildPreparationBatches, preparationBlockers, preparationScopeBytes,
  transferPreparationFiles, validatePreparationTotalSize } from './documentPreparation';
import { useDocumentPreparation } from './useDocumentPreparation';

jest.mock('axios', () => ({ get: jest.fn(), post: jest.fn(), delete: jest.fn() }));
jest.mock('../Constants', () => ({ API_URL: '/api/' }));
jest.mock('../auth/useSetHeaders', () => ({ ensureHeaders: jest.fn(async () => {}) }));

const policy = { max_files: 5, max_bytes: 20, concurrency: 2 };
const entries = count => Array.from({ length: count }, (_, i) => ({
  file: new File(['text'], `${i}.txt`), clientUploadId: `client-${i}`,
}));
const document = status => ({ document_id: 'doc', group_id: 'group', clientUploadId: 'client',
  filename: 'doc.txt', file_size: 4, scope: 'chat', status, stage: status });

afterEach(() => { jest.clearAllMocks(); jest.useRealTimers(); sessionStorage.clear(); });

test('100 documents respect both count and byte boundaries without changing file IDs', () => {
  const selection = entries(100);
  const batches = buildPreparationBatches(selection, { ...policy, max_bytes: 12 });
  expect(batches.flat()).toEqual(selection);
  expect(batches.every(batch => batch.length <= 5 && batch.reduce((n, e) => n + e.file.size, 0) <= 12)).toBe(true);
  expect(() => buildPreparationBatches(entries(1), { ...policy, max_bytes: 1 })).toThrow();
});

test('transfer queue bounds concurrency across independently submitted selections', async () => {
  let active = 0, maximum = 0;
  const api = { post: jest.fn(async (url, body) => {
    active += 1; maximum = Math.max(maximum, active);
    expect(body.getAll('files').length).toBeLessThanOrEqual(5);
    await Promise.resolve(); await Promise.resolve();
    active -= 1;
    return { data: { documents: [], failed: [] } };
  }) };
  await Promise.all([transferPreparationFiles(api, '/api/', 'one', entries(50), policy),
    transferPreparationFiles(api, '/api/', 'two', entries(50), policy)]);
  expect(maximum).toBe(2);
  expect(api.post).toHaveBeenCalledTimes(20);
});

test('a lost response replays the same client IDs', async () => {
  jest.useFakeTimers();
  const api = { post: jest.fn().mockRejectedValueOnce(new Error('connection lost'))
    .mockResolvedValue({ data: { failed: [] } }) };
  const completion = transferPreparationFiles(api, '/api/', 'group', entries(1), policy);
  for (let i = 0; i < 5; i += 1) await Promise.resolve();
  jest.advanceTimersByTime(501);
  await completion;
  expect(api.post.mock.calls[0][1].get('clientUploadIds')).toBe(api.post.mock.calls[1][1].get('clientUploadIds'));
});

test('failures block readiness until explicitly excluded', () => {
  expect(preparationBlockers([document('failed')], new Set())).toHaveLength(1);
  expect(preparationBlockers([document('failed')], new Set(['doc']))).toHaveLength(0);
  expect(preparationBlockers([document('summarizing')], new Set())).toHaveLength(1);
});

test('total size counts pending and failed documents in the destination scope only', () => {
  const documents = [document('ready'), document('summarizing'), document('failed'),
    { ...document('ready'), scope: 'matter' }, document('cancelled')];
  expect(preparationScopeBytes(documents, 'chat')).toBe(12);
  expect(() => validatePreparationTotalSize(entries(1), documents, 'chat', { total_bytes: 16 })).not.toThrow();
  expect(() => validatePreparationTotalSize(entries(2), documents, 'chat', { total_bytes: 16 })).toThrow('total');
  expect(() => validatePreparationTotalSize([], documents, 'chat', { total_bytes: 1 })).not.toThrow();
});

test('oversized selections fail before manifest admission or source transfer', async () => {
  axios.get.mockResolvedValue({ data: { enabled: true, policy: { ...policy, total_bytes: 8 },
    documents: [document('ready')] } });
  const { result } = renderHook(() => useDocumentPreparation({ draftId: 'chat', projectId: 1 }));
  await waitFor(() => expect(result.current.enabled).toBe(true));
  await act(async () => {
    await expect(result.current.upload(entries(2))).rejects.toThrow('total');
  });
  expect(axios.post).not.toHaveBeenCalled();
});

test('reselecting an existing reservation at the byte cap does not reserve bytes twice', async () => {
  const admitted = { ...document('queued'), needs_reupload: true };
  axios.get.mockResolvedValue({ data: { enabled: true, policy: { ...policy, total_bytes: 4 }, documents: [admitted] } });
  axios.post.mockResolvedValue({ data: { documents: [admitted], failed: [] } });
  const { result } = renderHook(() => useDocumentPreparation({ draftId: 'chat', projectId: 1 }));
  await waitFor(() => expect(result.current.enabled).toBe(true));
  await act(async () => { await result.current.upload([{ file: new File(['text'], 'doc.txt') }]); });
  expect(axios.post).toHaveBeenCalledTimes(1);
  expect(axios.post.mock.calls[0][0]).toBe('/api/workspace/upload-groups/group/files');
});

test('one aggregate poller blocks while summarizing and stops after ready', async () => {
  jest.useFakeTimers();
  axios.get.mockResolvedValueOnce({ data: { enabled: true, policy, revision: 'one', documents: [document('summarizing')] } })
    .mockResolvedValue({ data: { enabled: true, policy, revision: 'two', documents: [document('ready')] } });
  const { result } = renderHook(() => useDocumentPreparation({ draftId: 'chat', projectId: 1 }));
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
  expect(result.current.blocked).toBe(true);
  expect(axios.get).toHaveBeenCalledTimes(1);
  await act(async () => { jest.advanceTimersByTime(2001); });
  expect(result.current.blocked).toBe(false);
  await act(async () => { jest.advanceTimersByTime(30000); });
  expect(axios.get).toHaveBeenCalledTimes(2);
});

test('reload restores failed preparation and scoped exclusions', async () => {
  axios.get.mockResolvedValue({ data: { enabled: true, policy, documents: [document('failed')] } });
  const first = renderHook(() => useDocumentPreparation({ draftId: 'chat', projectId: 1 }));
  await waitFor(() => expect(first.result.current.enabled).toBe(true));
  expect(first.result.current.blocked).toBe(true);
  act(() => first.result.current.toggleExcluded('doc'));
  expect(first.result.current.blocked).toBe(false);
  first.unmount();
  const second = renderHook(() => useDocumentPreparation({ draftId: 'chat', projectId: 1 }));
  await waitFor(() => expect(second.result.current.loaded).toBe(true));
  expect(second.result.current.excludedIds.has('doc')).toBe(true);
  expect(second.result.current.blocked).toBe(false);
});

test('aggregate requests coalesce, and status errors fail closed', async () => {
  axios.get.mockResolvedValue({ data: { enabled: true, policy, documents: [] } });
  const { result } = renderHook(() => useDocumentPreparation({ draftId: 'chat', projectId: 1 }));
  await waitFor(() => expect(result.current.enabled).toBe(true));
  axios.get.mockClear();
  await act(async () => { await Promise.all([result.current.refresh(), result.current.refresh()]); });
  expect(axios.get).toHaveBeenCalledTimes(1);
  axios.get.mockRejectedValueOnce(new Error('offline'));
  await act(async () => { await expect(result.current.refresh()).rejects.toThrow('offline'); });
  expect(result.current.blocked).toBe(true);
});

test('partial ready files work across reloads and artifact changes without approval storage', async () => {
  const gapDocument = { ...document('ready'), artifact_id: 'artifact-a',
    coverage: { extraction: { status: 'partial', gap_fingerprint: 'gap-a', gaps: [{ page: 3, message: 'Unreadable page' }] } } };
  axios.get.mockResolvedValue({ data: { enabled: true, owner_id: 1, policy, documents: [gapDocument] } });
  // Old tab approvals are neither read nor required.
  sessionStorage.setItem('document-coverage:1:1/chat', 'invalid legacy storage');
  const first = renderHook(() => useDocumentPreparation({ draftId: 'chat', projectId: 1 }));
  await waitFor(() => expect(first.result.current.loaded).toBe(true));
  expect(first.result.current.blocked).toBe(false);
  first.unmount();
  sessionStorage.clear();
  const second = renderHook(() => useDocumentPreparation({ draftId: 'chat', projectId: 1 }));
  await waitFor(() => expect(second.result.current.loaded).toBe(true));
  expect(second.result.current.blocked).toBe(false);
  axios.get.mockResolvedValue({ data: { enabled: true, owner_id: 1, policy,
    documents: [{ ...gapDocument, artifact_id: 'artifact-b' }] } });
  await act(async () => second.result.current.refresh());
  expect(second.result.current.blocked).toBe(false);
  expect(second.result.current.documents[0].artifact_id).toBe('artifact-b');
  expect(second.result.current.acknowledgeCoverage).toBeUndefined();
});

test('a landing page and its new chat use ready matter files without an approval step', async () => {
  const gapDocument = { ...document('ready'), scope: 'matter', artifact_id: 'artifact-a',
    coverage: { extraction: { status: 'unverified', gap_fingerprint: 'gap-a' } } };
  axios.get.mockResolvedValue({ data: { enabled: true, owner_id: 1, policy, documents: [gapDocument] } });
  const { result, rerender } = renderHook(({ draftId }) => useDocumentPreparation({ draftId, projectId: 1 }),
    { initialProps: { draftId: null } });
  await waitFor(() => expect(result.current.loaded).toBe(true));
  expect(result.current.blocked).toBe(false);
  let firstSnapshot;
  await act(async () => { firstSnapshot = await result.current.refresh({ draftId: 'new-chat' }); });
  expect(axios.get.mock.calls.at(-1)[1].params).toEqual({ conversation_id: 'new-chat' });
  expect(firstSnapshot.documents).toEqual([gapDocument]);
  rerender({ draftId: 'new-chat' });
  await waitFor(() => expect(result.current.loaded).toBe(true));
  expect(result.current.blocked).toBe(false);
});

test('exclusion on a new implicit chat survives the scope transition', async () => {
  const failed = { ...document('failed'), scope: 'matter' };
  axios.get.mockResolvedValue({ data: { enabled: true, owner_id: 1, policy, documents: [failed] } });
  const { result, rerender } = renderHook(({ draftId }) => useDocumentPreparation({ draftId, projectId: 1 }),
    { initialProps: { draftId: null } });
  await waitFor(() => expect(result.current.loaded).toBe(true));
  act(() => result.current.toggleExcluded('doc', { draftId: 'new-chat' }));
  rerender({ draftId: 'new-chat' });
  await waitFor(() => expect(result.current.loaded).toBe(true));
  expect(result.current.blocked).toBe(false);
  expect(result.current.excludedIds.has('doc')).toBe(true);
});

test('clearing the displayed chat removes visible and saved exclusions', async () => {
  axios.get.mockResolvedValue({ data: { enabled: true, policy, documents: [document('failed')] } });
  const { result } = renderHook(() => useDocumentPreparation({ draftId: 'chat', projectId: 1 }));
  await waitFor(() => expect(result.current.loaded).toBe(true));
  act(() => result.current.toggleExcluded('doc'));
  expect(result.current.blocked).toBe(false);
  act(() => result.current.clearExclusions());
  expect(result.current.excludedIds.size).toBe(0);
  expect(result.current.blocked).toBe(true);
  expect(sessionStorage.getItem('document-exclusions:1/chat')).toBeNull();
  await act(async () => result.current.refresh());
  expect(result.current.excludedIds.size).toBe(0);
});

test.each([1, 2])('a late reset for A preserves B exclusions in matter %s', async projectId => {
  axios.get.mockResolvedValue({ data: { enabled: true, policy, documents: [document('failed')] } });
  const { result, rerender } = renderHook(props => useDocumentPreparation(props), {
    initialProps: { draftId: 'A', projectId: 1 },
  });
  await waitFor(() => expect(result.current.loaded).toBe(true));
  act(() => result.current.toggleExcluded('doc'));
  const clearA = result.current.clearExclusions;
  rerender({ draftId: 'B', projectId });
  await waitFor(() => expect(result.current.loaded).toBe(true));
  act(() => result.current.toggleExcluded('doc'));

  act(() => clearA({ projectId: 1, draftId: 'A' }));
  expect(sessionStorage.getItem('document-exclusions:1/A')).toBeNull();
  expect(result.current.excludedIds.has('doc')).toBe(true);
  expect(result.current.blocked).toBe(false);
  expect(JSON.parse(sessionStorage.getItem(`document-exclusions:${projectId}/B`))).toEqual(['doc']);

  rerender({ draftId: 'A', projectId: 1 });
  await waitFor(() => expect(result.current.loaded).toBe(true));
  expect(result.current.excludedIds.size).toBe(0);
  expect(result.current.blocked).toBe(true);
});

test('an implicit chat reset uses the explicit target instead of the landing scope', async () => {
  axios.get.mockResolvedValue({ data: { enabled: true, policy, documents: [document('failed')] } });
  const { result, rerender } = renderHook(({ draftId }) => useDocumentPreparation({ draftId, projectId: 1 }),
    { initialProps: { draftId: null } });
  await waitFor(() => expect(result.current.loaded).toBe(true));
  const clearFromLanding = result.current.clearExclusions;
  act(() => result.current.toggleExcluded('doc', { draftId: 'new-chat' }));
  rerender({ draftId: 'new-chat' });
  await waitFor(() => expect(result.current.loaded).toBe(true));
  expect(result.current.excludedIds.has('doc')).toBe(true);

  act(() => clearFromLanding({ projectId: 1, draftId: 'new-chat' }));
  expect(result.current.excludedIds.size).toBe(0);
  expect(sessionStorage.getItem('document-exclusions:1/new-chat')).toBeNull();
  await act(async () => result.current.refresh());
  expect(result.current.excludedIds.size).toBe(0);
});

test('returning to a tab refreshes terminal states without introducing per-file polling', async () => {
  axios.get.mockResolvedValue({ data: { enabled: true, owner_id: 1, policy, documents: [] } });
  const { result } = renderHook(() => useDocumentPreparation({ draftId: 'chat', projectId: 1 }));
  await waitFor(() => expect(result.current.loaded).toBe(true));
  axios.get.mockClear();
  await act(async () => { window.dispatchEvent(new Event('focus')); });
  await waitFor(() => expect(axios.get).toHaveBeenCalledTimes(1));
});

test('failed deletion retains the file, while Retry refreshes a new re-upload requirement', async () => {
  const failed = document('failed');
  axios.get.mockResolvedValue({ data: { enabled: true, owner_id: 1, policy, documents: [failed] } });
  const { result } = renderHook(() => useDocumentPreparation({ draftId: 'chat', projectId: 1 }));
  await waitFor(() => expect(result.current.loaded).toBe(true));
  axios.delete.mockRejectedValueOnce(new Error('Deletion unavailable'));
  await act(async () => { await expect(result.current.remove(failed)).rejects.toThrow('Deletion unavailable'); });
  expect(result.current.documents).toEqual([failed]);
  axios.post.mockResolvedValueOnce({ data: { ...failed, group_id: 'registered-group', needs_reupload: true } });
  axios.get.mockClear();
  await act(async () => { await expect(result.current.retry(failed)).rejects.toThrow('Select this file again'); });
  expect(axios.get).toHaveBeenCalled();
});

test('a disabled or unavailable service never falls back to legacy uploads', async () => {
  axios.get.mockResolvedValue({ data: { enabled: false, documents: [] } });
  const { result } = renderHook(() => useDocumentPreparation({ draftId: 'chat', projectId: 1 }));
  await waitFor(() => expect(result.current.error).toBeTruthy());
  await act(async () => {
    await expect(result.current.upload([{ file: new File(['text'], 'test.txt') }])).rejects.toThrow('unavailable');
  });
  expect(result.current.blocked).toBe(true);
  expect(axios.post).not.toHaveBeenCalled();
});

test('ambiguous reselected files cannot create duplicate pending reservations', async () => {
  const pending = { ...document('queued'), filename: 'same.txt', file_size: 4, needs_reupload: true };
  axios.get.mockResolvedValue({ data: { enabled: true, owner_id: 1, policy,
    documents: [pending, { ...pending, document_id: 'other-document', clientUploadId: 'other-client' }] } });
  const { result } = renderHook(() => useDocumentPreparation({ draftId: 'chat', projectId: 1 }));
  await waitFor(() => expect(result.current.loaded).toBe(true));
  await act(async () => {
    await expect(result.current.upload([{ file: new File(['text'], 'same.txt') }])).rejects.toThrow('Multiple pending uploads');
  });
  expect(axios.post).not.toHaveBeenCalled();
});
