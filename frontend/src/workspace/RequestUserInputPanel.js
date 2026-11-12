import React, { useState } from 'react';
export default function RequestUserInputPanel({
  request,
  onSubmit,
  onCancel,
  loading
}) {
  const [answer, setAnswer] = useState('');
  return <section aria-label="Agent question"><p>{request.message}</p>
 {(request.options || []).map(o => <button key={o.id} disabled={loading} onClick={() => setAnswer(o.label)}>{o.label}</button>)}
 <label>Your answer<textarea value={answer} onChange={e => setAnswer(e.target.value)} disabled={loading} /></label>
 <button disabled={loading || !answer.trim()} onClick={() => onSubmit({
      answer: answer.trim()
    })}>Continue</button>
 <button disabled={loading} onClick={onCancel}>Cancel request</button></section>;
}
