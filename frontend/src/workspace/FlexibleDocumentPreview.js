import React, { useMemo, useState } from 'react';
import { getDraftDownloadErrorMessage } from './downloadError';
import { downloadDocumentRevision } from './documentDownload';
import { renderDocumentMarkdown } from './documentMarkdown';
import './FlexibleDocumentPreview.css';

const EXTENSIONS = { markdown: 'md', html: 'html', docx: 'docx', pdf: 'pdf' };

export default function FlexibleDocumentPreview({ draft, draftId, showExport = true }) {
  const [view, setView] = useState('preview');
  const [format, setFormat] = useState('docx');
  const [preset, setPreset] = useState('general');
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState('');
  const html = useMemo(() => renderDocumentMarkdown(draft.markdown), [draft.markdown]);
  const formattedExport = format === 'docx' || format === 'pdf';

  const download = async () => {
    if (downloading || !draftId || !draft.revision_id) return;
    setDownloading(true);
    setError('');
    try {
      await downloadDocumentRevision({ draftId, revisionId: draft.revision_id,
        format, preset: formattedExport ? preset : 'general',
        filename: `${String(draft.title || 'Document').replace(/[\\/:*?"<>|]/g, '_')}.${EXTENSIONS[format]}` });
    } catch (failure) {
      setError(await getDraftDownloadErrorMessage(failure));
    } finally {
      setDownloading(false);
    }
  };

  return <section className="flexible-document" aria-label="Document preview">
    <header className="flexible-document__header">
      <h3>{draft.title || 'Untitled document'}</h3>
      <div className="flexible-document__controls">
        <button type="button" aria-pressed={view === 'preview'} onClick={() => setView('preview')}>Preview</button>
        <button type="button" aria-pressed={view === 'source'} onClick={() => setView('source')}>Source</button>
      </div>
      {showExport && <div className="flexible-document__controls">
        <label>Download format <select value={format} onChange={event => setFormat(event.target.value)} disabled={downloading}>
          <option value="markdown">Markdown (.md)</option>
          <option value="html">HTML (.html)</option>
          <option value="docx">Word (.docx)</option>
          <option value="pdf">PDF (.pdf)</option>
        </select></label>
        {formattedExport && <label>Layout <select value={preset} onChange={event => setPreset(event.target.value)} disabled={downloading}>
          <option value="general">General</option>
          <option value="court">Court</option>
        </select></label>}
        <button type="button" onClick={download} disabled={downloading || !draftId || !draft.revision_id}>
          {downloading ? 'Preparing download…' : 'Download'}
        </button>
      </div>}
      {error && <p role="alert">{error}</p>}
    </header>
    {view === 'source'
      ? <pre className="flexible-document__source" aria-label="Markdown source"><code>{draft.markdown}</code></pre>
      : <div className="flexible-document__body" aria-label="Rendered document" dangerouslySetInnerHTML={{ __html: html }} />}
  </section>;
}
