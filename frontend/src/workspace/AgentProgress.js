import React, { useState, useRef, useEffect, useCallback } from 'react';
import axios from 'axios';
import { API_URL } from '../Constants';
import { ensureHeaders } from '../auth/useSetHeaders';
import FlexibleDocumentPreview from './FlexibleDocumentPreview';
import { isMarkdownDraft } from './documentMarkdown';
import { downloadDocumentRevision } from './documentDownload';
import { getDraftDownloadErrorMessage } from './downloadError';
import AuthorityResearchResult, { AuthoritySourceResult } from './AuthorityResearchResult';

export default function AgentProgress({ workflowId, draftId, value }) {
  const [saved, setSaved] = useState(null);
  const [artifact, setArtifact] = useState(null);
  const [error, setError] = useState('');
  const [downloading, setDownloading] = useState(null);
  const version = useRef(0);
  const progressRevision = useRef(0);
  const details = useRef(null);
  useEffect(() => {
    version.current += 1;
    setSaved(null);
    setArtifact(null);
    setError('');
    setDownloading(null);
    return () => { version.current += 1; };
  }, [workflowId]);
  const progress = saved || value;
  const load = useCallback(async () => {
    const current = version.current;
    const request = ++progressRevision.current;
    try {
      await ensureHeaders();
      if (current !== version.current || request !== progressRevision.current) return;
      const response = await axios.get(`${API_URL}workspace/runs/${workflowId}`);
      if (current !== version.current || request !== progressRevision.current) return;
      setSaved(response.data.info?.agent || null);
      setError('');
    } catch (_) {
      if (current === version.current && request === progressRevision.current) setError('Could not load saved steps.');
    }
  }, [workflowId]);
  useEffect(() => {
    progressRevision.current += 1;
    setSaved(null);
    // A confirmed Stop drops its stale live snapshot. An expanded viewer
    // reloads the server's cancelled steps while preserving saved artifacts.
    if (!value && details.current?.open) load();
  }, [value, load]);
  const read = async (id) => {
    const current = version.current;
    try {
      await ensureHeaders();
      const response = await axios.get(`${API_URL}workspace/runs/${workflowId}/artifacts/${id}`);
      if (current !== version.current) return;
      setArtifact({ ...response.data, id: response.data.id || id });
      setError('');
    } catch (_) { if (current === version.current) setError('This saved result is unavailable.'); }
  };
  const download = async (file) => {
    const current = version.current;
    const artifactId = artifact.id;
    setDownloading(file.id);
    try {
      await ensureHeaders();
      const response = await axios.get(
        `${API_URL}workspace/runs/${encodeURIComponent(workflowId)}/artifacts/${encodeURIComponent(artifactId)}/files/${encodeURIComponent(file.id)}`,
        { responseType: 'blob' },
      );
      if (current !== version.current) return;
      const url = window.URL.createObjectURL(response.data);
      const link = document.createElement('a');
      try {
        link.href = url;
        link.download = file.filename;
        document.body.appendChild(link);
        link.click();
      } finally {
        link.remove();
        window.URL.revokeObjectURL(url);
      }
      setError('');
    } catch (_) { if (current === version.current) setError('Could not download this saved file.'); }
    finally { if (current === version.current) setDownloading(null); }
  };
  const downloadExport = async () => {
    const current = version.current;
    const exported = artifact.content;
    setDownloading(artifact.id);
    try {
      await downloadDocumentRevision({ draftId, revisionId: exported.source_revision_id,
        format: exported.format, preset: exported.preset, filename: exported.filename,
        isCurrent: () => current === version.current });
      if (current === version.current) setError('');
    } catch (failure) {
      if (current === version.current) setError(await getDraftDownloadErrorMessage(failure));
    } finally { if (current === version.current) setDownloading(null); }
  };
  return (
    <details ref={details} onToggle={(event) => { if (event.currentTarget.open && !progress) load(); }}>
      <summary>Steps and saved results</summary>
      <button type="button" onClick={load}>Refresh steps</button>
      {progress?.stopping_reason && <p>{progress.stopping_reason}</p>}
      <ol>{(progress?.steps || []).map((step) => (
        <li key={step.id}>
          {step.operation.replaceAll('_', ' ')} — {step.result?.error === true
            ? 'Operation error'
            : step.status === 'ready' ? 'Completed' : step.status}
          {step.result?.message && <p>{step.result.message}</p>}
          {step.result?.artifact_id && <button type="button" onClick={() => read(step.result.artifact_id)}>View saved result</button>}
        </li>
      ))}</ol>
      {!!progress?.artifacts?.some(item => item.kind === 'document_export') && <ul>
        {progress.artifacts.filter(item => item.kind === 'document_export').map((item, index) => <li key={item.id}>
          <button type="button" onClick={() => read(item.id)}>View document export {index + 1}</button>
        </li>)}
      </ul>}
      {!!progress?.artifacts?.some(item => item.kind === 'authority_research') && <details>
        <summary>Saved authority research</summary>
        <ul>{progress.artifacts.filter(item => item.kind === 'authority_research').map((item, index) => <li key={item.id}>
          <button type="button" onClick={() => read(item.id)}>View saved research {index + 1}</button>
        </li>)}</ul>
      </details>}
      {error && <p role="alert">{error}</p>}
      {artifact && <div>
        <button type="button" onClick={() => setArtifact(null)}>Close saved result</button>
        <div style={{ maxHeight: 400, overflow: 'auto' }}>
          {artifact.kind === 'authority_research'
            ? <AuthorityResearchResult content={artifact.content} onRead={read} />
            : artifact.kind === 'authority_source'
              ? <AuthoritySourceResult content={artifact.content} onRead={read} />
              : artifact.kind === 'draft' ? (isMarkdownDraft(artifact.content.draft)
            ? <FlexibleDocumentPreview draft={artifact.content.draft} showExport={false} />
            : <>
            <h3>{artifact.content.draft?.title}</h3>
            {(artifact.content.draft?.sections || []).map((section) => <section key={section.section_id}>
              <h4>{section.section_name}</h4><p style={{ whiteSpace: 'pre-wrap' }}>{section.section_content}</p>
            </section>)}
          </>) : artifact.kind === 'document_export' ? <>
            <h4>{artifact.content.filename}</h4>
            <p>Format: {artifact.content.format}; layout: {artifact.content.preset}</p>
            <p>Source revision: {artifact.content.source_revision_id}</p>
            <button type="button" disabled={!draftId || downloading === artifact.id} onClick={downloadExport}>
              {downloading === artifact.id ? 'Preparing download…' : `Download ${artifact.content.filename}`}
            </button>
          </> : artifact.kind === 'python_execution' ? <>
            <p>Python execution — {artifact.content.status}</p>
            {artifact.content.text && <p style={{ whiteSpace: 'pre-wrap' }}>{artifact.content.text}</p>}
            {artifact.content.code && <details>
              <summary>Python code</summary>
              <pre><code>{artifact.content.code}</code></pre>
            </details>}
            {artifact.content.stdout && <section><h4>Output</h4><pre>{artifact.content.stdout}</pre></section>}
            {artifact.content.stderr && <section><h4>Error output</h4><pre>{artifact.content.stderr}</pre></section>}
            {artifact.content.traceback && <section><h4>Traceback</h4><pre>{artifact.content.traceback}</pre></section>}
            {artifact.content.result != null && <section><h4>Result</h4>
              <pre>{typeof artifact.content.result === 'string' ? artifact.content.result : JSON.stringify(artifact.content.result, null, 2)}</pre>
            </section>}
            {!!artifact.content.files?.length && <ul>{artifact.content.files.map(file => <li key={file.id}>
              <button type="button" disabled={downloading === file.id} onClick={() => download(file)}>
                {downloading === file.id ? `Downloading ${file.filename}…` : `Download ${file.filename}`}
              </button>
            </li>)}</ul>}
          </> : <p style={{ whiteSpace: 'pre-wrap' }}>{artifact.content.text}</p>}
          {typeof artifact.content.coverage === 'string' && <p>{artifact.content.coverage}</p>}
        </div>
      </div>}
    </details>
  );
}
