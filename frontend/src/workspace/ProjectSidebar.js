import React, {useRef, useState} from 'react';
import {ActionMenu, Icon, IconButton, Modal} from './WorkspaceUI';

const message = error => error?.response?.data?.msg || error.message || 'The change could not be saved.';

export function ProjectDialog({onSave, onClose}) {
  const [folder, setFolder] = useState(''), [custom, setCustom] = useState(''), [naming, setNaming] = useState('custom');
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const fallback = useRef(null), customEdited = useRef(false);
  const name = naming === 'folder' ? folder : custom;
  function acceptFolder(value) {
    if (!value) return;
    setFolder(value); setNotice('');
    if (!customEdited.current) setNaming('folder');
  }
  async function chooseFolder() {
    if (typeof window.showDirectoryPicker === 'function') {
      try { const directory = await window.showDirectoryPicker({mode: 'read'}); acceptFolder(directory.name); }
      catch (failure) { if (failure.name !== 'AbortError') setNotice('Folder selection is unavailable. You can enter a project name instead.'); }
    } else fallback.current?.click();
  }
  return <Modal title="New project" onClose={onClose} busy={busy}><form onSubmit={async event => {
    event.preventDefault(); if (!name.trim() || busy) return;
    setBusy(true); setError('');
    try { await onSave(name.trim()); onClose(); } catch (failure) { setError(message(failure)); } finally { setBusy(false); }
  }}>
    <p className="muted">A place for related conversations and documents.</p>
    <button type="button" className="folder-picker" onClick={chooseFolder} disabled={busy}><Icon name="folder"/>{folder ? 'Choose another folder' : 'Choose folder'}<span className="muted">Optional</span></button>
    <input ref={fallback} type="file" hidden webkitdirectory="" directory="" multiple aria-label="Choose project folder" onChange={event => {
      const files = Array.from(event.target.files || []);
      const root = files[0]?.webkitRelativePath?.split('/')[0];
      if (root) acceptFolder(root);
      else if (files.length) setNotice('The folder name is unavailable. Enter a project name instead.');
      event.target.value = '';
    }}/>
    {folder && <><p className="chosen-folder"><Icon name="folder"/>{folder}</p><fieldset className="name-options"><legend className="sr-only">Project naming</legend>
      <label><input type="radio" name="naming" value="folder" checked={naming === 'folder'} onChange={() => setNaming('folder')} disabled={busy}/>Use folder name</label>
      <label><input type="radio" name="naming" value="custom" checked={naming === 'custom'} onChange={() => setNaming('custom')} disabled={busy}/>Custom name</label>
    </fieldset></>}
    <p className="field-help">Only the folder name is used. Add documents separately.</p>
    {notice && <p className="field-help">{notice}</p>}
    <label>Project name<input data-autofocus aria-label="Project name" value={name} maxLength={250} disabled={busy || naming === 'folder'} required onChange={event => { customEdited.current = true; setCustom(event.target.value); }}/></label>
    {error && <p role="alert">{error}</p>}
    <div className="dialog-actions"><button type="button" onClick={onClose} disabled={busy}>Cancel</button><button className="primary-button" disabled={busy || !name.trim()}>{busy ? 'Creating…' : 'Create project'}</button></div>
  </form></Modal>;
}

export function RenameDialog({kind, name: initialName, onSave, onClose}) {
  const [name, setName] = useState(initialName), [busy, setBusy] = useState(false), [error, setError] = useState('');
  return <Modal title={`Rename ${kind}`} onClose={onClose} busy={busy}><form onSubmit={async event => {
    event.preventDefault(); if (!name.trim() || busy) return;
    setBusy(true); setError('');
    try { await onSave(name.trim()); onClose(); } catch (failure) { setError(message(failure)); } finally { setBusy(false); }
  }}><label>{kind === 'project' ? 'Project' : 'Conversation'} name<input data-autofocus aria-label={kind === 'project' ? 'Project name' : 'Conversation name'} value={name} onChange={event => setName(event.target.value)} maxLength={250} required disabled={busy}/></label>
    {error && <p role="alert">{error}</p>}<div className="dialog-actions"><button type="button" disabled={busy} onClick={onClose}>Cancel</button><button className="primary-button" disabled={busy || !name.trim()}>{busy ? 'Saving…' : 'Save name'}</button></div>
  </form></Modal>;
}

function DeleteDialog({project, onDelete, onClose}) {
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  return <Modal title="Delete project" onClose={onClose} busy={busy}>
    <p>Delete <strong>{project.name}</strong> and its conversations, documents, and saved results? This cannot be undone.</p>
    {error && <p role="alert">{error}</p>}
    <div className="dialog-actions"><button type="button" data-autofocus onClick={onClose} disabled={busy}>Cancel</button><button className="danger-button" disabled={busy} onClick={async () => {
      setBusy(true); setError('');
      try { await onDelete(project.id); onClose(); }
      catch (failure) {
        const data = failure.response?.data;
        setError(data?.code === 'project_busy' ? `${data.active_runs} active request(s) and ${data.active_preparations} document(s) being prepared. Finish or cancel this work before deleting the project.` : message(failure));
      } finally { setBusy(false); }
    }}>{busy ? 'Deleting…' : 'Delete project'}</button></div>
  </Modal>;
}

