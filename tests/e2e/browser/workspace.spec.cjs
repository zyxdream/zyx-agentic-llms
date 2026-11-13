const {
  test
} = require('../../../frontend/node_modules/@playwright/test');
const {
  Backend,
  source,
  document,
  open,
  choose,
  showPanel,
  selectProject,
  send,
  expect
} = require('./backend.cjs');
let backend;
test.beforeEach(async ({
  context
}) => {
  backend = new Backend();
  await backend.mount(context);
});
test.afterEach(async () => {
  for (const hook of backend.releases || []) hook();
  expect(backend.unknown).toEqual([]);
});
const controls = page => page.getByRole('status');
async function idle(page) {
  await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))));
}
function hold(predicate, callback) {
  const h = backend.hold(predicate, callback);
  (backend.releases ||= []).push(h.release);
  return h;
}
test('empty conversation sends its first assignment and restores the answer on reload', async ({
  page
}) => {
  backend.autoComplete = true;
  await open(page, backend);
  await send(page);
  await expect(page.getByText('Replay answer', {
    exact: true
  })).toBeVisible();
  await page.reload();
  await expect(page.getByText('Replay answer', {
    exact: true
  })).toBeVisible();
  expect(Object.keys(backend.runs)).toHaveLength(1);
});
test('three uploads prepare, clarify across reload, publish and download DOCX', async ({
  page
}) => {
  backend.uploadReady = false;
  await open(page, backend);
  await page.getByLabel('Add files').setInputFiles(['one', 'two', 'three'].map(n => ({
    name: n + '.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from('source ' + n)
  })));
  await expect(page.locator('.source')).toHaveCount(3);
  await page.getByLabel('Assignment', {
    exact: true
  }).fill('Draft a report');
  await expect(page.getByRole('button', {
    name: 'Send',
    exact: true
  })).toBeDisabled();
  backend.sources.forEach(d => d.status = 'ready');
  await expect(page.getByRole('button', {
    name: 'Send',
    exact: true
  })).toBeEnabled();
  await page.getByRole('button', {
    name: 'Send',
    exact: true
  }).click();
  await expect.poll(() => Object.keys(backend.runs).length).toBe(1);
  const r = Object.values(backend.runs)[0];
  r.status = 'awaiting_user_confirmation';
  backend.candidate = {
    ...document('final'),
    markdown: '# Final source B report'
  };
  await expect(page.getByLabel('Your answer')).toBeVisible();
  await page.reload();
  await page.getByLabel('Your answer').fill('Use source B');
  await page.getByRole('button', {
    name: 'Continue',
    exact: true
  }).click();
  await showPanel(page,'document');
  await expect(page.getByRole('heading', {
    name: 'Final source B report'
  })).toBeVisible();
  expect(backend.lastConfirmation.decision.answer).toBe('Use source B');
  expect(backend.lastSubmission.promptAttachments).toHaveLength(3);
  const download = page.waitForEvent('download');
  await page.getByRole('button', {
    name: 'Download document'
  }).click();
  expect((await download).suggestedFilename()).toBe('Document.docx');
});
for (const mode of ['fast', 'deep']) test(`reasoning mode ${mode} is sent to the agent`, async ({
  page
}) => {
  await open(page, backend);
  await page.getByLabel('Reasoning mode').selectOption(mode);
  await send(page);
  await expect.poll(() => backend.lastSubmission?.assistant_mode).toBe(mode);
});
test('staged candidates stay out of the editor until publication', async ({
  page
}) => {
  const r = backend.run();
  r.steps = [{
    id: 'step',
    operation: 'create_draft',
    status: 'ready',
    result: {
      artifact_id: 'candidate'
    }
  }];
  await open(page, backend);
  await controls(page).getByText('Steps and saved results').click();
  await controls(page).getByRole('button', {
    name: 'View saved result'
  }).click();
  await expect(controls(page).getByRole('heading', {
    name: 'Staged candidate'
  })).toBeVisible();
  await showPanel(page,'document');
  await expect(page.getByRole('region', {
    name: 'Document',
    exact: true
  }).getByRole('heading', {
    name: 'Original a'
  })).toBeVisible();
});
test('unnamed Markdown body, tables and safe source links survive reload', async ({
  page
}) => {
  backend.documents.a = {
    ...document('a', ''),
    markdown: 'Body without title\n\n| Item | Count |\n|---|---|\n| A | 2 |\n\n[Source](https://example.org/source)'
  };
  await open(page, backend);
  await page.reload();
  await showPanel(page,'document');
  await expect(page.getByRole('cell', {
    name: '2',
    exact: true
  })).toBeVisible();
  await expect(page.getByRole('link', {
    name: 'Source'
  })).toHaveAttribute('href', 'https://example.org/source');
});
test('HTML and script payloads in results are sanitized', async ({
  page
}) => {
  const r = backend.run('a', 'completed');
  r.response = '<script>window.replayPwned=true</script><img src=x onerror="window.replayPwned=true">Safe response';
  await open(page, backend);
  await expect(page.getByText('Safe response')).toBeVisible();
  expect(await page.evaluate(() => window.replayPwned)).toBeUndefined();
});
for (const outcome of ['cancelled', 'failed', 'completed']) test(`Cancel shows actual server outcome ${outcome}`, async ({
  page
}) => {
  backend.cancelOutcome = outcome;
  backend.run();
  await open(page, backend);
  await page.locator('.workspace-composer').getByRole('button', {
    name: 'Cancel request'
  }).click();
  await expect(controls(page)).toHaveCount(0);
  await expect(page.getByText(outcome === 'completed' ? 'Completed before cancellation' : outcome, {
    exact: true
  })).toBeVisible();
  await page.reload();
  await expect(controls(page)).toHaveCount(0);
});
for (const completed of [false, true]) test(`question Cancel restores ${completed ? 'already completed answer' : 'cancellation'}`, async ({
  page
}) => {
  backend.cancelOutcome = completed ? 'completed' : 'cancelled';
  backend.run('a', 'awaiting_user_confirmation');
  await open(page, backend);
  await page.getByRole('region', {
    name: 'Agent question'
  }).getByRole('button', {
    name: 'Cancel request'
  }).click();
  await expect(page.getByLabel('Your answer')).toHaveCount(0);
  await expect(page.getByText(completed ? 'Completed before cancellation' : 'cancelled', {
    exact: true
  })).toBeVisible();
});
test('failed cancellation keeps the clarification and allows a retry', async ({
  page
}) => {
  backend.run('a', 'awaiting_user_confirmation');
  hold(q => q.path === 'runs/wf_a/cancel', q => backend.json(q, {
    msg: 'Temporary cancellation failure'
  }, 503)).release();
  await open(page, backend);
  const cancel = page.getByRole('region', {
    name: 'Agent question'
  }).getByRole('button', {
    name: 'Cancel request'
  });
  await cancel.click();
  await expect(page.getByRole('alert').filter({
    hasText: 'Temporary cancellation failure'
  })).toBeVisible();
  await expect(page.getByLabel('Your answer')).toBeVisible();
  await cancel.click();
  await expect(page.getByLabel('Your answer')).toHaveCount(0);
});
test('retry after reload resumes the same run without another submission', async ({
  page
}) => {
  backend.run('a', 'failed');
  await open(page, backend);
  await page.reload();
  await page.getByRole('button', {
    name: 'Retry request'
  }).click();
  await expect(controls(page)).toBeVisible();
  expect(backend.count('runs/wf_a/retry', 'POST')).toBe(1);
  expect(backend.requests.filter(q => /^conversations\/.*\/runs$/.test(q.path) && q.method === 'POST')).toHaveLength(0);
});
test('historical failures appear once before later completed answers', async ({
  page
}) => {
  backend.run('a', 'failed', 'wf_old').response = 'Earlier request failed';
  backend.run('a', 'completed', 'wf_new').response = 'Later successful answer';
  await open(page, backend);
  await page.reload();
  await expect(page.getByText('Earlier request failed', {
    exact: true
  })).toHaveCount(1);
  await expect(page.getByText('Later successful answer', {
    exact: true
  })).toHaveCount(1);
});
test('progress survives a connection failure and reload', async ({
  page
}) => {
  backend.run();
  let fails = 1;
  backend.hooks.push(q => q.path === 'runs/wf_a' && fails-- > 0 ? backend.json(q, {
    msg: 'Progress temporarily unavailable'
  }, 503) : undefined);
  await open(page, backend);
  await expect(page.getByRole('alert').filter({
    hasText: 'Progress temporarily unavailable'
  })).toBeVisible();
  await expect.poll(() => backend.count('runs/wf_a')).toBeGreaterThan(1);
  await page.reload();
  await expect(controls(page)).toContainText('Working on wf_a');
  backend.complete(backend.runs.wf_a);
  await expect(page.getByText('Replay answer', {
    exact: true
  })).toBeVisible();
});
test('only one status poller runs and conversation metadata is not repeatedly fetched', async ({
  page
}) => {
  backend.run();
  await page.clock.install();
  await open(page, backend);
  const initial = backend.count('conversations/a');
  for (let i = 0; i < 5; i++) {
    await page.clock.runFor(2100);
    await expect.poll(() => backend.count('runs/wf_a')).toBeGreaterThan(i);
  }
  expect(backend.count('conversations/a') - initial).toBeLessThanOrEqual(1);
  expect(backend.count('runs/wf_a')).toBeGreaterThan(1);
});
test('polling continues beyond 200 checks until the server timeout', async ({
  page
}) => {
  test.setTimeout(90000);
  backend.run();
  await page.clock.install();
  await open(page, backend);
  for (let i = 0; i < 205; i++) {
    await page.clock.runFor(2100);
    await expect.poll(() => backend.count('runs/wf_a')).toBeGreaterThan(i);
  }
  backend.runs.wf_a.status = 'failed';
  backend.runs.wf_a.response = 'Server request deadline reached';
  await page.clock.runFor(2100);
  await expect(page.getByText('Server request deadline reached', {
    exact: true
  })).toBeVisible();
});
test('switching chats restores the selected run and its controls', async ({
  page
}) => {
  backend.run();
  backend.run('b', 'awaiting_user_confirmation');
  await open(page, backend);
  await choose(page, 'Chat B');
  await expect(page.getByLabel('Your answer')).toBeVisible();
  await choose(page, 'Chat A');
  await expect(page.getByLabel('Your answer')).toHaveCount(0);
  await expect(controls(page)).toContainText('wf_a');
});
test('delayed preflight in A cannot submit after switching to B', async ({
  page
}) => {
  await open(page, backend);
  const h = hold(q => q.path === 'document-preparation' && q.url.searchParams.get('conversation_id') === 'a');
  await send(page);
  await h.entered;
  await choose(page, 'Chat B');
  h.release();
  await idle(page);
  expect(backend.requests.filter(q => q.path === 'conversations/a/runs')).toHaveLength(0);
  await page.getByLabel('Assignment', {
    exact: true
  }).fill('For B');
  await expect(page.getByRole('button', {
    name: 'Send',
    exact: true
  })).toBeEnabled();
});
for (const rejected of [false, true]) test(`late ${rejected ? 'rejected' : 'accepted'} submission in A leaves B controls and errors alone`, async ({
  page
}) => {
  backend.run('b');
  await open(page, backend);
  const h = hold(q => q.path === 'conversations/a/runs', rejected ? q => backend.json(q, {
    msg: 'Late A error'
  }, 503) : undefined);
  await send(page);
  await h.entered;
  await choose(page, 'Chat B');
  h.release();
  await idle(page);
  await expect(controls(page)).toContainText('wf_b');
  await expect(page.getByText('Late A error')).toHaveCount(0);
});
for (const operation of ['cancel', 'confirm', 'retry', 'poll']) test(`late ${operation} response from A cannot change B`, async ({
  page
}) => {
  backend.run('a', operation === 'confirm' ? 'awaiting_user_confirmation' : operation === 'retry' ? 'failed' : 'running');
  backend.run('b');
  await open(page, backend);
  const path = 'runs/wf_a' + (operation === 'poll' ? '' : '/' + operation);
  const h = hold(q => q.path === path);
  if (operation === 'confirm') {
    await page.getByLabel('Your answer').fill('A');
    await page.getByRole('button', {
      name: 'Continue',
      exact: true
    }).click();
  } else if (operation === 'retry') await page.getByRole('button', {
    name: 'Retry request'
  }).click();else if (operation === 'cancel') await page.locator('.workspace-composer').getByRole('button', {
    name: 'Cancel request'
  }).click();
  await h.entered;
  await choose(page, 'Chat B');
  h.release();
  await idle(page);
  await expect(controls(page)).toContainText('wf_b');
});
test('late Cancel cannot replace a newer assignment in the same chat', async ({
  page
}) => {
  const r = backend.run();
  await open(page, backend);
  const h = hold(q => q.path === 'runs/wf_a/cancel');
  await page.locator('.workspace-composer').getByRole('button', {
    name: 'Cancel request'
  }).click();
  await h.entered;
  backend.complete(r);
  await page.getByLabel('Assignment', {
    exact: true
  }).fill('A newer assignment');
  await expect(page.getByRole('button', {
    name: 'Send',
    exact: true
  })).toBeEnabled();
  await send(page, 'A newer assignment');
  await expect.poll(() => Object.keys(backend.runs).length).toBe(2);
  const latest = Object.values(backend.runs).at(-1);
  h.release();
  await idle(page);
  await expect(controls(page)).toContainText(latest.id);
});
test('manual save finishing after answer publication preserves the completed answer', async ({
  page
}) => {
  await open(page, backend);
  await showPanel(page,'document');
  await page.getByRole('button', {
    name: 'Edit Markdown'
  }).click();
  await page.getByLabel('Document Markdown').fill('# Manual content');
  const h = hold(q => q.path === 'documents/doc-a' && q.method === 'PATCH');
  await page.getByRole('button', {
    name: 'Save document'
  }).click();
  await h.entered;
  backend.run('a', 'completed').response = 'Concurrently saved answer';
  h.release();
  await page.reload();
  await expect(page.getByText('Concurrently saved answer', {
    exact: true
  })).toBeVisible();
  await expect(page.getByRole('heading', {
    name: 'Manual content'
  })).toBeVisible();
});
test('document conflict preserves unsaved Markdown until explicit reload', async ({
  page
}) => {
  await open(page, backend);
  await showPanel(page,'document');
  await page.getByRole('button', {
    name: 'Edit Markdown'
  }).click();
  await page.getByLabel('Document Markdown').fill('# Keep my unsaved text');
  backend.versions.a = 2;
  await page.getByRole('button', {
    name: 'Save document'
  }).click();
  await expect(page.getByRole('alert').filter({
    hasText: 'changed elsewhere'
  })).toBeVisible();
  await expect(page.getByLabel('Document Markdown')).toHaveValue('# Keep my unsaved text');
  await page.reload();
  await expect(page.getByRole('heading', {
    name: 'Original a'
  })).toBeVisible();
});
test('explicit saves serialize and use the acknowledged document version', async ({
  page
}) => {
  await open(page, backend);
  await showPanel(page,'document');
  await page.getByRole('button', {
    name: 'Edit Markdown'
  }).click();
  await page.getByLabel('Document Markdown').fill('# First');
  const h = hold(q => q.path === 'documents/doc-a' && q.method === 'PATCH');
  await page.getByRole('button', {
    name: 'Save document'
  }).click();
  await h.entered;
  await expect(page.getByRole('button', {
    name: 'Save document'
  })).toBeDisabled();
  await expect(page.getByLabel('Document Markdown',{exact:true})).toBeDisabled();
  h.release();
  await showPanel(page,'document');
  await page.getByRole('button', {
    name: 'Edit Markdown'
  }).click();
  await page.getByLabel('Document Markdown').fill('# Second');
  await page.getByRole('button', {
    name: 'Save document'
  }).click();
  await expect(page.getByRole('heading', {
    name: 'Second'
  })).toBeVisible();
  expect(backend.requests.filter(q => q.method === 'PATCH').map(q => q.body.expected_version)).toEqual([1, 2]);
});
test('save response for another document keeps local edits and reports the mismatch', async ({
  page
}) => {
  await open(page, backend);
  hold(q => q.path === 'documents/doc-a' && q.method === 'PATCH', q => backend.json(q, {
    document_id: 'doc-b',
    conversation_id: 'b',
    document: document('b'),
    document_version: 2
  })).release();
  await showPanel(page,'document');
  await page.getByRole('button', {
    name: 'Edit Markdown'
  }).click();
  await page.getByLabel('Document Markdown').fill('# Preserve this edit');
  await page.getByRole('button', {
    name: 'Save document'
  }).click();
  await expect(page.getByRole('alert')).toContainText(/document|conversation/i);
  await expect(page.getByLabel('Document Markdown')).toHaveValue('# Preserve this edit');
});
test('late document-save response cannot replace another conversation', async ({
  page
}) => {
  await open(page, backend);
  await showPanel(page,'document');
  await page.getByRole('button', {
    name: 'Edit Markdown'
  }).click();
  await page.getByLabel('Document Markdown').fill('# A saved edit');
  const h = hold(q => q.path === 'documents/doc-a' && q.method === 'PATCH');
  await page.getByRole('button', {
    name: 'Save document'
  }).click();
  await h.entered;
  await choose(page, 'Chat B');
  h.release();
  await idle(page);
  await expect(page.getByRole('heading', {
    name: 'Original b'
  })).toBeVisible();
  await expect(page.getByText('A saved edit')).toHaveCount(0);
});
for (const empty of [false, true]) test(`switching to a ${empty ? 'empty' : 'populated'} project preserves the selected project`, async ({
  page
}) => {
  if (empty) delete backend.chats.c;
  await open(page, backend);
  await selectProject(page,'Project Two');
  if (empty) await expect(page.getByText('Create or select a conversation to begin.')).toBeVisible();else await expect(page.getByRole('heading', {
    name: 'Chat C',
    exact: true
  })).toBeVisible();
  await expect(page.getByRole('button',{name:'Open project Project Two',exact:true})).toHaveAttribute('aria-current','true');
});
test('late conversation metadata cannot undo a project switch', async ({
  page
}) => {
  const h = hold(q => q.path === 'conversations/a');
  await page.goto('/');
  await h.entered;
  await selectProject(page,'Project Two');
  await expect(page.getByRole('heading', {
    name: 'Chat C',
    exact: true
  })).toBeVisible();
  h.release();
  await idle(page);
  await expect(page.getByRole('button',{name:'Open project Project Two',exact:true})).toHaveAttribute('aria-current','true');
  await expect(page.getByRole('heading', {
    name: 'Chat C',
    exact: true
  })).toBeVisible();
});
test('two tabs preserve independent project and conversation selections on reload', async ({
  page,
  context
}) => {
  await open(page, backend);
  const second = await context.newPage();
  await open(second, backend);
  await selectProject(second,'Project Two');
  await expect(second.getByRole('heading', {
    name: 'Chat C',
    exact: true
  })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', {
    name: 'Chat A',
    exact: true
  })).toBeVisible();
  await expect(page.getByRole('button',{name:'Open project Project One',exact:true})).toHaveAttribute('aria-current','true');
});
test('13 staggered uploads retain all source cards and readiness after reload', async ({
  page
}) => {
  backend.sources = Array.from({
    length: 13
  }, (_, i) => source('file-' + i, 'a', i < 5 ? 'ready' : 'summarizing'));
  await open(page, backend);
  await expect(page.locator('.source')).toHaveCount(13);
  await page.getByLabel('Assignment', {
    exact: true
  }).fill('Read all files');
  await expect(page.getByRole('button', {
    name: 'Send',
    exact: true
  })).toBeDisabled();
  await page.reload();
  await expect(page.locator('.source')).toHaveCount(13);
  backend.sources.forEach(s => s.status = 'ready');
  await page.getByLabel('Assignment', {
    exact: true
  }).fill('Read all files');
  await expect(page.getByRole('button', {
    name: 'Send',
    exact: true
  })).toBeEnabled();
});
for (const count of [100, 101]) test(`${count} selected files ${count === 100 ? 'are accepted' : 'are rejected before admission'}`, async ({
  page
}) => {
  test.setTimeout(60000);
  await open(page, backend);
  await page.getByLabel('Add files').setInputFiles(Array.from({
    length: count
  }, (_, i) => ({
    name: 'file-' + i + '.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from('data')
  })));
  if (count === 100) {
    await expect(page.locator('.source')).toHaveCount(100);
    await expect.poll(() => backend.sources.filter(s => s.status === 'ready').length).toBe(100);
    expect(backend.requests.filter(q => /^upload-groups\/.*\/files$/.test(q.path)).length).toBeGreaterThan(1);
  } else {
    await expect(page.getByRole('alert').filter({
      hasText: '100 files'
    })).toBeVisible();
    expect(backend.count('upload-groups', 'POST')).toBe(0);
  }
});
test('ready partial-coverage sources permit Send across reload without approval', async ({
  page
}) => {
  backend.sources = [source('partial', 'a', 'ready', {
    coverage: {
      extraction: {
        status: 'unverified',
        gap_fingerprint: 'gap'
      }
    }
  })];
  await open(page, backend);
  await page.reload();
  await page.getByLabel('Assignment', {
    exact: true
  }).fill('Read partial source');
  await expect(page.getByRole('button', {
    name: 'Send',
    exact: true
  })).toBeEnabled();
  await expect(page.getByRole('dialog')).toHaveCount(0);
});
test('excluded failed sources remain scoped and persist through reload', async ({
  page
}) => {
  backend.sources = [source('failed', 'a', 'failed'), source('ready')];
  await open(page, backend);
  await page.getByLabel('Assignment', {
    exact: true
  }).fill('Use ready source');
  await expect(page.getByRole('button', {
    name: 'Send',
    exact: true
  })).toBeDisabled();
  await page.locator('.source').filter({
    hasText: 'failed.txt'
  }).getByRole('checkbox').uncheck();
  await page.reload();
  await page.getByLabel('Assignment', {
    exact: true
  }).fill('Use ready source');
  await page.getByRole('button', {
    name: 'Send',
    exact: true
  }).click();
  await expect.poll(() => backend.lastSubmission?.excludedDocumentIds).toEqual(['failed']);
  expect(backend.lastSubmission.promptAttachments.map(s => s.file_id)).toEqual(['ready']);
});
for (const failed of [false, true]) test(`late attachment ${failed ? 'failure' : 'success'} in A does not change B`, async ({
  page
}) => {
  const h = hold(q => q.path === 'document-preparation' && q.url.searchParams.get('conversation_id') === 'a', failed ? q => backend.json(q, {
    msg: 'Old A preparation failure'
  }, 503) : undefined);
  await open(page, backend);
  await h.entered;
  await choose(page, 'Chat B');
  await page.getByLabel('Assignment', {
    exact: true
  }).fill('B only');
  await expect(page.getByRole('button', {
    name: 'Send',
    exact: true
  })).toBeEnabled();
  h.release();
  await idle(page);
  await expect(page.getByRole('button', {
    name: 'Send',
    exact: true
  })).toBeEnabled();
  await expect(page.getByText('Old A preparation failure')).toHaveCount(0);
});
test('source restoration failure blocks Send and reconnects without losing the assignment', async ({
  page
}) => {
  let remaining = 1;
  backend.hooks.push(q => q.path === 'document-preparation' && q.url.searchParams.get('conversation_id') === 'a' && remaining-- > 0 ? backend.json(q, {
    msg: 'Temporary source failure'
  }, 503) : undefined);
  await open(page, backend);
  await page.getByLabel('Assignment', {
    exact: true
  }).fill('Keep this assignment');
  await expect(page.getByRole('button', {
    name: 'Send',
    exact: true
  })).toBeDisabled();
  await expect(page.getByRole('alert').filter({
    hasText: 'Reconnecting'
  })).toBeVisible();
  await expect(page.getByRole('button', {
    name: 'Send',
    exact: true
  })).toBeEnabled({
    timeout: 20000
  });
  await expect(page.getByLabel('Assignment', {
    exact: true
  })).toHaveValue('Keep this assignment');
});
test('newly created conversation admits its first empty-source assignment', async ({
  page
}) => {
  backend.autoComplete = true;
  await open(page, backend);
  await page.getByRole('button',{name:'New conversation in Project One',exact:true}).click();
  await expect(page.getByRole('heading', {
    name: 'New conversation',
    exact: true
  })).toBeVisible();
  await send(page, 'First new-chat assignment');
  await expect(page.getByText('Replay answer', {
    exact: true
  })).toBeVisible();
  expect(backend.lastSubmission.promptAttachments).toEqual([]);
});
test('late accepted submission in A preserves B source exclusions', async ({
  page
}) => {
  backend.sources = [source('bad-b', 'b', 'failed')];
  await open(page, backend);
  const h = hold(q => q.path === 'conversations/a/runs');
  await send(page);
  await h.entered;
  await choose(page, 'Chat B');
  await page.locator('.source').getByRole('checkbox').uncheck();
  h.release();
  await idle(page);
  expect(await page.evaluate(() => JSON.parse(sessionStorage.getItem('document-exclusions:1/b')))).toEqual(['bad-b']);
  await page.getByLabel('Assignment', {
    exact: true
  }).fill('B request');
  await expect(page.getByRole('button', {
    name: 'Send',
    exact: true
  })).toBeEnabled();
});
for (const interrupted of [false, true]) test(`cross-tab source upload is discovered after ${interrupted ? 'restoration error' : 'reload'}`, async ({
  page,
  context
}) => {
  await open(page, backend);
  const second = await context.newPage();
  await open(second, backend);
  await second.getByLabel('Add files').setInputFiles({
    name: 'second-tab.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from('data')
  });
  await expect(second.locator('.source')).toHaveCount(1);
  let failures = interrupted ? 1 : 0;
  backend.hooks.push(q => q.path === 'document-preparation' && q.url.searchParams.get('conversation_id') === 'a' && failures-- > 0 ? backend.json(q, {
    msg: 'Temporary'
  }, 503) : undefined);
  await page.reload();
  await expect(page.locator('.source')).toHaveCount(1, {
    timeout: 20000
  });
});
test('project sources appear in both conversations without including other projects', async ({
  page
}) => {
  backend.sources = [source('project-one', null, 'ready', {
    scope: 'matter',
    project: 1
  }), source('project-two', null, 'ready', {
    scope: 'matter',
    project: 2
  })];
  await open(page, backend);
  await expect(page.locator('.source')).toContainText('project-one.txt');
  await choose(page, 'Chat B');
  await expect(page.locator('.source')).toContainText('project-one.txt');
  await expect(page.getByText('project-two.txt')).toHaveCount(0);
});
test('original source inspection and saved summaries use source identity', async ({
  page
}) => {
  backend.sources = [source('readable')];
  await open(page, backend);
  await page.getByRole('button', {
    name: 'Read source',
    exact: true
  }).click();
  await expect(page.getByRole('region', {
    name: 'Source text'
  })).toContainText('Original source text');
  await page.locator('.source summary').click();
  await expect(page.getByText('Detailed source evidence.')).toBeVisible();
  expect(backend.count('sources/readable/summary')).toBe(1);
});
test('saved Python results download the authorized generated file', async ({
  page
}) => {
  const r = backend.run();
  r.steps = [{
    id: 'python',
    operation: 'python_run',
    status: 'ready',
    result: {
      artifact_id: 'python-result'
    }
  }];
  backend.artifact = {
    id: 'python-result',
    kind: 'python_execution',
    content: {
      status: 'completed',
      stdout: '2 rows',
      files: [{
        id: 'file',
        filename: 'results.csv'
      }]
    }
  };
  await open(page, backend);
  await controls(page).getByText('Steps and saved results').click();
  await controls(page).getByRole('button', {
    name: 'View saved result'
  }).click();
  const event = page.waitForEvent('download');
  await page.getByRole('button', {
    name: 'Download results.csv'
  }).click();
  expect((await event).suggestedFilename()).toBe('results.csv');
});
test('accepted run can be cancelled while its transcript refresh is delayed', async ({
  page
}) => {
  await open(page, backend);
  const h = hold(q => q.path === 'conversations/a' && Object.keys(backend.runs).length > 0);
  await send(page);
  await h.entered;
  await expect(page.locator('.workspace-composer').getByRole('button', {
    name: 'Cancel request'
  })).toBeEnabled();
  const cancel = page.locator('.workspace-composer').getByRole('button', {
    name: 'Cancel request'
  }).click();
  await expect.poll(() => backend.count(Object.keys(backend.runs).map(id => 'runs/' + id + '/cancel')[0], 'POST')).toBe(1);
  h.release();
  await cancel;
  await expect(controls(page)).toHaveCount(0);
});
test('earlier chat files remain context without being marked newly attached', async ({
  page
}) => {
  backend.sources = [source('earlier', 'a', 'ready', {
    committed: true
  }), source('new-file')];
  await open(page, backend);
  await send(page);
  await expect.poll(() => backend.lastSubmission?.chatFiles.map(f => f.name)).toEqual(['earlier', 'new-file']);
  expect(backend.lastSubmission.promptAttachments.map(f => f.file_id)).toEqual(['new-file']);
});
test('excluding a failed source applies to one prompt and it returns when ready', async ({
  page
}) => {
  backend.autoComplete = true;
  backend.sources = [source('ready'), source('later', 'a', 'failed')];
  await open(page, backend);
  await page.locator('.source').filter({
    hasText: 'later.txt'
  }).getByRole('checkbox').uncheck();
  await send(page, 'First assignment');
  await expect(page.getByText('Replay answer', {
    exact: true
  })).toBeVisible();
  expect(backend.lastSubmission.promptAttachments.map(f => f.file_id)).toEqual(['ready']);
  backend.sources.find(s => s.document_id === 'later').status = 'ready';
  await page.reload();
  await expect(page.locator('.source').filter({
    hasText: 'later.txt'
  }).getByRole('checkbox')).toBeChecked();
  await send(page, 'Next assignment');
  await expect.poll(() => Object.keys(backend.runs).length).toBe(2);
  const latest = backend.requests.filter(r => r.method === 'POST' && r.path === 'conversations/a/runs').at(-1).body;
  expect(latest.promptAttachments.map(f => f.file_id)).toEqual(['later']);
  expect(latest.chatFiles.map(f => f.name)).toEqual(['ready', 'later']);
});
