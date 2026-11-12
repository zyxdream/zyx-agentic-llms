import React, { useEffect, useRef, useState } from 'react';
import axios from 'axios';
import { API_URL } from '../Constants';
import { ensureHeaders } from '../auth/useSetHeaders';

export default function PreparedDocumentSummary({ documentId, artifactId, metadata, filename }) {
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);
  const spreadsheet = detail?.spreadsheet || metadata?.spreadsheet;
  const identity = useRef(`${documentId}:${artifactId}`);
  identity.current = `${documentId}:${artifactId}`;
  useEffect(() => { setDetail(null); setError(null); setLoading(false); }, [documentId, artifactId]);
  const load = async event => {
    if (!event.currentTarget.open || detail || loading) return;
    const origin = `${documentId}:${artifactId}`;
    setLoading(true); setError(null);
    try {
      await ensureHeaders();
      const response = await axios.get(`${API_URL}workspace/sources/${encodeURIComponent(documentId)}/summary`,
        { params: artifactId ? { artifactId } : {} });
      if (origin === identity.current) setDetail(response.data);
    } catch (failure) { if (origin === identity.current) setError(failure.response?.data?.msg || 'Summary is unavailable.'); }
    finally { if (origin === identity.current) setLoading(false); }
  };
  return <details onToggle={load} style={{ minWidth: 0, fontSize: 12 }}>
    <summary style={{ cursor: 'pointer' }} aria-label={`Summary for ${filename || documentId}`}>
      {filename || 'Document summary'}{metadata?.headline && metadata.headline !== filename ? ` — ${metadata.headline}` : ''}
    </summary>
    {metadata?.preview && <p>{metadata.preview}</p>}
    {!!metadata?.key_points?.length && <ul>{metadata.key_points.map((point, index) =>
      <li key={index}>{point.text}</li>)}</ul>}
    {spreadsheet && <div>
      <p>Spreadsheet overview: {spreadsheet.sheet_count ?? spreadsheet.sheets?.length ?? 0} sheet(s). Samples are previews of the original data.</p>
      <ul>{(spreadsheet.sheets || []).map(sheet => <li key={sheet.name}>
        <strong>{sheet.name}</strong>{sheet.state && sheet.state !== 'visible' ? ` (${sheet.state})` : ''}
        {sheet.used_range ? ` — ${sheet.used_range}` : ''}
        {sheet.row_count != null && sheet.column_count != null ? `; ${sheet.row_count} rows × ${sheet.column_count} columns` : ''}
        {!!sheet.formula_count && <span>; {sheet.formula_count} formula(s)</span>}
        {!!sheet.missing_formula_cache_count && <span>; {sheet.missing_formula_cache_count} formula result(s) not saved</span>}
        {!!sheet.header_candidates?.length && <ul>{sheet.header_candidates.map(candidate => <li key={candidate.row}>
          Possible header, row {candidate.row}: {(candidate.cells || []).map(cell => `${cell.cell}: ${String(cell.value ?? '')}`).join(' | ')}
        </li>)}</ul>}
        {!!sheet.samples?.length && <details>
          <summary>Sample rows from {sheet.name}</summary>
          <pre style={{ whiteSpace: 'pre-wrap' }}>{sheet.samples.map(row =>
            (row.cells || []).map(cell => `${cell.cell}: ${String(cell.value ?? cell.cached_value ?? cell.formula ?? '')}`).join(' | ')
          ).join('\n')}</pre>
        </details>}
      </li>)}</ul>
      {spreadsheet.truncated && <p>This overview is abbreviated; additional workbook structure is available during analysis.</p>}
    </div>}
    {loading && <div>Loading summary…</div>}
    {error && <div role="alert">{error}</div>}
    {detail && <div style={{ whiteSpace: 'pre-wrap', padding: '8px 0' }}>
      {detail.detailed_digest}
      {!!detail.section_outline?.length && <ul>{detail.section_outline.map(section =>
        <li key={section.record_key}>{section.heading}</li>)}</ul>}
      {!!detail.coverage?.extraction?.gaps?.length && <div style={{ color: '#92400e' }}>
        Coverage notes<ul>{detail.coverage.extraction.gaps.map((gap, index) =>
          <li key={index}>{gap.sheet ? `${gap.sheet}${gap.cell ? `!${gap.cell}` : ''}: ` : ''}{gap.page ? `Page ${gap.page}: ` : ''}{gap.part ? `${gap.part}: ` : ''}{gap.message}</li>)}</ul>
      </div>}
    </div>}
  </details>;
}
