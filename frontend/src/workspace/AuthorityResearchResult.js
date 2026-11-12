import React from 'react';
import './AuthorityResearchResult.css';

const list = value => Array.isArray(value) ? value : [];
const label = value => String(value || '').replaceAll('_', ' ');

function describe(value) {
  if (value == null || value === '') return '';
  if (Array.isArray(value)) return value.map(describe).filter(Boolean).join('; ');
  if (typeof value === 'object') return Object.entries(value)
    .map(([key, item]) => `${label(key)}: ${describe(item)}`).join('; ');
  return String(value);
}

export function authorityLink(value) {
  if (typeof value !== 'string' || !value.trim()) return null;
  const href = value.trim();

  if (!/^https?:\/\//i.test(href)) return null;
  try {
    const parsed = new URL(href);
    return parsed.username || parsed.password ? null : parsed.href;
  } catch (_) {
    return null;
  }
}

function SourceLink({ href, children }) {
  const safe = authorityLink(href);
  return safe ? <a href={safe} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span>;
}

function Fields({ value, exclude = [] }) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return value ? <p>{describe(value)}</p> : null;
  const entries = Object.entries(value).filter(([key, item]) => !exclude.includes(key)
    && item != null && item !== '' && (!Array.isArray(item) || item.length));
  return !!entries.length && <dl className="authority-research__fields">{entries.map(([key, item]) => <React.Fragment key={key}>
    <dt>{label(key)}</dt><dd>{describe(item)}</dd>
  </React.Fragment>)}</dl>;
}

function ArtifactButton({ id, onRead, children }) {
  return typeof id === 'string' && id && onRead
    ? <button type="button" onClick={() => onRead(id)}>{children}</button> : null;
}

function occurrenceSummary(occurrences) {
  if (occurrences.every(item => item.discovery_mode === 'overview')) {
    return `${occurrences.length} recorded examples and attributed propositions`;
  }
  if (occurrences.every(item => item.discovery_mode === 'audit')) {
    return `${occurrences.length} individual occurrences and attributed propositions`;
  }
  return `${occurrences.length} saved uses and attributed propositions`;
}

function Coverage({ value, mode }) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return <Fields value={value} />;
  const completed = list(value.completed_chunks);
  const failed = list(value.failed_chunks);
  return <>
    {value.complete === true && <p>{mode
      ? 'The selected discovery scope was processed. Completion describes processing coverage, not a guarantee that every reference was recognized. Source reading and substantive assessment are recorded separately.'
      : 'The selected target text was processed. Source reading and substantive assessment are recorded separately.'}</p>}
    {value.complete === false && <p>Research coverage is partial; the unprocessed or unavailable material remains unchecked.</p>}
    {!!completed.length && <p>{completed.length} text {completed.length === 1 ? 'section' : 'sections'} processed.</p>}
    {!!failed.length && <ul>{failed.map((failure, index) => <li key={index}>
      <Fields value={failure} />
    </li>)}</ul>}
    <Fields value={value} exclude={['complete', 'completed_chunks', 'failed_chunks', 'targets', 'discovery_mode', 'scope', 'plan_id', 'chunk_plan_version']} />
    {!!list(value.scope).length && <details open>
      <summary>Selected discovery scope</summary>
      <ul>{value.scope.map((target, index) => <li key={target.target_id || index}><Fields value={target} /></li>)}</ul>
    </details>}
    {!!list(value.targets).length && <details>
      <summary>Target coverage details</summary>
      <ul>{value.targets.map((target, index) => <li key={target.id || index}><Fields value={target} /></li>)}</ul>
    </details>}
  </>;
}

function Candidate({ candidate }) {
  const name = candidate.case_name || candidate.title || candidate.citation || 'Candidate source';
  return <li>
    <SourceLink href={candidate.viewer_url || candidate.url}>{name}</SourceLink>
    <Fields value={candidate} exclude={['id', 'source_id', 'case_name', 'title', 'url', 'viewer_url', 'text', 'snippet']} />
    {candidate.snippet && <p>Search preview: {candidate.snippet}</p>}
    {candidate.viewer_url && candidate.url && candidate.viewer_url !== candidate.url
      && <p><SourceLink href={candidate.url}>Source website</SourceLink></p>}
  </li>;
}

export function AuthoritySourceResult({ content, onRead }) {
  const source = content || {};
  const passage = source.role === 'passage';
  return <section className="authority-research" aria-label={passage ? 'Saved authority passage' : 'Saved authority source'}>
    <h4>{source.title || (passage ? 'Source passage' : 'Retrieved source')}</h4>
    <p>{passage ? 'Source passage supplied for reading.' : 'Saved source text; retrieval alone does not establish legal support.'}</p>
    {source.url && <p><SourceLink href={source.url}>{source.url}</SourceLink></p>}
    <Fields value={{ opinion_id: source.opinion_id, opinion_type: source.opinion_type,
      locations: source.locations, start: source.start, end: source.end,
      total_characters: source.total_characters }} />
    <pre className="authority-research__text">{source.text || 'No readable source text was retrieved.'}</pre>
    {source.next_start != null && <p>Additional source text is available after character {source.next_start}.</p>}
    <ArtifactButton id={source.source_artifact_id} onRead={onRead}>Open saved source</ArtifactButton>
    {source.error && <p>{source.error}</p>}
  </section>;
}

