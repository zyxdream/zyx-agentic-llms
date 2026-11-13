import React, {useRef} from 'react';
import {IconButton} from './WorkspaceUI';
import PreparedDocumentSummary from '../upload/PreparedDocumentSummary';
import {renderDocumentMarkdown} from './documentMarkdown';

function InspectorView({open, tab, onClose, content, markdown, editing, editor, setEditor, busy, active, onSave, onEdit, onDiscard, format, setFormat, onDownload, preparation, onAttach, onFolder, source, onRead, onCloseSource, onNextSource, onRetry, onRemove}) {
  if (!open) return null;
  const project = tab === 'project-documents';
  const title = tab === 'output' ? 'Output' : project ? 'Project documents' : 'Attachments';
  const documents = preparation.documents.filter(document => document.scope === (project ? 'matter' : 'chat'));
  // Folders are presentation groups; document IDs remain the retrieval identity.
  const groups = new Map();
  for (const document of documents) {
    const key = document.folder_id ? `folder:${document.folder_id}` : 'files';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(document);
  }
  return <div className="inspector-view">
    <div className="inspector-scroll">
      {tab === 'output' && content && <section aria-label="Output">
        <div className="document-heading"><h2>{content.document?.title || 'Output'}</h2></div>
        {editing ? <><label className="sr-only" htmlFor="document-markdown">Output Markdown</label><textarea id="document-markdown" className="document-editor" aria-label="Output Markdown" value={editor} disabled={busy} onChange={event => setEditor(event.target.value)}/><div className="document-actions"><button className="primary-button" disabled={busy || active} onClick={onSave}>Save output</button><button onClick={onDiscard}>Discard edits</button></div><p className="field-help">Changes stay here until you save them.</p></> : <><div className="document-preview markdown-body" dangerouslySetInnerHTML={{__html: renderDocumentMarkdown(markdown)}}/>{!markdown && <p className="muted">Your output will appear here.</p>}<button disabled={busy || active} onClick={onEdit}>Edit Markdown</button></>}
        <div className="export-controls"><label>Download format<select value={format} onChange={event => setFormat(event.target.value)}>{['docx','pdf','markdown','html'].map(value => <option key={value}>{value}</option>)}</select></label><button disabled={busy || !markdown || editing} onClick={onDownload}>Download output</button></div>
      </section>}
      {tab !== 'output' && <section aria-label={title}>
        <p className="field-help">{project ? 'Available across this project’s conversations.' : 'Files attached to messages stay available in this conversation.'}</p>
        <div className="source-upload"><button onClick={() => onAttach(project ? 'matter' : 'chat')}>{project ? 'Upload files' : 'Attach files'}</button>{project && <button onClick={onFolder}>Upload folder</button>}</div>
        {preparation.error && <p role="alert">{preparation.error}</p>}
        {!documents.length && <div className="empty-sources"><p>{project ? 'No project documents yet' : 'No attachments yet'}</p></div>}
        {!!documents.length && <p className="field-help">Choose files to use for the next message. Unchecking a file does not remove it.</p>}
        {[...groups].map(([key, members]) => <div key={key} className="input-document-group">
          {key !== 'files' && <h3 className="input-folder-name">Folder · {members[0].folder_name || `Upload ${members[0].folder_id}`}</h3>}
          {members.map(document => <div className="source" key={document.document_id}>
            <label className="source-selection"><input type="checkbox" aria-label={`Use ${document.filename} for next message`} checked={!preparation.excludedIds.has(document.document_id)} onChange={() => preparation.toggleExcluded(document.document_id)}/><span className="source-name" title={document.relative_path || document.filename}>{document.filename}<span className="source-status">{document.status}{!project && !document.committed ? ' · Not sent yet' : ''}</span>{document.relative_path && <span className="source-path">{document.relative_path}</span>}</span></label>
            {document.error && <p className="field-help" role="status">{document.error}</p>}
            <PreparedDocumentSummary documentId={document.document_id} artifactId={document.artifact_id} metadata={document.metadata} filename={document.filename}/>
            <div className="source-actions"><button disabled={busy || document.status !== 'ready'} onClick={() => onRead(document,tab)}>Read file</button>{(document.status === 'failed' || document.needs_reupload) && <button onClick={() => onRetry(document)}>Retry preparation</button>}<button disabled={busy || active} onClick={() => onRemove(document)}>{['queued','parsing','indexing','summarizing'].includes(document.status) ? 'Cancel preparation' : 'Remove file'}</button></div>
          </div>)}
        </div>)}
        {source && <section className="source-reader" aria-label="File text"><div className="source-reader-heading"><h3>{source.filename}</h3><IconButton icon="close" label="Close file" onClick={() => onCloseSource(tab)}/></div><p className="field-help">Characters {source.start}–{source.end} of {source.total_characters}</p><pre>{source.text}</pre>{source.end < source.total_characters && <button onClick={() => onNextSource(source)}>Next file range</button>}</section>}
      </section>}
    </div>
  </div>;
}

const labels = {'project-documents':'Project documents',output:'Output',attachments:'Attachments'};
export default function WorkspaceInspector({open,tabs,tab,onTab,onCloseTab,onClose,sources,...props}) {
  const host=useRef(null);
  return <aside hidden={!open} className="workspace-inspector" aria-label="Workspace inspector" ref={host}>
    <div className="inspector-heading"><h2 className="sr-only">{labels[tab]}</h2><div className="dock-tabs" role="tablist" aria-label="Right panels" onKeyDown={event => {
      if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
      event.preventDefault();
      const index=tabs.indexOf(tab), next=event.key === 'Home' ? tabs[0] : event.key === 'End' ? tabs.at(-1) : tabs[(index+(event.key === 'ArrowRight' ? 1 : tabs.length-1))%tabs.length];
      onTab(next);host.current.querySelector(`#dock-tab-${next}`)?.focus();
    }}>{tabs.map(value => <div className="dock-tab" key={value}><button type="button" id={`dock-tab-${value}`} role="tab" tabIndex={tab === value ? 0 : -1} aria-selected={tab === value} aria-controls={`dock-view-${value}`} onClick={() => onTab(value)}>{labels[value]}</button><IconButton icon="close" label={`Close ${labels[value]} tab`} onClick={() => {onCloseTab(value);requestAnimationFrame(() => (host.current?.querySelector('[role=tab][aria-selected=true]') || document.querySelector(`.workspace-main [aria-label="${labels[value]}"]`))?.focus());}}/></div>)}</div><IconButton icon="sidebar" label="Hide right panel" onClick={onClose}/></div>
    {tabs.map(value => <div key={value} id={`dock-view-${value}`} role="tabpanel" aria-labelledby={`dock-tab-${value}`} hidden={value !== tab} className="dock-view"><InspectorView {...props} open tab={value} source={sources[value]}/></div>)}
  </aside>;
}