export default function ProjectSidebar({navigation: nav, onSignOut, beforeNavigate, expanded, onToggle}) {
  const [dialog, setDialog] = useState(null);
  return <>
    <aside className="project-sidebar" aria-label="Project navigation">
      <div className="sidebar-brand"><button type="button" className="sidebar-toggle" aria-label={expanded ? 'Collapse sidebar' : 'Expand sidebar'} title={expanded ? 'Collapse sidebar' : 'Expand sidebar'} aria-expanded={expanded} aria-controls="workspace-project-navigation" onClick={onToggle}><Icon name="sidebar"/><strong hidden={!expanded}>Workspace</strong></button></div>
      <div className="sidebar-section-heading"><h2 hidden={!expanded}>Projects</h2><IconButton icon="plus" label="New project" onClick={() => beforeNavigate(() => setDialog({kind: 'create'}))}/></div>
      <div className="sidebar-shortcuts" hidden={expanded}><IconButton icon="edit" label="New conversation in current project" disabled={!nav.project || nav.creating[nav.project]} onClick={() => nav.createChat(nav.project)}/></div>
      <nav id="workspace-project-navigation" aria-label="Projects" className="project-tree" hidden={!expanded}><ul>{nav.projects.map(project => {
        const id = String(project.id), expanded = nav.expanded.includes(id), selected = nav.project === id;
        return <li key={id} className="project-group"><div className={`project-row ${selected ? 'selected-project' : ''}`}>
          <IconButton className={`expand-button ${expanded ? 'expanded' : ''}`} icon="chevron" label={`${expanded ? 'Collapse' : 'Expand'} ${project.name}`} aria-expanded={expanded} onClick={() => nav.toggle(id)}/>
          <button className="project-name" title={project.name} aria-label={`Open project ${project.name}`} aria-current={selected ? 'true' : undefined} onClick={() => nav.selectProject(id)}><Icon name="folder"/><span>{project.name}</span></button>
          <IconButton icon="plus" label={`New conversation in ${project.name}`} disabled={nav.creating[id]} onClick={() => nav.createChat(id)}/>
          <ActionMenu label={`Project actions for ${project.name}`} items={[
            {label: 'Rename', icon: 'edit', onClick: () => setDialog({kind: 'project', project})},
            {label: 'Delete', icon: 'trash', danger: true, onClick: () => setDialog({kind: 'delete', project})},
          ]}/>
        </div>{expanded && <ul className="conversation-list" aria-label={`Conversations in ${project.name}`}>
          {nav.loading[id] && !nav.lists[id] && <li className="tree-notice">Loading conversations…</li>}
          {nav.listErrors[id] && <li className="tree-notice">{nav.listErrors[id]} <button onClick={() => nav.retryList(id)}>Retry</button></li>}
          {!nav.loading[id] && !nav.listErrors[id] && nav.lists[id]?.length === 0 && <li className="tree-notice">No conversations yet</li>}
          {(nav.lists[id] || []).map(conversation => <li className={`conversation-row ${nav.chat === conversation.id ? 'selected-conversation' : ''}`} key={conversation.id}>
            <button className="conversation-name" title={conversation.name} aria-current={nav.chat === conversation.id ? 'page' : undefined} onClick={() => nav.selectChat(id, conversation.id)}>{conversation.name}</button>
            <ActionMenu label={`Conversation actions for ${conversation.name}`} items={[{label: 'Rename', onClick: () => setDialog({kind: 'conversation', project, conversation})}]}/>
          </li>)}
        </ul>}</li>;
      })}</ul>{nav.projects.length === 0 && <p className="sidebar-empty">Create a project to get started.</p>}</nav>
      <div className="sidebar-footer"><button className="sign-out" aria-label="Sign out" title="Sign out" onClick={onSignOut}><Icon name="logout"/><span hidden={!expanded}>Sign out</span></button></div>
    </aside>
    {dialog?.kind === 'create' && <ProjectDialog onSave={nav.createProject} onClose={() => setDialog(null)}/>}
    {dialog?.kind === 'project' && <RenameDialog kind="project" name={dialog.project.name} onSave={name => nav.renameProject(dialog.project.id, name)} onClose={() => setDialog(null)}/>}
    {dialog?.kind === 'conversation' && <RenameDialog kind="conversation" name={dialog.conversation.name} onSave={name => nav.renameChat(dialog.project.id, dialog.conversation.id, name)} onClose={() => setDialog(null)}/>}
    {dialog?.kind === 'delete' && <DeleteDialog project={dialog.project} onDelete={nav.deleteProject} onClose={() => setDialog(null)}/>}
  </>;
}