export default function AuthorityResearchResult({ content, onRead }) {
  const research = content || {};
  const authorities = list(research.authorities);
  const mode = ['overview', 'audit'].includes(research.discovery_mode) ? research.discovery_mode : null;
  return <section className="authority-research" aria-label="Saved authority research">
    <h4>Authority research</h4>
    {research.purpose && <p>{research.purpose}</p>}
    {mode === 'overview' && <p>Latest discovery: overview. Recorded examples may consolidate repeated uses; they are not an exhaustive occurrence count.</p>}
    {mode === 'audit' && <p>Latest discovery: audit. Every reference occurrence was requested within the selected scope.</p>}
    <p>{authorities.length} {authorities.length === 1 ? 'authority' : 'authorities'} identified. Lookup results and source reading are recorded separately from generated assessments.</p>
    <section>
      <h5>Coverage and work performed</h5>
      <Coverage value={research.coverage} mode={mode} />
      {!!list(research.targets).length && <details>
        <summary>Research targets</summary>
        <ul>{research.targets.map((target, index) => <li key={target.id || index}>
          <Fields value={target} exclude={['text']} />
        </li>)}</ul>
      </details>}
    </section>
    {authorities.map((authority, index) => <section className="authority-research__authority" key={authority.id || index}>
      <h5>{authority.citation_as_written || authority.lookup_citation || authority.citation || authority.case_name || authority.name || `Authority ${index + 1}`}</h5>
      <Fields value={{ kind: authority.kind, case_name: authority.case_name, 'Lookup citation': authority.lookup_citation }} />
      {!!list(authority.occurrences).length && <details open>
        <summary>{occurrenceSummary(authority.occurrences)}</summary>
        <ol>{authority.occurrences.map((occurrence, occurrenceIndex) => <li key={occurrence.id || occurrenceIndex}>
          <p>{occurrence.discovery_mode === 'overview' ? 'Recorded example (overview discovery).'
            : occurrence.discovery_mode === 'audit' ? 'Individual occurrence (audit discovery).'
              : 'Saved use (discovery mode not recorded).'}</p>
          {occurrence.span_verified !== true && <p>Exact source anchoring was not recorded for this saved use.</p>}
          {occurrence.citation_as_written && <p>As written: {occurrence.citation_as_written}</p>}
          {occurrence.context && <blockquote>{occurrence.context}</blockquote>}
          {occurrence.proposition && <p><strong>Proposition attributed in the target: </strong>{occurrence.proposition}</p>}
          <Fields value={occurrence.source_location || occurrence.reference} />
          {occurrence.location_uncertain && <p>Exact occurrence location is uncertain; the broader source location is retained.</p>}
        </li>)}</ol>
      </details>}
      <h6>Lookup results</h6>
      {!!list(authority.candidates).length
        ? <ul>{authority.candidates.map((candidate, candidateIndex) => <Candidate key={candidate.id || `${candidate.source_id || ''}-${candidateIndex}`} candidate={candidate} />)}</ul>
        : <p>No candidate source is recorded for this authority.</p>}
      {!!list(authority.provider_outcomes).length && <details>
        <summary>Provider lookup outcomes</summary>
        <ul>{authority.provider_outcomes.map((outcome, outcomeIndex) => <li key={outcomeIndex}><Fields value={outcome} /></li>)}</ul>
      </details>}
      <Fields value={authority.checking && {
        identity: label(authority.checking.identity),
        source_read: authority.checking.source_read == null ? null : (authority.checking.source_read ? 'read' : 'not read'),
        'CourtListener opinion read': authority.checking.opinion_read == null ? null : (authority.checking.opinion_read ? 'read' : 'not read'),
        proposition: label(authority.checking.proposition),
      }} />
      {authority.findings && <p>{describe(authority.findings)}</p>}
      {authority.uncertainties && <p>{describe(authority.uncertainties)}</p>}
    </section>)}
    {!!list(research.sources).length && <section>
      <h5>Retrieved source records</h5>
      <ul>{research.sources.map((source, index) => <li key={source.id || index}>
        <SourceLink href={source.viewer_url || source.url}>{source.title || source.case_name || source.url || `Source ${index + 1}`}</SourceLink>
        <Fields value={{ provider: source.provider, opinion_id: source.opinion_id,
          opinion_type: source.opinion_type, opinions: source.opinions, error: source.error,
          read_errors: source.read_errors }} />
        {!!list(source.observations).length && <details>
          <summary>Additional provider observations</summary>
          <ul>{source.observations.map((observation, observationIndex) => <li key={observationIndex}>
            <Fields value={observation} exclude={['url', 'viewer_url']} />
            {observation.url && <SourceLink href={observation.url}>Source website</SourceLink>}
          </li>)}</ul>
        </details>}
        {list(source.passage_artifact_ids).map((id, passageIndex) => <ArtifactButton key={id} id={id} onRead={onRead}>
          Read source passage {passageIndex + 1} — {source.title || source.case_name || `Source ${index + 1}`}
        </ArtifactButton>)}
        {!list(source.passage_artifact_ids).length && <p>No source passages are recorded as read.</p>}
      </li>)}</ul>
    </section>}
    {!!list(research.queries).length && <details>
      <summary>Searches performed</summary>
      <ul>{research.queries.map((query, index) => <li key={index}><Fields value={query} /></li>)}</ul>
    </details>}
    <section>
      <h5>Generated assessments</h5>
      {list(research.assessments).length ? <ul>{research.assessments.map((assessment, index) => <li key={assessment.artifact_id || index}>
        {assessment.text && <p className="authority-research__assessment">{assessment.text}</p>}
        <ArtifactButton id={assessment.artifact_id} onRead={onRead}>Read generated assessment {index + 1}</ArtifactButton>
      </li>)}</ul> : <p>No generated assessment is saved in this research record.</p>}
    </section>
  </section>;
}
