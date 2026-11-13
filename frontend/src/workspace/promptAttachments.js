// Read historical payload spellings without modifying saved conversation turns.
export function promptAttachments(metadata = {}) {
  const entries = Array.isArray(metadata) ? metadata : metadata?.prompt_attachments || metadata?.promptAttachments || [];
  if (!Array.isArray(entries)) return [];
  return entries.filter(entry => entry && typeof entry === 'object').map(entry => ({
    id: String(entry.file_id || entry.fileId || ''),
    name: String(entry.file_name || entry.fileName || entry.filename || entry.name || entry.file_id || entry.fileId || 'Attachment')
  }));
}
