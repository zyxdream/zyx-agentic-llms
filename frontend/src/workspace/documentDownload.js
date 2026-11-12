import axios from 'axios';
import { API_URL } from '../Constants';
import { ensureHeaders } from '../auth/useSetHeaders';

export async function downloadDocumentRevision({ draftId, revisionId, format, preset, filename, isCurrent = () => true }) {
  await ensureHeaders();
  if (!isCurrent()) return;
  const response = await axios.post(`${API_URL}workspace/documents/${encodeURIComponent(draftId)}/revisions/${encodeURIComponent(revisionId)}/download`, {
    revision_id: revisionId, format, preset,
  }, { responseType: 'blob' });
  if (!isCurrent()) return;
  const disposition = response.headers?.['content-disposition'] || response.headers?.['Content-Disposition'];
  const match = disposition?.match(/filename\*=UTF-8''([^;\n]+)/) || disposition?.match(/filename="?([^";\n]+)"?/);
  if (match?.[1]) filename = decodeURIComponent(match[1]);
  const url = window.URL.createObjectURL(response.data);
  const link = document.createElement('a');
  try {
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
  } finally {
    link.remove();
    window.URL.revokeObjectURL(url);
  }
}
