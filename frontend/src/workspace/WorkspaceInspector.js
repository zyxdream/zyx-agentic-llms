import React, {useRef} from 'react';
import {IconButton, useFocusBoundary, useMediaQuery} from './WorkspaceUI';
import PreparedDocumentSummary from '../upload/PreparedDocumentSummary';
import {renderDocumentMarkdown} from './documentMarkdown';

export default function WorkspaceInspector({open, tab, onTab, onClose, content, markdown, editing, editor, setEditor, busy, active, onSave, onEdit, onDiscard, format, setFormat, onDownload, preparation, chat, uploadScope, setUploadScope, onAttach, source, onRead, onCloseSource, onNextSource, onRetry, onRemove}) {
  const ref = useRef(null), overlay = useMediaQuery('(max-width: 1099px)');
  useFocusBoundary(ref, onClose, open && overlay);
  if (!open) return null;
  return <aside ref={ref} tabIndex={-1} className="workspace-inspector" role={overlay ? 'dialog' : undefined} aria-label="Workspace inspector" aria-modal={overlay ? true : undefined}>
    <div className="inspector-heading"><div className="inspector-tabs" role="tablist" aria-label="Workspace panels" onKeyDown={event => {
      if (['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) { event.preventDefault(); const next = event.key === 'Home' ? (content ? 'document' : 'sources') : event.key === 'End' ? 'sources' : tab === 'document' || !content ? 'sources' : 'document'; onTab(next); document.getElementById(`${next}-tab`)?.focus(); }
    }}>
      <button type="button" role="tab" tabIndex={tab === 'document' ? 0 : -1} aria-selected={tab === 'document'} aria-controls="workspace-document-panel" id="document-tab" disabled={!content} onClick={() => onTab('document')}>Document</button>
      <button type="button" role="tab" tabIndex={tab === 'sources' ? 0 : -1} aria-selected={tab === 'sources'} aria-controls="workspace-sources-panel" id="sources-tab" onClick={() => onTab('sources')}>Sources</button>
    </div><IconButton icon="close" label="Close panel" onClick={onClose}/></div>
    <div className="inspector-scroll">
      {tab === 'document' && content && <div role="tabpanel" aria-labelledby="document-tab" id="workspace-document-panel"><section aria-label="Document">
        <div className="document-heading"><span className="eyebrow">WORKING DOCUMENT</span><h2>{content.document?.title || 'Document'}</h2></div>
        {editing ? <><label className="sr-only" htmlFor="document-markdown">Document Markdown</label><textarea id="document-markdown" className="document-editor" aria-label="Document Markdown" value={editor} disabled={busy} onChange={event => setEditor(event.target.value)}/><div className="document-actions"><button className="primary-button" disabled={busy || active} onClick={onSave}>Save document</button><button onClick={onDiscard}>Discard edits</button></div><p className="field-help">Changes stay here until you save them.</p></> : <><div className="document-preview markdown-body" dangerouslySetInnerHTML={{__html: renderDocumentMarkdown(markdown)}}/>{!markdown && <p className="muted">Drafts and documents will appear here. You can also start one yourself.</p>}<button disabled={busy || active} onClick={onEdit}>Edit Markdown</button></>}
        <div className="export-controls"><label>Download format<select value={format} onChange={event => setFormat(event.target.value)}>{['docx','pdf','markdown','html'].map(value => <option key={value}>{value}</option>)}</select></label><button disabled={busy || !markdown || editing} onClick={onDownload}>Download document</button></div>
      </section></div>}
      {tab === 'sources' && <div role="tabpanel" aria-labelledby="sources-tab" id="workspace-sources-panel"><section aria-label="Sources"><div className="document-heading"><span className="eyebrow">REFERENCE MATERIAL</span><h2>Sources</h2><p className="muted">Choose what Workspace can use for your next request.</p></div>
        <div className="source-upload"><label>Upload to<select value={chat ? uploadScope : 'matter'} onChange={event => setUploadScope(event.target.value)}><option value="chat" disabled={!chat}>This conversation</option><option value="matter">This project</option></select></label><button onClick={() => onAttach(chat ? uploadScope : 'matter')}>Add files</button></div>
        {preparation.error && <p role="alert">{preparation.error}</p>}
        {preparation.documents.length === 0 && <div className="empty-sources"><p>No sources yet</p><p className="field-help">Add a PDF, Word document, spreadsheet, or text file.</p></div>}
        {preparation.documents.map(document => <div className="source" key={document.document_id}>
          <label className="source-selection"><input type="checkbox" checked={!preparation.excludedIds.has(document.document_id)} onChange={() => preparation.toggleExcluded(document.document_id)}/><span className="source-name">{document.filename}<span className="source-status">{document.status}{document.scope === 'matter' ? ' · Project source' : ''}</span></span></label>
          <PreparedDocumentSummary documentId={document.document_id} artifactId={document.artifact_id} metadata={document.metadata} filename={document.filename}/>
          <div className="source-actions"><button disabled={busy} onClick={() => onRead(document)}>Read source</button>{(document.status === 'failed' || document.needs_reupload) && <button onClick={() => onRetry(document)}>Retry preparation</button>}<button disabled={busy || active} onClick={() => onRemove(document)}>{['queued','parsing','indexing','summarizing'].includes(document.status) ? 'Cancel preparation' : 'Remove source'}</button></div>
        </div>)}
        {source && <section className="source-reader" aria-label="Source text"><div className="source-reader-heading"><h3>{source.filename}</h3><IconButton icon="close" label="Close source" onClick={onCloseSource}/></div><p className="field-help">Characters {source.start}–{source.end} of {source.total_characters}</p><pre>{source.text}</pre>{source.end < source.total_characters && <button onClick={onNextSource}>Next source range</button>}</section>}
      </section></div>}
    </div>
  </aside>;
}
