// Transport helpers have no chat-state dependency. IDs survive request retries.
export const uploadId = () => window.crypto?.randomUUID?.()
  || `upload-${Date.now()}-${Math.random().toString(36).slice(2)}`;

export function matchesPendingUpload(document, entry, options) {
  return document.scope === options.scope && document.needs_reupload
    && document.filename === entry.file.name && document.file_size === entry.file.size
    && (document.relative_path || null) === (entry.relativePath || null)
    && (!options.folderName || document.folder_name === options.folderName)
    && (!options.folderId || String(document.folder_id) === String(options.folderId));
}

export function preparationScopeBytes(documents, scope) {
  return documents.filter(doc => doc.scope === scope && doc.status !== 'cancelled')
    .reduce((total, doc) => total + (doc.file_size || 0), 0);
}

export function validatePreparationTotalSize(entries, documents, scope, policy) {
  const fileLimit = scope === 'chat' ? (policy.chat_files ?? 100) : (policy.matter_files ?? 30);
  const existingCount = documents.filter(doc => doc.scope === scope && doc.status !== 'cancelled').length;
  if (existingCount + entries.length > fileLimit) {
    throw new Error(`${scope === 'chat' ? 'This conversation' : 'This project'} allows ${fileLimit} files, including pending and failed uploads. Remove documents before adding more.`);
  }
  const limit = policy.total_bytes ?? 1024 ** 3;
  const selectionBytes = entries.reduce((total, entry) => total + entry.file.size, 0);
  if (entries.length && preparationScopeBytes(documents, scope) + selectionBytes > limit) {
    const label = scope === 'chat' ? 'This chat' : 'This project';
    throw new Error(`${label} allows ${(limit / 1024 ** 3).toLocaleString()} GB total, including pending and failed uploads. Remove documents before adding more.`);
  }
}

export function buildPreparationBatches(entries, policy) {
  const batches = [];
  let batch = [], bytes = 0;
  for (const entry of entries) {
    if (entry.file.size > policy.max_bytes) throw new Error('A file exceeds the upload group size limit.');
    if (batch.length && (batch.length >= policy.max_files || bytes + entry.file.size > policy.max_bytes)) {
      batches.push(batch); batch = []; bytes = 0;
    }
    batch.push(entry); bytes += entry.file.size;
  }
  if (batch.length) batches.push(batch);
  return batches;
}

let activeTransfers = 0;
const waitingTransfers = [];
async function transferSlot(limit, work) {
  while (activeTransfers >= limit) await new Promise(resolve => waitingTransfers.push(resolve));
  activeTransfers += 1;
  try { return await work(); }
  finally {
    activeTransfers -= 1;
    waitingTransfers.splice(0).forEach(resolve => resolve());
  }
}

export async function retryRequest(work) {
  for (let attempt = 0; ; attempt += 1) {
    try { return await work(); }
    catch (error) {
      const status = error.response?.status;
      if (attempt >= 2 || (status && status < 500 && status !== 429)) throw error;
      await new Promise(resolve => setTimeout(resolve, 500 * (attempt + 1)));
    }
  }
}

export async function transferPreparationFiles(api, baseUrl, groupId, entries, policy) {
  const batches = buildPreparationBatches(entries, policy);
  const results = await Promise.allSettled(batches.map(batch => transferSlot(policy.concurrency, async () => {
    const response = await retryRequest(() => {
      const form = new FormData();
      form.append('clientUploadIds', JSON.stringify(batch.map(e => e.clientUploadId)));
      batch.forEach(entry => form.append('files', entry.file));
      return api.post(`${baseUrl}workspace/upload-groups/${groupId}/files`, form);
    });
    if (response.data.failed?.length) throw new Error(response.data.failed.map(f => f.msg).join(' '));
    return response.data;
  })));
  const failures = results.filter(result => result.status === 'rejected');
  if (failures.length) throw failures[0].reason;
  return results.map(result => result.value);
}

export function preparationBlockers(documents, excludedIds) {
  return documents.filter(doc => doc.status !== 'cancelled' && !excludedIds.has(doc.document_id)
    && doc.status !== 'ready');
}
