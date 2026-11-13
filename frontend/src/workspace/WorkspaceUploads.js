import React, {forwardRef, useEffect, useImperativeHandle, useRef, useState} from 'react';
import {WORKSPACE_ACCEPT, isWorkspaceFilenameSupported} from '../upload/workspaceFileTypes';
import {matchesPendingUpload, validatePreparationTotalSize} from '../upload/documentPreparation';
import {Modal} from './WorkspaceUI';

// Only relative paths from a directory selection enter the upload manifest.
export function reviewFolder(files, policy = {}) {
  const entries = [], skipped = [];
  let folderName = '';
  for (const file of files) {
    const path = (file.webkitRelativePath || file.name).replace(/\\/g, '/');
    const parts = path.split('/');
    if (!folderName && parts.length > 1) folderName = parts[0];
    const relativePath = parts.length > 1 ? parts.slice(1).join('/') : file.name;
    const reason = !isWorkspaceFilenameSupported(file.name) ? 'Unsupported file type'
      : !file.size ? 'Empty file'
      : file.size > (policy.matter_file_bytes ?? 100 * 1024 ** 2) ? 'File exceeds the size limit'
      : path.startsWith('/') || parts.includes('..') || relativePath.length > 1000 ? 'Invalid relative path' : null;
    if (reason) skipped.push({path, reason});
    else entries.push({file, relativePath});
  }
  return {folderName: folderName || 'Selected folder', entries, skipped};
}

export default forwardRef(function WorkspaceUploads({project, chat, documents, policy = {}, onUpload, onError}, ref) {
  const fileInput = useRef(null), folderInput = useRef(null), destination = useRef(null);
  const navigation = useRef({project, chat, epoch:0});
  if (navigation.current.project !== project || navigation.current.chat !== chat) {
    navigation.current = {project, chat, epoch:navigation.current.epoch + 1};
  }
  const [review, setReview] = useState(null);
  useEffect(() => setReview(null), [project, chat]);
  useImperativeHandle(ref, () => ({
    files(scope) {
      destination.current = {...navigation.current, scope};
      fileInput.current?.click();
    },
    folder() {
      if (!('webkitdirectory' in folderInput.current)) {
        onError('Folder upload is unavailable in this browser. Use Upload files instead.');
        return;
      }
      destination.current = {...navigation.current, scope:'matter'};
      folderInput.current.click();
    }
  }), [onError]);
  function selected(event, folder) {
    const files = Array.from(event.target.files || []);
    event.target.value = '';
    const target = destination.current;
    destination.current = null;
    if (!files.length) return;
    if (!target || target.epoch !== navigation.current.epoch) {
      onError('Select the files again for the current project or conversation.');
      return;
    }
    if (folder) {
      const selection = reviewFolder(files, policy);
      setReview({...selection, target, selected:new Set(selection.entries.map((_, i) => i))});
    } else {
      onUpload(files.map(file => ({file})), {scope:target.scope, scopeId:target.scope === 'chat' ? target.chat : target.project});
    }
  }
  const chosen = review?.entries.filter((_, i) => review.selected.has(i)) || [];
  let limitError = '';
  const fresh = chosen.filter(entry => !documents.some(document => matchesPendingUpload(document, entry, {scope:'matter', folderName:review?.folderName})));
  try { validatePreparationTotalSize(fresh, documents, 'matter', policy); } catch (error) { limitError = error.message; }
  return <>
    <input ref={fileInput} type="file" hidden multiple aria-label="Add files" accept={WORKSPACE_ACCEPT} onChange={event => selected(event, false)}/>
    <input ref={folderInput} type="file" hidden multiple webkitdirectory="" aria-label="Upload project folder" onChange={event => selected(event, true)}/>
    {review && <Modal title="Upload folder" onClose={() => setReview(null)} className="folder-upload-dialog">
      <p><strong>{review.folderName}</strong></p>
      <p className="field-help">Import a copy into Project documents. Files will not stay linked to the computer folder.</p>
      <p>{chosen.length} selected · {(chosen.reduce((sum, entry) => sum + entry.file.size, 0) / 1024 ** 2).toLocaleString(undefined, {maximumFractionDigits:1})} MB</p>
      <div className="folder-file-list">{review.entries.map((entry, i) => <label key={entry.relativePath} title={entry.relativePath}><input type="checkbox" checked={review.selected.has(i)} onChange={() => setReview(value => {
        const selected = new Set(value.selected); selected.has(i) ? selected.delete(i) : selected.add(i);
        return {...value, selected};
      })}/><span>{entry.relativePath}</span></label>)}</div>
      {!review.entries.length && <p>No supported, nonempty documents in this folder.</p>}
      {!!review.skipped.length && <details><summary>{review.skipped.length} skipped {review.skipped.length === 1 ? 'file' : 'files'}</summary>{review.skipped.map((entry, i) => <p className="field-help" key={i}>{entry.path} — {entry.reason}</p>)}</details>}
      {limitError && <p role="alert">{limitError}</p>}
      <div className="dialog-actions"><button onClick={() => setReview(null)}>Cancel</button><button className="primary-button" disabled={!chosen.length || !!limitError} onClick={() => {
        if (review.target.epoch !== navigation.current.epoch) return;
        onUpload(chosen, {scope:'matter', scopeId:review.target.project, folderName:review.folderName});
        setReview(null);
      }}>Upload {chosen.length} {chosen.length === 1 ? 'file' : 'files'}</button></div>
    </Modal>}
  </>;
});
