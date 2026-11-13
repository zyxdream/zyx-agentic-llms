import React, {useLayoutEffect, useRef} from 'react';
import {Icon, IconButton} from './WorkspaceUI';

export default function WorkspaceComposer({query, onQuery, onSubmit, onAttach, disabled, busy, active, controlBusy, onStop, preparation, onPanel, onRemove, showPreparationError = true}) {
  const ref = useRef(null);
  useLayoutEffect(() => { if (ref.current) { ref.current.style.height = 'auto'; ref.current.style.height = `${Math.min(ref.current.scrollHeight || 58, 180)}px`; } }, [query]);
  const unavailable = preparation.documents.filter(d => d.status !== 'ready' && d.status !== 'cancelled' && !preparation.excludedIds.has(d.document_id));
  const staged = preparation.documents.filter(d => d.status !== 'cancelled' && !d.committed && d.scope === 'chat');
  const attachments = preparation.documents.filter(d => d.status !== 'cancelled' && d.scope === 'chat');
  const projectUnavailable = unavailable.filter(d => d.scope === 'matter');
  const attachmentUnavailable = unavailable.filter(d => d.scope === 'chat');
  return <div className="composer-dock"><div className="composer-container">
    {preparation.error && showPreparationError && <p className="composer-notice" role="alert">{preparation.error} <button onClick={() => onPanel('project-documents')}>Project documents</button> · <button onClick={() => onPanel('attachments')}>Attachments</button></p>}
    <div className="composer-notice" aria-live="polite">
      {preparation.transferring > 0 && <p>Uploading files…</p>}
      {[[projectUnavailable,'project document','project-documents'],[attachmentUnavailable,'attachment','attachments']].map(([files, label, view]) => files.length > 0 && <p key={view}>{files.length} {label}{files.length === 1 ? '' : 's'} not ready. Wait, retry, or exclude. <button onClick={() => onPanel(view)}>View {view === 'attachments' ? 'attachments' : 'project documents'}</button></p>)}
      {preparation.blocked && !preparation.transferring && !unavailable.length && !preparation.error && <p>Loading file status…</p>}
    </div>
    {staged.length > 0 && <div className="attachment-chips">{staged.slice(0,3).map(d => <div className="attachment-chip" key={d.document_id}><button onClick={() => onPanel('attachments')} title={`${d.filename} — ${d.status}`}>{d.filename}<span>{d.status}</span></button><IconButton icon="close" label={`Remove attachment ${d.filename}`} disabled={busy || active} onClick={() => onRemove(d)}/></div>)}{staged.length > 3 && <button onClick={() => onPanel('attachments')}>+{staged.length - 3} more</button>}</div>}
    <form className="workspace-composer" onSubmit={onSubmit}>
      <label className="sr-only" htmlFor="workspace-message">Message</label><textarea id="workspace-message" ref={ref} rows={2} aria-label="Message" value={query} onChange={event => onQuery(event.target.value)} disabled={disabled} placeholder="Message Workspace…" onKeyDown={event => {
        if (event.key === 'Enter' && event.shiftKey && !event.ctrlKey && !event.altKey && !event.metaKey && !event.nativeEvent.isComposing && event.nativeEvent.keyCode !== 229) { event.preventDefault(); if (!busy && !active && query.trim() && !preparation.blocked && !disabled) event.currentTarget.form.requestSubmit(); }
      }}/>
      <div className="composer-tools"><div className="composer-options"><IconButton label="Attach files" icon="attach" disabled={disabled || active} onClick={onAttach}/>
      {attachments.length > 0 && <button className="exclusion-count" type="button" aria-label="Attachments" onClick={() => onPanel('attachments')}>Attachments <span>{attachments.length}</span></button>}
      {preparation.excludedIds.size > 0 && <span className="exclusion-count">{preparation.excludedIds.size} excluded</span>}</div>
      {active ? <button type="button" className="send-button" disabled={controlBusy} aria-label="Cancel request" title="Stop request" onClick={onStop}><Icon name="stop"/></button> : <div className="send-control"><button className="send-button" disabled={disabled || busy || !query.trim() || preparation.blocked} aria-label="Send" aria-keyshortcuts="Shift+Enter" title="Send (Shift+Enter)"><Icon name="send"/></button><span className="send-shortcut">shift+enter</span></div>}
      </div>
    </form>
  </div></div>;
}
