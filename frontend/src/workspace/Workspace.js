import React, { useCallback, useEffect, useRef, useState } from 'react';
import axios from 'axios';
import { API_URL } from '../Constants';
import { ensureHeaders } from '../auth/useSetHeaders';
import { useDocumentPreparation } from '../upload/useDocumentPreparation';
import { uploadId } from '../upload/documentPreparation';
import { WORKSPACE_ACCEPT } from '../upload/workspaceFileTypes';
import PreparedDocumentSummary from '../upload/PreparedDocumentSummary';
import AgentProgress from './AgentProgress';
import RequestUserInputPanel from './RequestUserInputPanel';
import { renderDocumentMarkdown } from './documentMarkdown';
const terminal = v => ['completed', 'failed', 'cancelled'].includes(v?.status);
async function api(method, path, data) {
  await ensureHeaders();
  return (await axios({
    method,
    url: `${API_URL}workspace/${path}`,
    data
  })).data;
}
function choice(k) {
  try {
    return sessionStorage.getItem(k) || '';
  } catch {
    return '';
  }
}
function remember(k, v) {
  try {
    sessionStorage.setItem(k, v);
  } catch {/* optional */}
}
export default function Workspace({
  onSignOut
}) {
  const [mode, setMode] = useState('fast'),
    [uploadScope, setUploadScope] = useState('chat'),
    [source, setSource] = useState(null);
  const [projects, setProjects] = useState([]),
    [project, setProject] = useState(choice('workspace.project'));
  const [chats, setChats] = useState([]),
    [chat, setChat] = useState(choice('workspace.conversation'));
  const [content, setContent] = useState(null),
    [run, setRun] = useState(null),
    [query, setQuery] = useState('');
  const [name, setName] = useState(''),
    [chatName, setChatName] = useState(''),
    [error, setError] = useState(''),
    [activity, setActivity] = useState(null);
  const [editing, setEditing] = useState(false),
    [editor, setEditor] = useState(''),
    [baseVersion, setBaseVersion] = useState(null),
    [format, setFormat] = useState('docx');
  const scope = useRef('');
  scope.current = chat;
  const navigation = useRef({
    chat,
    epoch: 0
  });
  if (navigation.current.chat !== chat) navigation.current = {
    chat,
    epoch: navigation.current.epoch + 1
  };
  const activitySequence = useRef(0);
  const controlSequence = useRef(0);
  const busy = !!activity && activity.epoch === navigation.current.epoch && !(activity.runId && activity.wasActive && run?.run_id === activity.runId && terminal(run));
  const controlBusy = busy && activity?.runId === run?.run_id;
  function navigationGuard() {
    const epoch = navigation.current.epoch;
    return () => navigation.current.epoch === epoch;
  }
  const pending = useRef(null);
  const preparation = useDocumentPreparation({
    draftId: chat || null,
    projectId: project || null
  });
  const fail = useCallback(e => setError(e?.response?.data?.msg || e.message || 'The request could not be completed.'), []);
  async function action(fn, metadata = {}) {
    const id = ++activitySequence.current;
    const current = navigationGuard();
    setActivity({
      id,
      epoch: navigation.current.epoch,
      ...metadata
    });
    setError('');
    try {
      await fn();
    } catch (e) {
      if (current() && (!metadata.isCurrent || metadata.isCurrent())) fail(e);
    } finally {
      setActivity(value => value?.id === id ? null : value);
    }
  }
  useEffect(() => {
    let alive = true;
    api('get', 'projects').then(v => {
      if (!alive) return;
      setProjects(v.projects);
      setProject(p => v.projects.some(x => String(x.id) === p) ? p : String(v.projects[0]?.id || ''));
    }).catch(e => alive && fail(e));
    return () => {
      alive = false;
    };
  }, [fail]);
  useEffect(() => {
    let alive = true;
    remember('workspace.project', project);
    setChats([]);
    setContent(null);
    setRun(null);
    if (project) api('get', `projects/${project}/conversations`).then(v => {
      if (!alive) return;
      setChats(v.conversations);
      setChat(c => v.conversations.some(x => x.id === c) ? c : v.conversations[0]?.id || '');
    }).catch(e => alive && fail(e));else setChat('');
    return () => {
      alive = false;
    };
  }, [project, fail]);
  useEffect(() => {
    let alive = true,
      timer;
    remember('workspace.conversation', chat);
    setContent(null);
    setRun(null);
    setEditing(false);
    setEditor('');
    setSource(null);
    setQuery('');
    pending.current = null;
    if (chat) {
      const reload = async () => {
        try {
          const v = await api('get', `conversations/${chat}`);
          if (!alive) return;
          setContent(v);
          setRun(v.active_run);
          setError('');
          // Run polling owns active progress; do not start a second conversation poller.
        } catch (e) {
          if (alive) {
            fail(e);
            timer = setTimeout(reload, 5000);
          }
        }
      };
      reload();
    }
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [chat, fail]);
  const runId = run?.run_id,
    runState = run?.status;
  useEffect(() => {
    if (!runId || terminal({
      status: runState
    })) return;
    let alive = true,
      timer;
    const poll = async () => {
      try {
        const next = await api('get', `runs/${runId}`);
        if (!alive) return;
        if (terminal(next)) {
          const v = await api('get', `conversations/${chat}`);
          if (!alive) return;
          setContent(v);
          setRun(v.active_run || next);
        } else {
          setRun(next);
          timer = setTimeout(poll, 2000);
        }
      } catch (e) {
        if (alive) {
          fail(e);
          timer = setTimeout(poll, 5000);
        }
      }
    };
    timer = setTimeout(poll, 1000);
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [runId, runState, chat, fail]);
  const active = run && !terminal(run);
  async function submit(e) {
    e.preventDefault();
    const origin = chat;
    const stillHere = navigationGuard();
    const sequence = ++controlSequence.current;
    const current = () => stillHere() && controlSequence.current === sequence;
    await action(async () => {
      const catalog = await preparation.refresh();
      if (!current()) return;
      if (preparation.transferring) throw new Error('Wait for uploads to finish.');
      const excluded = catalog.excludedIds || preparation.excludedIds;
      if (catalog.documents.some(doc => doc.status !== 'ready' && doc.status !== 'cancelled' && !excluded.has(doc.document_id))) {
        throw new Error('Wait for document preparation or exclude unavailable sources.');
      }
      const fingerprint = JSON.stringify([origin, query, mode, [...excluded], catalog.documents.map(doc => [doc.document_id, doc.generation, doc.artifact_id])]);
      if (pending.current?.fingerprint !== fingerprint) pending.current = {
        fingerprint,
        id: `wf_${uploadId()}`
      };
      const body = {
        query,
        run_id: pending.current.id,
        assistant_mode: mode,
        excludedDocumentIds: [...excluded],
        chatFiles: catalog.documents.filter(doc => doc.scope === 'chat' && doc.status !== 'cancelled' && !excluded.has(doc.document_id)).map(doc => ({
          name: doc.document_id,
          filename: doc.filename
        })),
        promptAttachments: catalog.documents.filter(doc => doc.scope === 'chat' && doc.status !== 'cancelled' && !excluded.has(doc.document_id) && !doc.committed).map(doc => ({
          file_id: doc.document_id,
          filename: doc.filename
        }))
      };
      const value = await api('post', `conversations/${origin}/runs`, body);
      if (!current()) return;
      setRun(value);
      preparation.clearExclusions({
        projectId: project,
        draftId: origin
      });
      setQuery('');
      pending.current = null;
      const saved = await api('get', `conversations/${origin}`);
      if (current()) setContent(saved);
    }, {
      isCurrent: current
    });
  }
  async function runAction(verb, data, target = run?.run_id) {
    if (!target) return;
    const origin = chat;
    const stillHere = navigationGuard();
    const sequence = ++controlSequence.current;
    const current = () => stillHere() && controlSequence.current === sequence;
    await action(async () => {
      await api('post', `runs/${target}/${verb}`, data);
      if (!current()) return;
      const saved = await api('get', `conversations/${origin}`);
      const progress = saved.active_run || (await api('get', `runs/${target}`));
      if (!current()) return;
      setContent(saved);
      setRun(progress);
    }, {
      runId: target,
      wasActive: run?.run_id === target && !terminal(run),
      isCurrent: current
    });
  }
  async function save() {
    const origin = chat;
    const current = navigationGuard();
    const documentId = content.document_id;
    await action(async () => {
      const draft = {
        ...content.document,
        format_version: 2,
        source_format: 'markdown',
        markdown: editor,
        revision_id: null,
        parent_revision_id: content.document.revision_id || null
      };
      const v = await api('patch', `documents/${content.document_id}`, {
        draft,
        expected_version: baseVersion
      });
      if (current()) {
        if (v.document_id !== documentId || v.conversation_id !== origin) {
          throw new Error('The save response belongs to another document or conversation. Your edits have been kept.');
        }
        setContent(c => ({
          ...c,
          document: v.document,
          document_version: v.document_version
        }));
        setEditing(false);
      }
    });
  }
  async function download() {
    await action(async () => {
      await ensureHeaders();
      const r = await axios.post(`${API_URL}workspace/documents/${content.document_id}/download`, {
        format
      }, {
        responseType: 'blob'
      });
      const url = URL.createObjectURL(r.data),
        a = document.createElement('a');
      a.href = url;
      a.download = `Document.${format === 'markdown' ? 'md' : format}`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    });
  }
  const markdown = content?.document?.markdown ?? (content?.document?.sections || []).map(s => `## ${s.section_name}\n\n${s.section_content}`).join('\n\n');
  return <div className="workspace-shell"><header><div><strong>Workspace</strong><small>zyx-agentic-llms</small></div><button onClick={onSignOut}>Sign out</button></header>
 <aside><h2>Projects</h2><select aria-label="Project" value={project} onChange={e => {
        setChat('');
        setProject(e.target.value);
      }}><option value="">Choose a project</option>{projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select>
 <form onSubmit={e => {
        e.preventDefault();
        action(async () => {
          const p = await api('post', 'projects', {
            name
          });
          setProjects(v => [...v, p]);
          setProject(String(p.id));
          setChat('');
          setName('');
        });
      }}><input aria-label="New project name" placeholder="Project name" value={name} onChange={e => setName(e.target.value)} /><button disabled={busy || !name.trim()}>Add project</button></form>
 <h2>Conversations</h2><nav>{chats.map(c => <button key={c.id} aria-current={chat === c.id ? 'page' : undefined} onClick={() => setChat(c.id)}>{c.name}</button>)}</nav>
 <form onSubmit={e => {
        e.preventDefault();
        action(async () => {
          const c = await api('post', `projects/${project}/conversations`, {
            name: chatName || 'New conversation'
          });
          setChats(v => [c, ...v]);
          setChat(c.id);
          setChatName('');
        });
      }}><input aria-label="New conversation name" placeholder="Conversation name" value={chatName} onChange={e => setChatName(e.target.value)} /><button disabled={!project || busy}>New conversation</button></form></aside>
 <main>{error && <p role="alert">{error}</p>}{!content ? <p>Create or select a conversation to begin.</p> : <><h1>{content.name}</h1><div className="workspace-columns"><section aria-label="Conversation"><div className="history">{content.history.map(t => <article key={t.id}><h3>You</h3><p className="user-message">{t.query}</p><h3>Workspace</h3><div dangerouslySetInnerHTML={{
                  __html: renderDocumentMarkdown(t.response || t.status || 'Working…')
                }} />{t.run_id && <><AgentProgress workflowId={t.run_id} draftId={content.document_id} />{['failed', 'cancelled'].includes(t.status) && !active && <button disabled={busy} onClick={() => runAction('retry', undefined, t.run_id)}>Retry request</button>}</>}</article>)}</div>
 {active && <section role="status"><p>{run.info?.status}</p><AgentProgress workflowId={run.run_id} draftId={content.document_id} value={run.info?.agent} /><button disabled={controlBusy} onClick={() => runAction('cancel')}>Cancel request</button></section>}
 {run?.status === 'awaiting_user_confirmation' && <RequestUserInputPanel key={run.run_id} request={run.value?.confirmation || run.value || {}} loading={controlBusy} onSubmit={decision => runAction('confirm', {
              decision
            })} onCancel={() => runAction('cancel')} />}
 <form onSubmit={submit}><label>Reasoning mode<select value={mode} onChange={e => setMode(e.target.value)}><option value="fast">Fast</option><option value="deep">Deep</option></select></label><label>Assignment<textarea aria-label="Assignment" value={query} onChange={e => {
                  setQuery(e.target.value);
                  pending.current = null;
                }} placeholder="Analyze documents, research a question, or draft something…" /></label><button disabled={busy || active || !query.trim() || preparation.blocked}>Send</button></form>
 <section aria-label="Sources"><h2>Sources</h2><label>Upload to<select value={uploadScope} onChange={e => setUploadScope(e.target.value)}><option value="chat">This conversation</option><option value="matter">This project</option></select></label><label>Add files<input type="file" multiple accept={WORKSPACE_ACCEPT} onChange={e => {
                  const entries = Array.from(e.target.files || []).map(file => ({
                    file
                  }));
                  e.target.value = '';
                  action(() => preparation.upload(entries, {
                    scope: uploadScope,
                    scopeId: uploadScope === 'chat' ? chat : project
                  }));
                }} /></label>{preparation.error && <p role="alert">{preparation.error}</p>}{preparation.documents.map(d => <div className="source" key={d.document_id}><label><input type="checkbox" checked={!preparation.excludedIds.has(d.document_id)} onChange={() => preparation.toggleExcluded(d.document_id)} />{d.filename} — {d.status}</label><PreparedDocumentSummary documentId={d.document_id} artifactId={d.artifact_id} metadata={d.metadata} filename={d.filename} /><button disabled={busy} onClick={() => action(async () => {
                  const origin = chat,
                    v = await api('get', `sources/${d.document_id}/content`);
                  if (scope.current === origin) setSource(v);
                })}>Read source</button>{(d.status === 'failed' || d.needs_reupload) && <button onClick={() => action(() => preparation.retry(d))}>Retry preparation</button>}<button disabled={busy || active} onClick={() => action(() => preparation.remove(d))}>Remove source</button></div>)}{source && <section aria-label="Source text"><h3>{source.filename}</h3><p>Characters {source.start}–{source.end} of {source.total_characters}</p><pre>{source.text}</pre><button onClick={() => setSource(null)}>Close source</button>{source.end < source.total_characters && <button onClick={() => action(async () => {
                  const origin = chat,
                    v = await api('get', `sources/${source.id}/content?start=${source.end}`);
                  if (scope.current === origin) setSource(v);
                })}>Next source range</button>}</section>}</section></section>
 <section aria-label="Document"><h2>{content.document?.title || 'Document'}</h2>{editing ? <><textarea className="document-editor" aria-label="Document Markdown" value={editor} onChange={e => setEditor(e.target.value)} /><button disabled={busy || active} onClick={save}>Save document</button><button onClick={() => setEditing(false)}>Discard edits</button></> : <><div className="document-preview" dangerouslySetInnerHTML={{
                __html: renderDocumentMarkdown(markdown)
              }} /><button disabled={busy || active} onClick={() => {
                setEditor(markdown);
                setBaseVersion(content.document_version);
                setEditing(true);
              }}>Edit Markdown</button></>}<label>Download format<select value={format} onChange={e => setFormat(e.target.value)}>{['docx', 'pdf', 'markdown', 'html'].map(f => <option key={f}>{f}</option>)}</select></label><button disabled={busy || !markdown || editing} onClick={download}>Download document</button></section></div></>}</main></div>;
}
