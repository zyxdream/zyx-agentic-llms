import React, {useEffect, useRef} from 'react';
import AgentProgress from './AgentProgress';
import RequestUserInputPanel from './RequestUserInputPanel';
import {renderDocumentMarkdown} from './documentMarkdown';
import {promptAttachments} from './promptAttachments';

export default function ConversationView({content, run, active, busy, controlBusy, runAction, onOpenDocument, onOpenAttachment}) {
  const viewport = useRef(null), atBottom = useRef(true);
  useEffect(() => { if (atBottom.current && viewport.current) viewport.current.scrollTop = viewport.current.scrollHeight; }, [content, run]);
  useEffect(() => { atBottom.current = true; }, [content?.id]);
  return <div ref={viewport} className="conversation-scroll" onScroll={() => {
    const el = viewport.current; atBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 100;
  }}><section className="conversation-reading" aria-label="Conversation">
    {!content.history.length && !active && <div className="conversation-welcome"><h2>What’s next?</h2></div>}
    <div className="history">{content.history.map(turn => <article className="conversation-turn" key={turn.id}>
      <div className="user-bubble"><h3 className="sr-only">You</h3><p className="user-message">{turn.query}</p>{promptAttachments(turn.metadata).length > 0 && <div className="message-attachments" aria-label="Message attachments">{promptAttachments(turn.metadata).map((file, index) => <button key={`${file.id}:${index}`} title={file.name} disabled={!file.id} onClick={() => onOpenAttachment(file.id)}>{file.name}</button>)}</div>}</div>
      <div className="assistant-message"><h3>Workspace</h3><div className="markdown-body" dangerouslySetInnerHTML={{__html: renderDocumentMarkdown(turn.response || turn.status || 'Working…')}}/>
        {turn.run_id && <><AgentProgress workflowId={turn.run_id} draftId={content.document_id}/>{['failed','cancelled'].includes(turn.status) && !active && <button className="subtle-button" disabled={busy} onClick={() => runAction('retry', undefined, turn.run_id)}>Retry request</button>}</>}
      </div>
    </article>)}</div>
    {active && <section className="run-status" role="status"><div className="run-status-heading"><span className="working-dot"/><p>{run.info?.status || 'Working on your request…'}</p></div><AgentProgress workflowId={run.run_id} draftId={content.document_id} value={run.info?.agent}/></section>}
    {run?.status === 'awaiting_user_confirmation' && <RequestUserInputPanel key={run.run_id} request={run.value?.confirmation || run.value || {}} loading={controlBusy} onSubmit={decision => runAction('confirm', {decision})} onCancel={() => runAction('cancel')}/>}
    {!!(content.document?.markdown || content.document?.sections?.length) && <button className="document-shortcut" onClick={onOpenDocument}>Open output <span aria-hidden="true">↗</span></button>}
  </section></div>;
}
