import React, { useCallback, useEffect, useRef, useState } from 'react';
import axios from 'axios';
import { API_URL } from '../Constants';
import { ensureHeaders } from '../auth/useSetHeaders';
import { useDocumentPreparation } from '../upload/useDocumentPreparation';
import { uploadId } from '../upload/documentPreparation';
import { WORKSPACE_ACCEPT } from '../upload/workspaceFileTypes';
import ProjectSidebar from './ProjectSidebar';
import ConversationView from './ConversationView';
import WorkspaceComposer from './WorkspaceComposer';
import WorkspaceInspector from './WorkspaceInspector';
import {Icon, Modal} from './WorkspaceUI';
import {useProjectNavigation} from './useProjectNavigation';
import {workspaceApi as api, readChoice, rememberChoice} from './workspaceApi';
const terminal = v => ['completed', 'failed', 'cancelled'].includes(v?.status);
export default function Workspace({
  onSignOut
}) {
  const [mode, setMode] = useState('fast'),
    [uploadScope, setUploadScope] = useState('chat'),
    [source, setSource] = useState(null);
  const [content, setContent] = useState(null),
    [run, setRun] = useState(null),
    [query, setQuery] = useState('');
  const [error, setError] = useState(''),
    [activity, setActivity] = useState(null);
  const [editing, setEditing] = useState(false),
    [editor, setEditor] = useState(''),
    [baseVersion, setBaseVersion] = useState(null),
    [format, setFormat] = useState('docx');
  const [discard, setDiscard] = useState(null);
  const [sidebarExpanded, setSidebarExpanded] = useState(() => {
    const saved = readChoice('workspace.sidebar');
    return saved ? saved === 'expanded' : !window.matchMedia?.('(max-width: 767px)').matches;
  });
  const toggleSidebar = () => setSidebarExpanded(value => {
    rememberChoice('workspace.sidebar', value ? 'collapsed' : 'expanded');
    return !value;
  });
  const [panel, setPanel] = useState({key:'',open:false,tab:'document'});
  const fileInput = useRef(null), fileDestination = useRef(null), dirtyRef = useRef(false);
  const markdown = content?.document?.markdown ?? (content?.document?.sections || []).map(s => `## ${s.section_name}\n\n${s.section_content}`).join('\n\n');
  dirtyRef.current = editing && editor !== markdown;
  const fail = useCallback(e => setError(e?.response?.data?.msg || e.message || 'The request could not be completed.'), []);
  const beforeNavigate = useCallback(proceed => {
    if (dirtyRef.current) setDiscard(() => proceed); else proceed();
  }, []);
  const nav = useProjectNavigation({beforeNavigate, onError:fail});
  const {project,chat} = nav;
  const panelKey = chat || `project:${project}`;
  const panelOpen = panel.key === panelKey && panel.open;
  const showPanel = tab => {
    const next = {key:panelKey,open:true,tab}; setPanel(next);
    rememberChoice(`workspace.panel:${panelKey}`,JSON.stringify(next));
  };
  const closePanel = () => {
    const next = {...panel,key:panelKey,open:false};setPanel(next);
    rememberChoice(`workspace.panel:${panelKey}`,JSON.stringify(next));
  };
  useEffect(() => {
    let saved; try { saved = JSON.parse(readChoice(`workspace.panel:${panelKey}`,'null')); } catch { /* optional */ }
    setPanel({key:panelKey,open:!!saved?.open,tab:saved?.tab === 'sources' ? 'sources' : 'document'});
  },[panelKey]);
  useEffect(() => {
    const warn = event => { if (dirtyRef.current) { event.preventDefault(); event.returnValue = ''; } };
    window.addEventListener('beforeunload',warn);
    return () => window.removeEventListener('beforeunload',warn);
  },[]);
  const scope = useRef('');
  scope.current = `${project}/${chat}`;
  const navigation = useRef({project,chat,epoch:0});
  if (navigation.current.chat !== chat || navigation.current.project !== project) navigation.current = {project,chat,epoch:navigation.current.epoch + 1};
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
    let alive = true,
      timer;
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
  }, [project, chat, fail]);
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
  function attachFiles(destination) {
    fileDestination.current = {project,chat,scope:destination};
    setUploadScope(destination); fileInput.current?.click();
  }
  async function readSource(path) {
    const origin = scope.current;
    await action(async () => { const value = await api('get',path); if (scope.current === origin) setSource(value); });
  }
  const selectedProject = nav.projects.find(p => String(p.id) === project);
  const title = nav.lists[project]?.find(c => c.id === chat)?.name || content?.name || selectedProject?.name || 'Workspace';
  return <div className={`workspace-shell ${sidebarExpanded ? 'sidebar-expanded' : 'sidebar-collapsed'} ${panelOpen ? 'with-inspector' : ''}`}>
    <ProjectSidebar navigation={nav} beforeNavigate={beforeNavigate} onSignOut={() => beforeNavigate(onSignOut)} expanded={sidebarExpanded} onToggle={toggleSidebar}/>
    <main className="workspace-main"><header className="workspace-toolbar"><div className="conversation-heading">{selectedProject && chat && <span>{selectedProject.name}</span>}<h1 tabIndex={-1}>{title}</h1></div><div className="toolbar-actions"><button aria-label="Sources" aria-pressed={panelOpen && panel.tab === 'sources'} disabled={!project} onClick={() => showPanel('sources')}><Icon name="attach"/><span>Sources</span>{preparation.documents.length > 0 && <span className="count-badge">{preparation.documents.length}</span>}</button><button aria-label="Document" aria-pressed={panelOpen && panel.tab === 'document'} disabled={!content} onClick={() => showPanel('document')}><Icon name="document"/><span>Document</span></button></div></header>
      {error && <p className="workspace-error" role="alert">{error}</p>}
      {!content ? <div className="workspace-empty"><span className="empty-mark"><Icon name="document"/></span><h2>{chat ? 'Opening your conversation…' : project ? 'A fresh place to work' : 'Welcome to Workspace'}</h2><p>{chat ? 'Your saved messages and documents will be here shortly.' : 'Create or select a conversation to begin.'}</p>{project && !chat && <button className="primary-button" onClick={() => nav.createChat(project)} disabled={nav.creating[project]}><Icon name="plus"/>New conversation</button>}</div> : <>
        <ConversationView content={content} run={run} active={active} busy={busy} controlBusy={controlBusy} runAction={runAction} onOpenDocument={() => showPanel('document')}/>
        <WorkspaceComposer query={query} onQuery={value => {setQuery(value);pending.current=null;}} mode={mode} setMode={setMode} onSubmit={submit} onAttach={attachFiles} disabled={!chat} busy={busy} active={active} controlBusy={controlBusy} onStop={() => runAction('cancel')} preparation={preparation} onSources={() => showPanel('sources')} showPreparationError={!(panelOpen && panel.tab === 'sources')}/>
      </>}
      <input ref={fileInput} type="file" hidden multiple aria-label="Add files" accept={WORKSPACE_ACCEPT} onChange={event => {
        const entries = Array.from(event.target.files || []).map(file => ({file})); event.target.value='';
        const destination = fileDestination.current || {project,chat,scope:chat ? uploadScope : 'matter'};
        fileDestination.current = null;
        if (!entries.length) return;
        if (destination.project !== project || destination.chat !== chat) {setError('Select the files again for the current conversation.');return;}
        showPanel('sources');
        action(() => preparation.upload(entries,{scope:destination.scope,scopeId:destination.scope === 'chat' ? chat : project}));
      }}/>
    </main>
    <WorkspaceInspector open={panelOpen} tab={panel.tab} onTab={showPanel} onClose={closePanel} content={content} markdown={markdown} editing={editing} editor={editor} setEditor={setEditor} busy={busy} active={active} onSave={save} onEdit={() => {setEditor(markdown);setBaseVersion(content.document_version);setEditing(true);}} onDiscard={() => setEditing(false)} format={format} setFormat={setFormat} onDownload={download} preparation={preparation} chat={chat} uploadScope={uploadScope} setUploadScope={setUploadScope} onAttach={attachFiles} source={source} onRead={document => readSource(`sources/${document.document_id}/content`)} onCloseSource={() => setSource(null)} onNextSource={() => readSource(`sources/${source.id}/content?start=${source.end}`)} onRetry={document => action(() => preparation.retry(document))} onRemove={document => action(() => preparation.remove(document))}/>
    {discard && <Modal title="Unsaved document changes" onClose={() => setDiscard(null)}><p>Your document has unsaved changes. Discard them before leaving this conversation?</p><div className="dialog-actions"><button data-autofocus onClick={() => setDiscard(null)}>Keep editing</button><button className="danger-button" onClick={() => {const proceed=discard;dirtyRef.current=false;setEditing(false);setDiscard(null);proceed();}}>Discard changes</button></div></Modal>}
  </div>;
}
