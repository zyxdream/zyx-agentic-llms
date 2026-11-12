import { useCallback, useEffect, useRef, useState } from 'react';
import axios from 'axios';
import { API_URL } from '../Constants';
import { ensureHeaders } from '../auth/useSetHeaders';
import { preparationBlockers, retryRequest, transferPreparationFiles, uploadId, validatePreparationTotalSize } from './documentPreparation';

export function useDocumentPreparation({ draftId, projectId }) {
  const [catalog, setCatalog] = useState({ enabled: true, loaded: false, documents: [] });
  const [error, setError] = useState(null);
  const [excludedIds, setExcludedIds] = useState(new Set());
  const [epoch, setEpoch] = useState(0);
  const [transfers, setTransfers] = useState({});
  const files = useRef(new Map());
  const requests = useRef(new Map());
  // Keep prompt exclusions in memory as well as sessionStorage (which can be
  // unavailable). A new implicit chat can receive an exclusion before React has
  // switched this hook from the matter landing page to that actual chat ID.
  const exclusionChoices = useRef(new Map());
  const scope = `${projectId || ''}/${draftId || ''}`;
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const wake = useCallback(() => setEpoch(value => value + 1), []);

  useEffect(() => {
    setCatalog({ enabled: true, loaded: false, documents: [] });
    setError(null);
    try { setExcludedIds(new Set(exclusionChoices.current.get(scope) || JSON.parse(sessionStorage.getItem(`document-exclusions:${scope}`) || '[]'))); }
    catch { setExcludedIds(new Set()); }
  }, [scope]);

  const refresh = useCallback(async (target = {}) => {
    const targetProject = target.projectId ?? projectId;
    const targetDraft = target.draftId === undefined ? draftId : target.draftId;
    const targetScope = `${targetProject || ''}/${targetDraft || ''}`;
    if (!targetProject) return { enabled: true, documents: [], excludedIds: new Set() };
    if (requests.current.has(targetScope)) return requests.current.get(targetScope);
    const work = (async () => {
      await ensureHeaders();
      const params = targetDraft ? { conversation_id: targetDraft } : { projectId: targetProject };
      const response = await axios.get(`${API_URL}workspace/document-preparation`, { params });
      if (response.data.enabled !== true) throw new Error('Required document preparation service is unavailable.');
      let excluded = [];
      try { excluded = exclusionChoices.current.get(targetScope) || JSON.parse(sessionStorage.getItem(`document-exclusions:${targetScope}`) || '[]'); } catch { /* optional */ }
      const available = new Set(response.data.documents.map(doc => doc.document_id));
      excluded = new Set((Array.isArray(excluded) ? excluded : []).filter(id => available.has(id)));
      exclusionChoices.current.set(targetScope, [...excluded]);
      if (scopeRef.current === targetScope) {
        setCatalog({ documents: [], ...response.data, loaded: true }); setError(null);
        setExcludedIds(excluded);
      }
      return { ...response.data, excludedIds: excluded };
    })();
    requests.current.set(targetScope, work);
    try { return await work; }
    catch (failure) {
      if (scopeRef.current === targetScope) setError('Document preparation status is unavailable. Reconnecting…');
      throw failure;
    } finally { requests.current.delete(targetScope); }
  }, [draftId, projectId]);

  useEffect(() => {
    let disposed = false, timer, previousRevision, delay = 2000;
    async function poll() {
      try {
        const next = await refresh();
        if (disposed || !next.enabled) return;
        delay = next.revision === previousRevision ? Math.min(delay * 1.5, 10000) : 2000;
        previousRevision = next.revision;
        if (!next.documents.some(doc => !['ready', 'failed', 'cancelled'].includes(doc.status))) return;
      } catch (failure) {
        if (disposed) return;
        setError('Document preparation status is unavailable. Reconnecting…');
        delay = Math.min(delay * 2, 15000);
      }
      if (!disposed) timer = setTimeout(poll, delay);
    }
    poll();
    return () => { disposed = true; clearTimeout(timer); };
  }, [refresh, epoch]);

  useEffect(() => {
    const visible = () => { if (document.visibilityState === 'visible') wake(); };
    window.addEventListener('focus', wake);
    document.addEventListener('visibilitychange', visible);
    return () => { window.removeEventListener('focus', wake); document.removeEventListener('visibilitychange', visible); };
  }, [wake]);

  const toggleExcluded = useCallback((id, target = {}) => {
    const destination = `${target.projectId ?? projectId ?? ''}/${target.draftId ?? draftId ?? ''}`;
    let previous = exclusionChoices.current.get(destination);
    if (!previous) { try { previous = JSON.parse(sessionStorage.getItem(`document-exclusions:${destination}`) || '[]'); } catch { previous = []; } }
    const next = new Set(previous);
    next.has(id) ? next.delete(id) : next.add(id);
    exclusionChoices.current.set(destination, [...next]);
    try { sessionStorage.setItem(`document-exclusions:${destination}`, JSON.stringify([...next])); } catch { /* optional persistence */ }
    if (scopeRef.current === destination) setExcludedIds(next);
    return next;
  }, [projectId, draftId]);

  const clearExclusions = useCallback((target = {}) => {
    const destination = `${target.projectId ?? projectId ?? ''}/${target.draftId ?? draftId ?? ''}`;
    exclusionChoices.current.set(destination, []);
    try { sessionStorage.removeItem(`document-exclusions:${destination}`); } catch { /* optional persistence */ }
    if (scopeRef.current === destination) setExcludedIds(new Set());
  }, [projectId, draftId]);

  const upload = useCallback(async (entries, options = {}) => {
    const origin = scope;
    const destination = options.scope || 'chat';
    const transferKey = `${projectId}:${destination}:${options.scopeId || (destination === 'chat' ? draftId : projectId)}`;
    setTransfers(previous => ({ ...previous, [transferKey]: (previous[transferKey] || 0) + 1 }));
    try {
      await ensureHeaders();
      const current = await refresh({ draftId: destination === 'chat' ? (options.scopeId || draftId) : draftId });
      const pending = current.documents.filter(doc => doc.scope === destination && doc.needs_reupload);
      // A reselected, untransferred file resumes its durable manifest entry.
      const resumed = [], fresh = [];
      for (const entry of entries) {
        const matches = pending.filter(doc => doc.filename === entry.file.name && doc.file_size === entry.file.size
          && (doc.relative_path || null) === (entry.relativePath || null));
        if (matches.length > 1) throw new Error('Multiple pending uploads have this name and size. Remove the ambiguous pending entries, then select the files again.');
        if (matches.length === 1) {
          const doc = matches[0]; pending.splice(pending.indexOf(doc), 1);
          const updated = doc.status === 'failed'
            ? (await axios.post(`${API_URL}workspace/sources/${encodeURIComponent(doc.document_id)}/retry`,
              { generation: doc.generation, backfill: doc.error_code === 'backfill_required' })).data : doc;
          resumed.push({ ...entry, clientUploadId: updated.clientUploadId, document: updated });
        } else fresh.push({ ...entry, clientUploadId: entry.clientUploadId || uploadId() });
      }
      validatePreparationTotalSize(fresh, current.documents, destination, current.policy);
      const selections = [];
      if (fresh.length) {
        const selectionId = uploadId();
        const response = await retryRequest(() => axios.post(`${API_URL}workspace/upload-groups`, {
          scope: destination, scopeId: options.scopeId || (destination === 'chat' ? draftId : projectId),
          selectionId, folderName: options.folderName, folderId: options.folderId,
          documents: fresh.map(entry => ({ client_upload_id: entry.clientUploadId,
            filename: entry.file.name, file_size: entry.file.size, relative_path: entry.relativePath || null })),
        }));
        selections.push({ ...response.data, entries: fresh });
      }
      for (const groupId of new Set(resumed.map(entry => entry.document.group_id))) {
        const members = resumed.filter(entry => entry.document.group_id === groupId);
        selections.push({ group_id: groupId, documents: members.map(entry => entry.document),
          policy: current.policy, entries: members });
      }
      for (const selection of selections) {
        selection.documents.forEach(doc => {
          const entry = selection.entries.find(e => e.clientUploadId === doc.clientUploadId);
          if (entry) files.current.set(doc.document_id, entry.file);
        });
        options.onAdmitted?.(selection.documents);
      }
      wake();
      await Promise.all(selections.map(selection => transferPreparationFiles(
        axios, API_URL, selection.group_id, selection.entries, selection.policy)));
    } finally {
      if (scopeRef.current === origin) await refresh().catch(() => {});
      setTransfers(previous => {
        const next = { ...previous, [transferKey]: (previous[transferKey] || 1) - 1 };
        if (!next[transferKey]) delete next[transferKey];
        return next;
      });
      wake();
    }
  }, [draftId, projectId, scope, refresh, wake]);

  const retry = useCallback(async doc => {
    await ensureHeaders();
    try {
      let updated = doc;
      if (doc.status === 'failed' || doc.coverage?.extraction?.gap_fingerprint) updated = (await axios.post(`${API_URL}workspace/sources/${encodeURIComponent(doc.document_id)}/retry`,
        { generation: doc.generation, backfill: doc.error_code === 'backfill_required' })).data;
      const file = files.current.get(doc.document_id);
      if (updated.needs_reupload && file) {
        await transferPreparationFiles(axios, API_URL, updated.group_id,
          [{ file, clientUploadId: updated.clientUploadId }], catalog.policy);
      } else if (updated.needs_reupload) throw new Error('Select this file again using Add Docs to resume the upload.');
    } finally { await refresh(); wake(); }
  }, [catalog.policy, refresh, wake]);

  const remove = useCallback(async doc => {
    await ensureHeaders();
    await axios.delete(`${API_URL}workspace/sources/${encodeURIComponent(doc.document_id)}`);
    files.current.delete(doc.document_id);
    await refresh(); wake();
  }, [refresh, wake]);

  const blockers = preparationBlockers(catalog.documents, excludedIds);
  const transferring = Object.entries(transfers).reduce((sum, [key, count]) =>
    sum + ((key === `${projectId}:chat:${draftId}` || key === `${projectId}:matter:${projectId}`) ? count : 0), 0);
  return { ...catalog, error, excludedIds, toggleExcluded, clearExclusions, upload, retry, remove,
    refresh, blockers, transferring, scope,
    blocked: !!projectId && (!catalog.loaded || !!error || !!transferring || !!blockers.length) };
}
