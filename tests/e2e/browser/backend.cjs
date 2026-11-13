const {
  expect
} = require('../../../frontend/node_modules/@playwright/test');
const clone = v => JSON.parse(JSON.stringify(v));
const terminal = r => ['completed', 'cancelled', 'failed'].includes(r.status);
const policy = {
  max_files: 5,
  max_bytes: 104857600,
  concurrency: 2,
  chat_files: 100,
  matter_files: 30,
  total_bytes: 1073741824,
  chat_file_bytes: 52428800,
  matter_file_bytes: 104857600
};
function deferred() {
  let release, started;
  const gate = new Promise(r => release = r),
    entered = new Promise(r => started = r);
  return {
    gate,
    entered,
    release,
    started
  };
}
function document(id, name = 'Notes') {
  return {
    public_id: 'doc-' + id,
    title: name,
    format_version: 2,
    source_format: 'markdown',
    markdown: '# Original ' + id,
    revision_id: null
  };
}
function source(id, chat = 'a', status = 'ready', extra = {}) {
  return {
    document_id: id,
    clientUploadId: id,
    group_id: 'group-' + id,
    filename: id + '.txt',
    file_size: 4,
    scope: 'chat',
    chat,
    status,
    stage: status,
    generation: 1,
    artifact_id: 'prepared-' + id,
    metadata: {
      headline: 'Prepared ' + id
    },
    ...extra
  };
}
class Backend {
  constructor(options = {}) {
    Object.assign(this, {
      projects: [{
        id: 1,
        name: 'Project One'
      }, {
        id: 2,
        name: 'Project Two'
      }],
      chats: {
        a: {
          id: 'a',
          project_id: 1,
          name: 'Chat A'
        },
        b: {
          id: 'b',
          project_id: 1,
          name: 'Chat B'
        },
        c: {
          id: 'c',
          project_id: 2,
          name: 'Chat C'
        }
      },
      documents: {
        a: document('a'),
        b: document('b'),
        c: document('c')
      },
      versions: {
        a: 1,
        b: 1,
        c: 1
      },
      runs: {},
      sources: [],
      groups: {},
      requests: [],
      hooks: [],
      unknown: [],
      uploadReady: true,
      autoComplete: false,
      confirmComplete: true,
      answer: 'Replay answer',
      cancelOutcome: 'cancelled'
    }, options);
  }
  run(chat = 'a', status = 'running', id = 'wf_' + chat) {
    this.runs[id] = {
      id,
      chat,
      status,
      query: 'Assignment ' + id,
      response: status === 'completed' ? this.answer : '',
      steps: [],
      artifacts: [],
      created_at: new Date(1700000000000 + Object.keys(this.runs).length * 1000).toISOString()
    };
    return this.runs[id];
  }
  progress(r) {
    return {
      run_id: r.id,
      conversation_id: r.chat,
      document_id: 'doc-' + r.chat,
      status: r.status,
      ready: r.status !== 'running',
      successful: r.status === 'completed',
      info: {
        status: r.status === 'running' ? 'Working on ' + r.id : r.status === 'awaiting_user_confirmation' ? 'Waiting for your answer' : r.status,
        agent: {
          steps: r.steps,
          current_operation: 'choose_next_action',
          artifacts: r.artifacts
        }
      },
      value: r.status === 'awaiting_user_confirmation' ? {
        confirmation: {
          kind: 'agent_question',
          message: 'Which source should I use?',
          options: [{
            id: 'a',
            label: 'Source A'
          }]
        }
      } : terminal(r) ? {
        response: r.response || r.status
      } : null
    };
  }
  chat(id) {
    const c = this.chats[id],
      runs = Object.values(this.runs).filter(r => r.chat === id);
    const active = runs.find(r => !terminal(r));
    return {
      ...c,
      document_id: 'doc-' + id,
      document: clone(this.documents[id]),
      document_version: this.versions[id],
      history: runs.map(r => ({
        id: r.id,
        run_id: r.id,
        query: r.query,
        response: r.response,
        status: r.status,
        created_at: r.created_at,
        metadata: {prompt_attachments:r.attachments || []}
      })),
      active_run: active ? this.progress(active) : null
    };
  }
  catalog(chat, project) {
    const selected = this.sources.filter(d => d.status !== 'cancelled' && (d.scope === 'matter' ? String(d.project || 1) === String(project || this.chats[chat]?.project_id) : d.chat === chat));
    return {
      enabled: true,
      documents: clone(selected),
      policy,
      revision: JSON.stringify(selected.map(d => [d.document_id, d.status])),
      owner_id: 1
    };
  }
  complete(r, response = this.answer) {
    r.status = 'completed';
    r.response = response;
    if (this.candidate) this.documents[r.chat] = clone(this.candidate);
    return this.progress(r);
  }
  hold(predicate, callback) {
    const wait = deferred();
    let once = true;
    this.hooks.push(async q => {
      if (once && predicate(q)) {
        once = false;
        wait.started();
        await wait.gate;
        return callback?.(q);
      }
    });
    return wait;
  }
  async mount(context) {
    const dialogs = page => page.on('dialog', dialog => dialog.accept());
    context.pages().forEach(dialogs);
    context.on('page', dialogs);
    await context.route('**/*', async route => {
      const req = route.request(),
        url = new URL(req.url());
      if (url.origin !== 'http://127.0.0.1:39240') return route.abort('blockedbyclient');
      if (!url.pathname.startsWith('/api/')) return route.continue();
      const path = url.pathname.replace('/api/workspace/', ''),
        method = req.method();
      let body;
      try {
        body = req.postDataJSON();
      } catch {}
      const q = {
        route,
        request: req,
        url,
        path,
        method,
        body
      };
      this.requests.push({
        path,
        method,
        body,
        time: Date.now()
      });
      for (const hook of [...this.hooks].reverse()) {
        const result = await hook(q);
        if (result === true) return;
      }
      return this.respond(q);
    });
  }
  async json(q, value, status = 200) {
    try {
      await q.route.fulfill({
        status,
        contentType: 'application/json',
        body: JSON.stringify(value)
      });
    } catch (e) {
      if (!/closed|disposed|canceled|cancelled/i.test(e.message)) throw e;
    }
    return true;
  }
  async respond(q) {
    const {
      path,
      method,
      body,
      url
    } = q;
    let m;
    if (path === 'projects' && method === 'GET') return this.json(q, {
      projects: this.projects
    });
    if (path === 'projects' && method === 'POST') {
      if (this.projects.some(p => p.name === body.name)) return this.json(q,{code:'project_name_conflict',msg:'You already have a project with this name.'},409);
      const p = {
        id: Math.max(0,...this.projects.map(p => p.id)) + 1,
        name: body.name
      };
      this.projects.push(p);
      return this.json(q, p, 201);
    }
    if (m = path.match(/^projects\/(\d+)$/)) {
      const project=this.projects.find(p => p.id===+m[1]);
      if (!project) return this.json(q,{msg:'Project not found'},404);
      if (method==='PATCH') {
        if (this.projects.some(p => p!==project && p.name===body.name)) return this.json(q,{code:'project_name_conflict',msg:'You already have a project with this name.'},409);
        project.name=body.name;
      }
      if (method==='DELETE') {
        const ids=Object.values(this.chats).filter(c=>c.project_id===project.id).map(c=>c.id);
        const active_runs=Object.values(this.runs).filter(r=>ids.includes(r.chat)&&!terminal(r)).length;
        const active_preparations=this.sources.filter(s=>(s.scope==='matter'?s.project===project.id:ids.includes(s.chat))&&!['ready','failed','cancelled'].includes(s.status)).length;
        if (active_runs||active_preparations) return this.json(q,{code:'project_busy',msg:'Stop active work first.',active_runs,active_preparations},409);
        this.projects=this.projects.filter(p=>p!==project);
        ids.forEach(id=>{delete this.chats[id];delete this.documents[id];delete this.versions[id];});
        return this.json(q,{deleted:true});
      }
      return this.json(q,project);
    }
    if (m = path.match(/^projects\/(\d+)\/conversations$/)) {
      if (method === 'GET') return this.json(q, {
        conversations: Object.values(this.chats).filter(c => c.project_id === +m[1])
      });
      const c = {
        id: this.chats['new-chat'] ? `new-chat-${Object.keys(this.chats).length}` : 'new-chat',
        project_id: +m[1],
        name: body.name
      };
      this.chats[c.id] = c;
      this.documents[c.id] = document(c.id);
      this.versions[c.id] = 1;
      return this.json(q, c, 201);
    }
    if (m = path.match(/^conversations\/([^/]+)$/)) {
      if (!this.chats[m[1]]) return this.json(q,{msg:'Conversation not found'},404);
      if (method==='PATCH') this.chats[m[1]].name=body.name;
      return this.json(q, this.chat(m[1]));
    }
    if (path === 'document-preparation') return this.json(q, this.catalog(url.searchParams.get('conversation_id'), url.searchParams.get('projectId')));
    if ((m = path.match(/^conversations\/([^/]+)\/runs$/)) && method === 'POST') {
      let r = this.runs[body.run_id];
      if (!r) {
        r = this.run(m[1], this.autoComplete ? 'completed' : 'running', body.run_id);
        r.query = body.query;
        r.attachments = body.promptAttachments || [];
        r.response = this.autoComplete ? this.answer : '';
        this.lastSubmission = body;
        for (const d of this.sources) {
          if ((body.promptAttachments || []).some(f => f.file_id === d.document_id)) d.committed = true;
        }
      }
      return this.json(q, this.progress(r), 202);
    }
    if (m = path.match(/^runs\/([^/]+)(?:\/(cancel|confirm|retry))?$/)) {
      const r = this.runs[m[1]];
      if (!r) return this.json(q, {
        msg: 'Run not found'
      }, 404);
      if (m[2] === 'cancel') {
        r.status = this.cancelOutcome;
        if (r.status === 'completed') this.complete(r, 'Completed before cancellation');
      }
      if (m[2] === 'confirm') {
        this.lastConfirmation = body;
        r.status = this.confirmComplete ? 'completed' : 'running';
        if (this.confirmComplete) this.complete(r);
      }
      if (m[2] === 'retry') r.status = 'running';
      return this.json(q, m[2] ? {
        run_id: r.id,
        status: r.status,
        cancelled: r.status === 'cancelled'
      } : this.progress(r));
    }
    if (m = path.match(/^runs\/([^/]+)\/artifacts\/([^/]+)(?:\/files\/([^/]+))?$/)) {
      if (m[3]) return q.route.fulfill({
        contentType: 'text/csv',
        body: 'item,value\nA,2\n',
        headers: {
          'content-disposition': 'attachment; filename="results.csv"'
        }
      });
      return this.json(q, this.artifact || {
        id: m[2],
        kind: 'draft',
        content: {
          draft: {
            ...document('candidate'),
            markdown: '# Staged candidate'
          }
        }
      });
    }
    if ((m = path.match(/^documents\/doc-([^/]+)$/)) && method === 'PATCH') {
      const id = m[1];
      if (body.expected_version !== this.versions[id]) return this.json(q, {
        code: 'document_conflict',
        msg: 'The document changed elsewhere. Your edits have been kept.',
        document_version: this.versions[id]
      }, 409);
      this.documents[id] = clone(body.draft);
      this.versions[id]++;
      return this.json(q, {
        document_id: 'doc-' + id,
        conversation_id: id,
        document: this.documents[id],
        document_version: this.versions[id]
      });
    }
    if (/documents\/.*\/download$/.test(path)) return q.route.fulfill({
      contentType: 'application/octet-stream',
      body: 'mock exported document',
      headers: {
        'content-disposition': 'attachment; filename="Document.docx"'
      }
    });
    if (path === 'upload-groups' && method === 'POST') {
      const existing = Object.values(this.groups).find(g => g.key === body.selectionId);
      if (existing) return this.json(q, existing, 201);
      const id = 'group-' + Object.keys(this.groups).length;
      const docs = body.documents.map(e => source(e.client_upload_id, body.scope === 'chat' ? body.scopeId : null, 'queued', {
        filename: e.filename,
        file_size: e.file_size,
        clientUploadId: e.client_upload_id,
        group_id: id,
        scope: body.scope,
        relative_path: e.relative_path || null,
        folder_id: body.folderName ? id : null,
        folder_name: body.folderName || null,
        project: body.scope === 'matter' ? +body.scopeId : this.chats[body.scopeId].project_id,
        needs_reupload: true
      }));
      this.sources.push(...docs);
      const group = {
        group_id: id,
        documents: docs,
        policy,
        key: body.selectionId
      };
      this.groups[id] = group;
      return this.json(q, group, 201);
    }
    if (m = path.match(/^upload-groups\/([^/]+)\/files$/)) {
      const group = this.groups[m[1]];
      for (const d of group.documents) {
        if (d.status === 'cancelled') continue;
        d.status = this.uploadReady ? 'ready' : 'summarizing';
        d.stage = d.status;
        d.needs_reupload = false;
      }
      return this.json(q, {
        accepted: group.documents,
        failed: []
      });
    }
    if (m = path.match(/^sources\/([^/]+)(?:\/(retry|summary|content))?$/)) {
      const d = this.sources.find(d => d.document_id === m[1]);
      if (!d) return this.json(q, {
        msg: 'Source missing'
      }, 404);
      if (method === 'DELETE') {
        d.status = 'cancelled';
        return this.json(q, {
          deleted: true
        });
      }
      if (m[2] === 'retry') {
        d.status = 'ready';
        d.generation++;
        d.needs_reupload = false;
        return this.json(q, d, 202);
      }
      if (m[2] === 'summary') return this.json(q, {
        detailed_digest: 'Detailed source evidence.',
        section_outline: []
      });
      if (m[2] === 'content') return this.json(q, {
        id: d.document_id,
        filename: d.filename,
        text: 'Original source text',
        start: 0,
        end: 20,
        total_characters: 20,
        status: d.status
      });
    }
    this.unknown.push({
      path,
      method
    });
    return this.json(q, {
      msg: 'Unexpected replay endpoint ' + method + ' ' + path
    }, 404);
  }
  count(path, method) {
    return this.requests.filter(r => r.path === path && (!method || r.method === method)).length;
  }
}
async function open(page, backend, chat = 'a', {panel = 'attachments'} = {}) {
  await page.addInitScript(({
    chat, panel
  }) => {
    if (!sessionStorage.getItem('replay-seeded')) {
      sessionStorage.setItem('workspace.project', chat === 'c' ? '2' : '1');
      sessionStorage.setItem('workspace.conversation', chat);
      localStorage.setItem('workspace.project', chat === 'c' ? '2' : '1');
      localStorage.setItem('workspace.conversation', chat);
      if (panel) sessionStorage.setItem(`workspace.panel:${chat}`, JSON.stringify({open:true,tab:panel}));
      sessionStorage.setItem('replay-seeded', '1');
    }
  }, {
    chat, panel
  });
  await page.goto('/');
  await expect(page.getByRole('heading', {
    name: backend.chats[chat].name,
    exact: true
  })).toBeVisible();
  if (panel) await expect(page.locator('.workspace-inspector:not([hidden]) .inspector-heading h2')).toHaveText(panel === 'output' ? 'Output' : panel === 'project-documents' ? 'Project documents' : 'Attachments');
}
async function showPanel(page,tab) {
  const name = tab === 'output' ? 'Output' : tab === 'project-documents' ? 'Project documents' : 'Attachments';
  await page.getByRole('button',{name,exact:true}).first().click();
  await expect(page.locator('.workspace-inspector:not([hidden]) .inspector-heading h2')).toHaveText(name);
}
async function selectProject(page,name) {
  await page.getByRole('button',{name:`Open project ${name}`,exact:true}).click();
  if (await page.getByRole('button',{name:'Discard changes',exact:true}).isVisible()) await page.getByRole('button',{name:'Discard changes',exact:true}).click();
  await expect(page.getByRole('button',{name:`Open project ${name}`,exact:true})).toHaveAttribute('aria-current','true');
}
async function choose(page, name) {
  const heading=page.locator('.workspace-inspector:not([hidden]) .inspector-heading h2');
  const title=await heading.count() ? await heading.textContent() : null;
  const panel=title === 'Output' ? 'output' : title === 'Project documents' ? 'project-documents' : title === 'Attachments' ? 'attachments' : null;
  if (panel) await page.evaluate(panel=>{for(const chat of ['a','b','c']) if (!sessionStorage.getItem(`workspace.panel:${chat}`)) sessionStorage.setItem(`workspace.panel:${chat}`,JSON.stringify({open:true,tab:panel}));},panel);
  await page.getByRole('button', {
    name,
    exact: true
  }).click();
  if (await page.getByRole('button',{name:'Discard changes',exact:true}).isVisible()) await page.getByRole('button',{name:'Discard changes',exact:true}).click();
  await expect(page.getByRole('heading', {
    name,
    exact: true
  })).toBeVisible();
  if (panel) await expect(page.locator('.workspace-inspector:not([hidden]) .inspector-heading h2')).toHaveText(title);
}
async function send(page, text = 'Please summarize the source.') {
  await page.getByLabel('Message', {
    exact: true
  }).fill(text);
  await page.getByRole('button', {
    name: 'Send',
    exact: true
  }).click();
}
module.exports = {
  Backend,
  source,
  document,
  policy,
  deferred,
  open,
  choose,
  showPanel,
  selectProject,
  send,
  expect
};
