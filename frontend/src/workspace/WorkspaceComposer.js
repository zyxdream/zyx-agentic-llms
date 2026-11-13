import React, {useLayoutEffect, useRef} from 'react';
import {ActionMenu, Icon} from './WorkspaceUI';

export default function WorkspaceComposer({query, onQuery, mode, setMode, onSubmit, onAttach, disabled, busy, active, controlBusy, onStop, preparation, onSources, showPreparationError = true}) {
  const ref = useRef(null);
  useLayoutEffect(() => { if (ref.current) { ref.current.style.height = 'auto'; ref.current.style.height = `${Math.min(ref.current.scrollHeight || 58, 180)}px`; } }, [query]);
  const unavailable = preparation.documents.filter(d => d.status !== 'ready' && d.status !== 'cancelled' && !preparation.excludedIds.has(d.document_id));
  const staged = preparation.documents.filter(d => d.status !== 'cancelled' && !d.committed && d.scope === 'chat');
  return <div className="composer-dock"><div className="composer-container">
    {preparation.error && showPreparationError && <p className="composer-notice" role="alert">{preparation.error} <button onClick={onSources}>View sources</button></p>}
    {(preparation.transferring > 0 || unavailable.length > 0 || preparation.blocked) && <p className="composer-notice" aria-live="polite">{preparation.transferring ? 'Uploading files…' : unavailable.length ? `${unavailable.length} source${unavailable.length === 1 ? '' : 's'} not ready. Wait for preparation, retry, or exclude unavailable sources.` : 'Loading source status…'} <button onClick={onSources}>View sources</button></p>}
    {staged.length > 0 && <div className="attachment-chips">{staged.slice(0,3).map(d => <button key={d.document_id} onClick={onSources} title={`${d.filename} — ${d.status}`}>{d.filename}<span>{d.status}</span></button>)}{staged.length > 3 && <button onClick={onSources}>+{staged.length - 3} more</button>}</div>}
    <form className="workspace-composer" onSubmit={onSubmit}>
      <label className="sr-only" htmlFor="workspace-assignment">Assignment</label><textarea id="workspace-assignment" ref={ref} rows={2} aria-label="Assignment" value={query} onChange={event => onQuery(event.target.value)} disabled={disabled} placeholder="Ask a question, analyze documents, or draft something…" onKeyDown={event => {
        if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); if (!busy && !active && query.trim() && !preparation.blocked && !disabled) event.currentTarget.form.requestSubmit(); }
      }}/>
      <div className="composer-tools"><div className="composer-options"><ActionMenu label="Attach files" icon="attach" disabled={disabled} items={[
        {label:'Add to conversation',icon:'attach',onClick:() => onAttach('chat')},
        {label:'Add to project',icon:'folder',onClick:() => onAttach('matter')},
      ]}/><label className="mode-control"><span className="sr-only">Reasoning mode</span><select aria-label="Reasoning mode" value={mode} onChange={event => setMode(event.target.value)}><option value="fast">Fast</option><option value="deep">Deep</option></select></label>
      {preparation.excludedIds.size > 0 && <button className="exclusion-count" type="button" onClick={onSources}>{preparation.excludedIds.size} excluded</button>}</div>
      {active ? <button type="button" className="send-button" disabled={controlBusy} aria-label="Cancel request" title="Stop request" onClick={onStop}><Icon name="stop"/></button> : <button className="send-button" disabled={disabled || busy || !query.trim() || preparation.blocked} aria-label="Send" title="Send"><Icon name="send"/></button>}
      </div>
    </form><p className="composer-caption">Enter to send · Shift + Enter for a new line</p>
  </div></div>;
}
