import React, {useEffect, useRef, useState} from 'react';
import {downloadGeneratedFile} from './generatedFileDownload';
import {Icon} from './WorkspaceUI';

export default function GeneratedFiles({runId, files, scope = ''}) {
  const selected = Array.isArray(files) ? files.filter(file => file && ['file_id','execution_id','filename'].every(key => typeof file[key] === 'string' && file[key])) : [];
  const identity = JSON.stringify([scope,runId,selected.map(file => [file.file_id,file.execution_id])]);
  const navigation = useRef({identity, epoch:0});
  if (navigation.current.identity !== identity) navigation.current = {identity, epoch:navigation.current.epoch + 1};
  const [downloading, setDownloading] = useState(null), [error, setError] = useState('');
  useEffect(() => {
    setDownloading(null);setError('');
    return () => {navigation.current.epoch += 1;};
  }, [identity]);
  if (!runId || !selected.length) return null;
  async function download(file) {
    const epoch = navigation.current.epoch;
    const isCurrent = () => navigation.current.epoch === epoch;
    setDownloading(file.file_id);setError('');
    try {
      await downloadGeneratedFile({runId, executionId:file.execution_id, fileId:file.file_id, filename:file.filename, isCurrent});
    } catch (_) {
      if (isCurrent()) setError('Could not download this file. Try again.');
    } finally {
      if (isCurrent()) setDownloading(null);
    }
  }
  return <div className="generated-files">
    <ul aria-label="Output files">{selected.map(file => <li key={file.file_id}>
      <button type="button" aria-label={`Download ${file.filename}`} disabled={downloading !== null} onClick={() => download(file)}>
        <Icon name="document"/><span className="generated-file-name">{file.filename}</span><span className="generated-file-action">{downloading === file.file_id ? 'Downloading…' : 'Download'}</span>
      </button>
    </li>)}</ul>
    {error && <p role="alert">{error}</p>}
  </div>;
}
