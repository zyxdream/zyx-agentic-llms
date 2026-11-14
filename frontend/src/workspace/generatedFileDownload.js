import axios from 'axios';
import { API_URL } from '../Constants';
import { ensureHeaders } from '../auth/useSetHeaders';

// Identifiers come from saved file records, never a model-written path or URL.
export async function downloadGeneratedFile({runId, executionId, fileId, filename, isCurrent = () => true}) {
  if (![runId, executionId, fileId, filename].every(value => typeof value === 'string' && value)) {
    throw new Error('This saved file is unavailable.');
  }
  await ensureHeaders();
  if (!isCurrent()) return;
  const response = await axios.get(
    `${API_URL}workspace/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(executionId)}/files/${encodeURIComponent(fileId)}`,
    {responseType:'blob'},
  );
  if (!isCurrent()) return;
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
