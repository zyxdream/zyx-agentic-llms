const path = require('path');
const { createRequire } = require('module');
const frontendRequire = createRequire(
  path.join(__dirname, '../../../frontend/lex-intel/package.json'),
);
const { test, expect } = frontendRequire('@playwright/test');

const JSON_HEADERS = {
  'access-control-allow-origin': '*',
  'content-type': 'application/json',
};

const DOCX_HEADERS = {
  'access-control-allow-origin': '*',
  'access-control-expose-headers': 'content-disposition',
  'content-disposition': 'attachment; filename="Three_Document_Task.docx"',
  'content-type': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
};

function fulfillJson(route, payload, status = 200) {
  return route.fulfill({
    status,
    headers: JSON_HEADERS,
    body: JSON.stringify(payload),
  });
}

function documentState(completed, chatThreadId = 'e2e-case-assistant') {
  return {
    chat_thread_id: chatThreadId,
    draft_document_id: `document-${chatThreadId}`,
    draft_version: 1,
    draft: {
      title: 'Reply to Opposition',
      sections: completed
        ? [
            {
              section_id: 'introduction',
              section_name: 'Introduction',
              section_content: 'A reply grounded in the attached documents.',
            },
            {
              section_id: 'argument',
              section_name: 'Argument',
              section_content: 'The opposition is unpersuasive.',
            },
          ]
        : [],
    },
    chatHistory: completed
      ? [
          {
            input: 'Draft a reply to the Opposition to the Motion to Dismiss',
            response: 'Drafted a reply based on the three attached documents.',
          },
        ]
      : [],
    chatHistoryMeta: completed
      ? [
          {
            prompt_attachments: [
              { file_id: 'file-1', file_name: 'opposition.pdf' },
              { file_id: 'file-2', file_name: 'motion-to-dismiss.pdf' },
              { file_id: 'file-3', file_name: 'declaration.pdf' },
            ],
          },
        ]
      : [],
  };
}

function confirmationResult() {
  return {
    awaiting_confirmation: true,
    workflow_run_id: 'workflow-1',
    confirmation: {
      message: 'How should this reply be handled?',
      questions: [
        {
          id: 'draft_action',
          header: 'Draft',
          question: 'Choose a workflow for this reply.',
          options: [
            {
              id: 'new-reply',
              label: 'Draft a new reply',
              description: 'Create a new reply from the attached materials.',
              decision: {
                draft_action: 'new',
                revision_mode: 'general_revision',
                selected_file_ids: ['file-1', 'file-2', 'file-3'],
              },
            },
            {
              id: 'revise',
              label: 'Revise the existing draft',
              description: 'Use the documents to revise the current draft.',
              decision: {
                draft_action: 'revise',
                revision_mode: 'general_revision',
                selected_file_ids: ['file-1', 'file-2', 'file-3'],
              },
            },
          ],
        },
      ],
    },
  };
}

async function mockCaseAssistantApis(page, state) {
  const agentQuestion = { kind: 'agent_question', message: 'Which exhibit should I use?', options: [] };
  let serverUploadedFiles = state.savedFiles || [];
  let preparationDocuments = serverUploadedFiles.map((file, index) => ({
    document_id: file.unique_id, unique_id: file.unique_id, filename: file.filename, file_size: file.file_size,
    clientUploadId: `saved-${index}`, group_id: 'saved', scope: 'chat', status: 'ready', stage: 'ready',
    artifact_id: `artifact-${file.unique_id}`, committed: !!state.completed && index < 3,
    metadata: { status: 'ready', headline: 'Prepared legal document', preview: 'A prepared document.', key_points: [] },
  }));
  const preparationPolicy = { max_files: 2, max_bytes: 1024 * 1024, concurrency: 2,
    chat_files: 100, matter_files: 30, total_bytes: 1024 ** 3 };

  await page.route('https://script.google.com/**', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: '{}',
  }));

  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const pathname = url.pathname;

    if (request.method() === 'GET' && pathname === '/api/everything/document-preparation') {
      state.preparationPolls = (state.preparationPolls || 0) + 1;
      preparationDocuments.forEach((doc, index) => {
        if (doc.needs_reupload || doc.group_id === 'saved') return;
        const completed = state.completedUploadCount === undefined || index < state.completedUploadCount;
        const ready = state.preparationReady !== undefined ? state.preparationReady
          : state.holdSummaries ? !!state.summariesReady : true;
        doc.status = doc.stage = completed ? (ready ? 'ready' : 'summarizing') : 'parsing';
        if (completed) {
          state.completedUploads = state.completedUploads || new Set();
          state.completedUploads.add(doc.document_id);
        }
        doc.artifact_id = ready && completed ? `artifact-${doc.document_id}` : null;
        if (state.coverageGaps && ready && completed) {
          doc.artifact_id += `-v${state.coverageVersion || 1}`;
          doc.coverage = { extraction: { status: 'partial', gap_fingerprint: `${doc.artifact_id}:gap`,
            gaps: [{ page: 4, message: 'This page could not be read.' }] } };
          doc.warning = 'Review unreadable content.';
        }
        doc.metadata = { status: doc.status === 'ready' ? 'ready' : 'pending', headline: 'Prepared legal document',
          preview: 'A prepared document.', summary: 'A prepared document.', document_kind: 'pleading', key_points: [] };
      });
      if (preparationDocuments.some(doc => doc.status !== 'ready')) state.listDuringUploads = true;
      return fulfillJson(route, { enabled: true, owner_id: 1, policy: preparationPolicy, documents: preparationDocuments,
        revision: preparationDocuments.map(doc => doc.status).join(',') });
    }
    if (request.method() === 'GET' && /^\/api\/everything\/documents\/[^/]+\/summary$/.test(pathname)) {
      return fulfillJson(route, { detailed_digest: 'A detailed document digest with source facts.', section_outline: [] });
    }
    if (request.method() === 'DELETE' && pathname.startsWith('/api/everything/documents/')) {
      state.deletedDocumentId = pathname.split('/').pop();
      preparationDocuments = preparationDocuments.filter(doc => doc.document_id !== state.deletedDocumentId);
      serverUploadedFiles = serverUploadedFiles.filter(file => file.unique_id !== state.deletedDocumentId);
      return fulfillJson(route, {});
    }
    if (request.method() === 'POST' && pathname === '/api/everything/upload-groups') {
      const body = request.postDataJSON();
      state.uploadedCount = (state.uploadedCount || 0) + body.documents.length;
      const start = preparationDocuments.length;
      const added = body.documents.map((doc, index) => ({
        ...doc, document_id: `file-${start + index + 1}`, unique_id: `file-${start + index + 1}`,
        clientUploadId: doc.client_upload_id, group_id: 'group-1', scope: 'chat', committed: false,
        status: 'queued', stage: 'awaiting_upload', needs_reupload: true, generation: 1,
      }));
      preparationDocuments.push(...added);
      return fulfillJson(route, { group_id: 'group-1', documents: added, policy: preparationPolicy }, 201);
    }
    if (request.method() === 'POST' && pathname === '/api/everything/upload-groups/group-1/files') {
      const idsMatch = (request.postData() || '').match(/name="clientUploadIds"\r?\n\r?\n([^\r\n]+)/);
      const ids = JSON.parse(idsMatch[1]);
      expect(ids.length).toBeLessThanOrEqual(2);
      state.transferGroups = (state.transferGroups || 0) + 1;
      const documents = preparationDocuments.filter(doc => ids.includes(doc.clientUploadId));
      documents.forEach(doc => { doc.status = doc.stage = 'summarizing'; doc.needs_reupload = false; });
      serverUploadedFiles = preparationDocuments.map(doc => ({ unique_id: doc.document_id, filename: doc.filename,
        file_size: doc.file_size, upload_time: '2026-09-02T00:00:00.000Z' }));
      return fulfillJson(route, { documents, failed: [] }, 202);
    }

    if (request.method() === 'POST' && pathname === '/api/list-projects') {
      return fulfillJson(route, { projects: [['E2E Matter', 1]] });
    }
    if (request.method() === 'POST' && pathname === '/api/list-drafts') {
      return fulfillJson(route, {
        drafts: [['e2e-case-assistant', 'Three Document Task', '', 69]],
      });
    }
    if (request.method() === 'GET' && pathname === '/api/smart-search/sessions') {
      return fulfillJson(route, { sessions: [] });
    }
    if (request.method() === 'GET' && pathname === '/api/smart-search/tags/1') {
      return fulfillJson(route, { tags: [] });
    }
    if (request.method() === 'GET' && pathname === '/api/draft-name/e2e-case-assistant') {
      return fulfillJson(route, {
        draftId: 'e2e-case-assistant',
        draftName: 'Three Document Task',
        projectId: 1,
        docCategory: 69,
      });
    }
    if (request.method() === 'POST' && pathname === '/api/get_document') {
      const chatThreadId = request.postDataJSON().chat_thread_id || request.postDataJSON().draftId;
      const data = { ...structuredClone(state.documents?.[chatThreadId]
        || state.document || documentState(state.completed, chatThreadId)),
        chat_thread_id: chatThreadId, draft_document_id: `document-${chatThreadId}` };
      if (state.workflowId) {
        const meta = { workflow_run_id: state.workflowId, extraction_report: state.workflowId, prompt_attachments: [] };
        if (state.completed) {
          data.chatHistoryMeta[0] = { ...data.chatHistoryMeta[0], ...meta };
          if (state.completedResponse) data.chatHistory[0].response = state.completedResponse;
        } else if (state.workflowFailed) {
          data.chatHistory = [[state.promptBody.query, 'A document section failed. Retry to resume saved work.']];
          data.chatHistoryMeta = [{ ...meta, workflow_failed: true }];
        } else {
          data.activeWorkflow = { workflow_run_id: state.workflowId,
            status: state.awaitingQuestion ? 'awaiting_user_confirmation' : 'running' };
          data.pendingTurn = { input: state.promptBody.query, response: state.awaitingQuestion ? agentQuestion.message : '',
            draftId: 'e2e-case-assistant', workflowRunId: state.workflowId, promptAttachments: [],
            awaitingConfirmation: !!state.awaitingQuestion, confirmation: state.awaitingQuestion ? agentQuestion : undefined };
        }
      }
      return fulfillJson(route, data);
    }
    if (request.method() === 'POST' && ['/api/update_section_text', '/api/delete_draft_section'].includes(pathname)) {
      const body = request.postDataJSON();
      state.editorBodies = [...(state.editorBodies || []), body];
      if (state.beforeEditorSave) await state.beforeEditorSave(body);
      const chatThreadId = body.chat_thread_id;
      const current = state.documents?.[chatThreadId] || state.document || documentState(state.completed, chatThreadId);
      if (body.draft_document_id !== `document-${chatThreadId}`) return fulfillJson(route, { msg: 'Document belongs to another chat.' }, 404);
      if (body.expected_draft_version !== current.draft_version) return fulfillJson(route, {
        code: 'draft_conflict', draft_version: current.draft_version,
        msg: 'The draft changed elsewhere. Your unsaved edits have been kept.',
      }, 409);
      // Mirror the canonical write boundary: editor mutations only replace draft state.
      const draft = pathname.endsWith('delete_draft_section')
        ? { ...current.draft, sections: current.draft.sections.filter(section => section.section_id !== body.sectionId) }
        : body.draft;
      const next = { ...current, draft, draft_version: current.draft_version + 1 };
      if (state.documents) state.documents[chatThreadId] = next;
      else state.document = next;
      return fulfillJson(route, { draft: next.draft, draft_version: next.draft_version,
        chat_thread_id: chatThreadId, draft_document_id: `document-${chatThreadId}`, ...state.editorResponseOverrides });
    }
    if (request.method() === 'POST' && pathname === '/api/get-uploaded-files-everything') {
      if (state.uploadedCount && state.completedUploadCount !== undefined) state.listDuringUploads = true;
      serverUploadedFiles.forEach(file => {
        file.metadata = { status: state.holdSummaries && !state.summariesReady ? 'pending' : 'ready',
          summary: 'A prepared document.', document_kind: 'pleading' };
      });
      return fulfillJson(route, { uploaded_files: serverUploadedFiles });
    }
    if (request.method() === 'POST' && pathname === '/api/delete-uploaded-file-everything') {
      const body = request.postDataJSON();
      serverUploadedFiles = serverUploadedFiles.filter(file => file.unique_id !== body.unique_id);
      state.deletedDocumentId = body.unique_id;
      return fulfillJson(route, { msg: 'File deleted' });
    }
    if (request.method() === 'POST' && pathname === '/api/get-uploaded-files-matter-everything') {
      return fulfillJson(route, { folders: [], loose_files: [] });
    }
    if (request.method() === 'POST' && pathname === '/api/get-uploaded-files-search') {
      return fulfillJson(route, { folders: [], loose_files: [] });
    }
    if (request.method() === 'POST' && pathname === '/api/everything/upload-files-async') {
      throw new Error('The frontend must use durable upload groups.');
    }
    if (request.method() === 'POST' && pathname === '/api/everything') {
      state.promptBody = JSON.parse(request.postData() || '{}');
      state.promptCount = (state.promptCount || 0) + 1;
      const submitted = new Set((state.promptBody.promptAttachments || []).map(file => file.file_id));
      preparationDocuments.filter(doc => submitted.has(doc.document_id)).forEach(doc => { doc.committed = true; });
      if (state.durableWorkflow) {
        state.workflowId = state.promptBody.workflow_run_id;
        if (state.admissionResponseLost) return fulfillJson(route, { msg: 'Response interrupted after admission.' }, 503);
        return fulfillJson(route, { result_id: state.workflowId, workflow_run_id: state.workflowId });
      }
      return fulfillJson(route, { result_id: 'assistant-task-1' });
    }
    if (state.workflowId && request.method() === 'POST' && pathname === `/api/everything/workflows/${state.workflowId}/retry`) {
      state.retryWorkflowId = state.workflowId;
      state.workflowFailed = false;
      return fulfillJson(route, { result_id: state.workflowId, workflow_run_id: state.workflowId });
    }
    if (state.workflowId && request.method() === 'POST' && pathname === `/api/cancel/${state.workflowId}`) {
      if (state.cancelUnavailable) return fulfillJson(route, { msg: 'Temporary cancellation failure.' }, 503);
      state.cancelCount = (state.cancelCount || 0) + 1;
      if (state.completeOnCancel) state.completed = true;
      if (state.failOnCancel) state.workflowFailed = true;
      state.workflowCancelled = !state.completed && !state.workflowFailed;
      return fulfillJson(route, { workflow_run_id: state.workflowId,
        status: state.completed ? 'completed' : state.workflowFailed ? 'failed' : 'cancelled', cancelled: state.workflowCancelled });
    }
    if (state.workflowId && request.method() === 'GET' && pathname === `/api/everything/workflows/${state.workflowId}/evidence`) {
      return fulfillJson(route, { workflow_run_id: state.workflowId, total_sections: 34, required_sections: 34,
        complete: state.completed, extraction_complete: state.completed, coverage_mode: 'whole_document', next: null,
        sections: [{ section_id: 'section-4', file_id: 'file-1', filename: 'correspondence.txt', scope: 'chat',
          extraction_coverage: { status: 'complete' }, excerpts: [{ block_id: 'b004', quote: 'The extension was rejected on July 12.', source_location: { page: 4 } }] }] });
    }
    if (state.workflowId && request.method() === 'GET' && pathname === `/api/everything/workflows/${state.workflowId}`) {
      if (state.interruptProgress) {
        state.interruptProgress = false;
        return fulfillJson(route, { msg: 'Temporarily unavailable.' }, 503);
      }
      if (state.workflowFinished) state.completed = true;
      return fulfillJson(route, { workflow_run_id: state.workflowId,
        ready: state.completed || state.workflowFailed || state.workflowCancelled || state.awaitingQuestion,
        state: state.workflowCancelled ? 'REVOKED' : undefined,
        report_available: true, status: state.completed ? 'completed' : state.workflowFailed ? 'failed'
          : state.workflowCancelled ? 'cancelled' : state.awaitingQuestion ? 'awaiting_user_confirmation' : 'running',
        info: { status: 'Extracting passages: 22 of 34 sections completed', counts: { 'query:ready': 22, 'query:queued': 12 } },
        value: state.workflowFailed ? { error: true, error_message: 'A document section failed. Retry to resume saved work.' }
          : state.completed ? { response: state.completedResponse || 'Drafted a reply based on the three attached documents.' }
            : state.awaitingQuestion ? { awaiting_confirmation: true, workflow_run_id: state.workflowId, confirmation: agentQuestion } : null });
    }
    if (request.method() === 'POST' && pathname === '/api/smart-search/query') {
      state.searchBody = JSON.parse(request.postData() || '{}');
      return fulfillJson(route, {
        result_id: 'search-task-1',
        session_id: 'search-session-1',
      });
    }
    if (request.method() === 'POST' && pathname === '/api/everything/confirm-workflow') {
      state.confirmationBody = JSON.parse(request.postData() || '{}');
      if (state.durableWorkflow && state.confirmationBody.decision.cancelled) {
        if (state.confirmationCancelUnavailable) return fulfillJson(route, { msg: 'Temporary cancellation failure.' }, 503);
        state.confirmationCancelCount = (state.confirmationCancelCount || 0) + 1;
        if (state.completeOnConfirmationCancel) state.completed = true;
        state.workflowCancelled = !state.completed;
        return fulfillJson(route, { workflow_run_id: state.workflowId,
          status: state.completed ? 'completed' : 'cancelled', cancelled: !state.completed });
      }
      state.completed = true;
      return fulfillJson(route, { result_id: 'assistant-task-2' });
    }
    if (request.method() === 'GET' && pathname === '/api/result/assistant-task-1') {
      if (state.keepFirstTaskPending) {
        state.pendingPollCount = (state.pendingPollCount || 0) + 1;
        if (!state.returnTaskTimeout) {
          return fulfillJson(route, { ready: false, state: 'STARTED' });
        }
        return fulfillJson(route, {
          ready: true,
          successful: true,
          value: {
            error: true,
            error_code: 'task_timeout',
            retryable: true,
            error_message: 'Case Assistant exceeded its processing time limit.',
          },
        });
      }
      return fulfillJson(route, {
        ready: true,
        successful: true,
        value: confirmationResult(),
      });
    }
    if (request.method() === 'GET' && pathname === '/api/result/assistant-task-2') {
      return fulfillJson(route, {
        ready: true,
        successful: true,
        value: { ok: true },
      });
    }
    if (request.method() === 'GET' && pathname === '/api/result/search-task-1') {
      return fulfillJson(route, {
        ready: true,
        successful: true,
        value: { response: 'Search completed.', sources: [] },
      });
    }
    if (request.method() === 'POST' && pathname === '/api/download-everything/e2e-case-assistant') {
      state.downloadBody = JSON.parse(request.postData() || '{}');
      return route.fulfill({
        status: 200,
        headers: DOCX_HEADERS,
        body: Buffer.from('PK\\x03\\x04 E2E fixture'),
      });
    }

    return fulfillJson(route, {
      error: `Unexpected E2E request: ${request.method()} ${pathname}`,
    }, 404);
  });
  return {
    uploadedFiles: () => serverUploadedFiles,
    preparation: () => ({ enabled: true, owner_id: 1, policy: preparationPolicy,
      documents: preparationDocuments, revision: JSON.stringify(preparationDocuments) }),
  };
}

async function mockMatterSwitchingApis(page, hasChat = false) {
  const state = { completed: false, savedFiles: [{ unique_id: 'a-file', filename: 'A-only.txt', file_size: 4 }] };
  await mockCaseAssistantApis(page, state);
  await page.route('**/api/list-projects', route => fulfillJson(route, {
    projects: [['E2E Matter', 1], ['Second Matter', 2]],
  }));
  await page.route('**/api/list-drafts', route => {
    if (route.request().postDataJSON()['proj-id'] !== 2) return route.fallback();
    return fulfillJson(route, { drafts: hasChat ? [['second-chat', 'Second chat', '', 69]] : [] });
  });
  await page.route('**/api/draft-name/second-chat', route => fulfillJson(route, {
    draftId: 'second-chat', draftName: 'Second chat', projectId: 2, docCategory: 69,
  }));
  await page.route('**/api/get-uploaded-files-everything', route => {
    if (route.request().postDataJSON().draftId !== 'second-chat') return route.fallback();
    return fulfillJson(route, { uploaded_files: [] });
  });
  await page.route('**/api/everything/document-preparation?**', route => {
    const params = new URL(route.request().url()).searchParams;
    if (params.get('projectId') !== '2' && params.get('draftId') !== 'second-chat') return route.fallback();
    return fulfillJson(route, { enabled: true, documents: [], policy: { chat_files: 100 }, revision: 'empty' });
  });
  await page.addInitScript(() => {
    localStorage.setItem('videoWelcomeSeen_v2.0', 'true');
    if (!localStorage.getItem('li_selectedProject')) {
      localStorage.setItem('li_selectedProject', '1');
      localStorage.setItem('li_selectedDraft', 'e2e-case-assistant');
    }
  });
  return state;
}

async function openPersistenceWorkspace(page, state) {
  await mockCaseAssistantApis(page, state);
  await page.addInitScript(() => {
    localStorage.setItem('videoWelcomeSeen_v2.0', 'true');
    localStorage.setItem('li_selectedProject', '1');
    localStorage.setItem('li_selectedDraft', 'e2e-case-assistant');
  });
  await page.goto('/case-assistant/e2e-case-assistant');
  await expect(page.getByRole('heading', { name: state.document.draft.title, exact: true })).toBeVisible();
}

async function editDraftTitle(page, title) {
  await page.getByRole('button', { name: 'Edit title', exact: true }).click();
  const row = page.getByRole('button', { name: 'Save', exact: true }).locator('..');
  await row.getByRole('textbox').fill(title);
  await row.getByRole('button', { name: 'Save', exact: true }).click();
}

// Keep workflow state separate per chat so late responses exercise real UI
// ownership checks instead of sharing the original single-workflow mock.
async function mockChatRunScopes(page, state) {
  const backend = await mockCaseAssistantApis(page, { completed: false });
  state.chats = {
    'e2e-case-assistant': { label: 'Chat A', ...state.chatA },
    'scope-chat-B': { label: 'Chat B', ...state.chatB },
  };
  state.submissions = [];
  state.cancellations = [];
  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const pathname = url.pathname;
    const body = request.method() === 'POST' ? request.postDataJSON() : null;
    if (pathname === '/api/list-drafts') return fulfillJson(route, {
      drafts: Object.entries(state.chats).map(([id, chat]) => [id, chat.label, '', 69]),
    });
    if (pathname.startsWith('/api/draft-name/')) {
      const id = pathname.split('/').pop();
      if (state.chats[id]) return fulfillJson(route, {
        draftId: id, draftName: state.chats[id].label, projectId: 1, docCategory: 69,
      });
    }
    if (pathname === '/api/get_document') {
      const id = body.chat_thread_id || body.draftId;
      await state.beforeDocument?.(id);
      const chat = state.chats[id];
      const document = documentState(false, id);
      if (chat?.run) {
        const run = chat.run;
        if (run.status === 'failed' || run.status === 'completed') {
          document.chatHistory = [[run.query, run.response || `${chat.label} saved failure.`]];
          document.chatHistoryMeta = [{ workflow_run_id: run.id, agent_run: true,
            workflow_failed: run.status === 'failed' }];
        } else if (run.status !== 'cancelled') {
          document.activeWorkflow = { workflow_run_id: run.id,
            status: run.status === 'question' ? 'awaiting_user_confirmation' : 'running' };
          document.pendingTurn = { input: run.query, response: '', draftId: id,
            workflowRunId: run.id, agentRun: true, promptAttachments: [],
            awaitingConfirmation: run.status === 'question',
            confirmation: run.status === 'question' ? {
              kind: 'agent_question', message: 'Which authority should Chat A use?', options: [],
            } : undefined };
        }
      }
      return fulfillJson(route, document);
    }
    if (pathname === '/api/get-uploaded-files-everything') {
      return fulfillJson(route, { uploaded_files: [] });
    }
    if (pathname === '/api/everything/document-preparation') {
      return fulfillJson(route, { ...backend.preparation(), documents: [] });
    }
    if (pathname === '/api/everything') {
      const id = body.chat_thread_id;
      state.chats[id].run = { id: body.workflow_run_id, query: body.query, status: 'running' };
      state.submissions.push(body);
      await state.beforeSubmission?.(id, body);
      if (state.rejectSubmission === id) {
        state.chats[id].run = null;
        return fulfillJson(route, { msg: 'Old chat admission failed.' }, 400);
      }
      return fulfillJson(route, { result_id: body.workflow_run_id, workflow_run_id: body.workflow_run_id });
    }
    const workflowId = pathname.match(/^\/api\/(?:everything\/workflows|cancel)\/(wf_[^/]+)/)?.[1];
    if (workflowId) {
      const entry = Object.entries(state.chats).find(([, chat]) => chat.run?.id === workflowId);
      if (!entry) return fulfillJson(route, { msg: 'Workflow not found' }, 404);
      const [id, chat] = entry;
      if (pathname.startsWith('/api/cancel/')) {
        state.cancellations.push(workflowId);
        chat.run.status = 'cancelled';
        await state.beforeCancellation?.(id);
        return fulfillJson(route, { workflow_run_id: workflowId, status: 'cancelled', cancelled: true });
      }
      if (pathname.endsWith('/retry')) {
        chat.run.status = 'running';
        await state.beforeRetry?.(id);
        return fulfillJson(route, { result_id: workflowId, workflow_run_id: workflowId });
      }
      if (!pathname.endsWith(workflowId)) return fulfillJson(route, { artifacts: [], steps: [] });
      const run = structuredClone(chat.run);
      await state.beforePoll?.(id, run);
      return fulfillJson(route, { workflow_run_id: workflowId,
        ready: run.status !== 'running', status: run.status,
        state: run.status === 'cancelled' ? 'REVOKED' : undefined,
        info: { status: `${chat.label} is working.` },
        value: run.status === 'completed' ? { response: run.response || `${chat.label} answer.` }
          : run.status === 'failed' ? { error_message: `${chat.label} saved failure.` }
            : run.status === 'question' ? { awaiting_confirmation: true, workflow_run_id: workflowId,
              confirmation: { kind: 'agent_question', message: 'Which authority should Chat A use?', options: [] } }
              : null });
    }
    if (pathname === '/api/everything/confirm-workflow') {
      const id = body.draftId;
      const run = state.chats[id].run;
      run.status = 'running';
      await state.beforeConfirmation?.(id);
      return fulfillJson(route, { result_id: run.id, workflow_run_id: run.id });
    }
    return route.fallback();
  });
  await page.addInitScript(() => {
    localStorage.setItem('videoWelcomeSeen_v2.0', 'true');
    localStorage.setItem('li_selectedProject', '1');
    localStorage.setItem('li_selectedDraft', 'e2e-case-assistant');
  });
  await page.goto(state.entryPath || '/case-assistant/e2e-case-assistant');
}

function delayedChatResponse() {
  let release, notify;
  const gate = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { notify = resolve; });
  return { release, started, wait: () => { notify(); return gate; } };
}

async function selectRunScopeChat(page, id) {
  await page.evaluate(chatId => {
    history.pushState({}, '', `/case-assistant/${chatId}`);
    window.dispatchEvent(new PopStateEvent('popstate'));
  }, id);
  await expect(page).toHaveURL(`/case-assistant/${id}`);
}

async function submitScopeChatB(page, state) {
  await selectRunScopeChat(page, 'scope-chat-B');
  await page.locator('textarea.chat-input').fill('Work on B only.');
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect.poll(() => state.chats['scope-chat-B'].run?.id).toMatch(/^wf_/);
  await expect(page.getByRole('button', { name: 'Stop', exact: true })).toBeVisible();
  return state.chats['scope-chat-B'].run.id;
}

async function expectStopTargetsChatB(page, state, workflowId) {
  await expect(page.getByText('Work on B only.', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Stop', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Stop', exact: true }).click();
  await expect.poll(() => state.cancellations[state.cancellations.length - 1]).toBe(workflowId);
  await expect(page.getByText('Request cancelled.', { exact: true })).toBeVisible();
}

test('workflow controls belong to the selected chat and restore when returning to its run', async ({ page }) => {
  const state = { entryPath: '/case-assistant/e2e-case-assistant/' };
  await mockChatRunScopes(page, state);
  await page.locator('textarea.chat-input').fill('Work on A only.');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Stop', exact: true })).toBeVisible();
  await expect.poll(() => state.chats['e2e-case-assistant'].run?.id).toMatch(/^wf_/);
  const aId = state.chats['e2e-case-assistant'].run.id;
  await selectRunScopeChat(page, 'scope-chat-B');
  await page.locator('textarea.chat-input').fill('B remains available.');
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeEnabled();
  await expect(page.getByRole('button', { name: 'Stop', exact: true })).toHaveCount(0);
  expect(state.cancellations).toEqual([]);
  await selectRunScopeChat(page, 'e2e-case-assistant');
  await expect(page.getByRole('button', { name: 'Stop', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Stop', exact: true }).click();
  await expect.poll(() => state.cancellations).toEqual([aId]);
});

test('a delayed preflight document read from A cannot submit or change B workflow controls', async ({ page }) => {
  const delayed = delayedChatResponse();
  const state = {};
  await mockChatRunScopes(page, state);
  await page.locator('textarea.chat-input').fill('Work on A only.');
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeEnabled();
  state.beforeDocument = id => id === 'e2e-case-assistant' ? delayed.wait() : undefined;
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await delayed.started;
  const bId = await submitScopeChatB(page, state);
  const response = page.waitForResponse(r => r.url().endsWith('/api/get_document')
    && (r.request().postDataJSON().chat_thread_id || r.request().postDataJSON().draftId) === 'e2e-case-assistant');
  delayed.release(); await response;
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  expect(state.submissions.map(body => body.chat_thread_id)).toEqual(['scope-chat-B']);
  await expectStopTargetsChatB(page, state, bId);
});

for (const rejected of [false, true]) {
  test(`a delayed ${rejected ? 'failed' : 'accepted'} submission from A cannot replace B workflow controls`, async ({ page }) => {
    const delayed = delayedChatResponse();
    const state = { beforeSubmission: id => id === 'e2e-case-assistant' ? delayed.wait() : undefined,
      rejectSubmission: rejected ? 'e2e-case-assistant' : null };
    await mockChatRunScopes(page, state);
    await page.locator('textarea.chat-input').fill('Work on A only.');
    await page.getByRole('button', { name: 'Send', exact: true }).click();
    await delayed.started;
    const bId = await submitScopeChatB(page, state);
    const response = page.waitForResponse(r => r.url().endsWith('/api/everything')
      && r.request().postDataJSON().chat_thread_id === 'e2e-case-assistant');
    delayed.release(); await response;
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await expect(page.getByText('Error fetching data. Please try again.', { exact: true })).toHaveCount(0);
    await expectStopTargetsChatB(page, state, bId);
    expect(state.cancellations).toEqual([bId]);
  });
}

for (const operation of ['retry', 'confirmation', 'Stop']) {
  test(`a delayed ${operation} response from A cannot change B workflow controls`, async ({ page }) => {
    const delayed = delayedChatResponse();
    const run = { id: 'wf_scope_A', query: 'Work on A only.',
      status: operation === 'retry' ? 'failed' : operation === 'confirmation' ? 'question' : 'running' };
    const hook = operation === 'retry' ? 'beforeRetry' : operation === 'confirmation' ? 'beforeConfirmation' : 'beforeCancellation';
    const state = { chatA: { run }, [hook]: id => id === 'e2e-case-assistant' ? delayed.wait() : undefined };
    await mockChatRunScopes(page, state);
    if (operation === 'confirmation') {
      await page.getByRole('textbox', { name: 'Your answer', exact: true }).fill('Use the source document.');
      await page.getByRole('button', { name: 'Continue', exact: true }).click();
    } else await page.getByRole('button', { name: operation === 'retry' ? 'Retry' : 'Stop', exact: true }).click();
    await delayed.started;
    const bId = await submitScopeChatB(page, state);
    const response = page.waitForResponse(r => operation === 'confirmation'
      ? r.url().endsWith('/api/everything/confirm-workflow')
      : r.url().endsWith(operation === 'retry' ? '/wf_scope_A/retry' : '/cancel/wf_scope_A'));
    delayed.release(); await response;
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await expect(page.getByText('Confirmed: Use the source document....', { exact: false })).toHaveCount(0);
    await expectStopTargetsChatB(page, state, bId);
    expect(state.cancellations.filter(id => id !== 'wf_scope_A')).toEqual([bId]);
  });
}

test('a late poll from A cannot display its completion or clear B workflow controls', async ({ page }) => {
  const delayed = delayedChatResponse();
  let held = false;
  const state = { chatA: { run: { id: 'wf_scope_A', query: 'Work on A only.', status: 'running' } },
    beforePoll: async (id, run) => {
      if (id !== 'e2e-case-assistant' || held) return;
      held = true;
      await delayed.wait();
      run.status = 'completed';
      run.response = 'A late completion must stay in A.';
    } };
  await mockChatRunScopes(page, state);
  await delayed.started;
  const bId = await submitScopeChatB(page, state);
  const response = page.waitForResponse(r => r.url().endsWith('/api/everything/workflows/wf_scope_A'));
  delayed.release(); await response;
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await expect(page.getByText('A late completion must stay in A.', { exact: true })).toHaveCount(0);
  await expectStopTargetsChatB(page, state, bId);
});

test('draft persistence keeps a completed answer when an in-flight editor save finishes and reloads', async ({ page }, testInfo) => {
  let releaseSave;
  const gate = new Promise(resolve => { releaseSave = resolve; });
  const state = { completed: true, document: documentState(true), beforeEditorSave: () => gate };
  await openPersistenceWorkspace(page, state);
  await editDraftTitle(page, 'Manually saved reply');
  await expect.poll(() => state.editorBodies?.length).toBe(1);
  expect(state.editorBodies[0].expected_draft_version).toBe(1);
  expect(state.editorBodies[0]).toMatchObject({
    chat_thread_id: 'e2e-case-assistant', draft_document_id: 'document-e2e-case-assistant',
  });
  expect(state.editorBodies[0]).not.toHaveProperty('draftId');

  // A workflow publishes an answer while the editor request is still in flight.
  state.document.chatHistory.push({ input: 'Explain the deadline.', response: 'The completed deadline analysis remains saved.' });
  state.document.chatHistoryMeta.push({ workflow_run_id: 'completed-answer' });
  releaseSave();
  await expect(page.getByRole('heading', { name: 'Manually saved reply', exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Manually saved reply', exact: true })).toBeVisible();
  await expect(page.getByText('The completed deadline analysis remains saved.', { exact: true })).toBeVisible();
  await expect(page.getByText('Drafted a reply based on the three attached documents.', { exact: true })).toBeVisible();
  expect(state.document.chatHistory).toHaveLength(2);
  await page.screenshot({ path: testInfo.outputPath('draft-and-completed-history.png') });
});

test('draft persistence preserves conflict edits for copying until explicit reload', async ({ page }, testInfo) => {
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
  const state = { completed: true, document: documentState(true) };
  state.document.draft.sections = [{ section_id: 'body', section_name: '', section_content: 'Original agreed terms.' }];
  await openPersistenceWorkspace(page, state);
  const body = page.locator('div.draft-section').filter({ hasText: 'Original agreed terms.' });
  await body.getByRole('button', { name: 'Manual edit', exact: true }).click();
  const editor = page.locator('[contenteditable="true"]');
  await editor.fill('My unsaved replacement terms.');
  // Another writer has advanced only the canonical draft while this editor remains open.
  state.document.draft_version = 2;
  state.document.draft.sections[0].section_content = 'New server terms from another writer.';
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.getByRole('alert').filter({ hasText: 'The draft changed elsewhere.' })).toBeVisible();
  await expect(editor).toHaveText('My unsaved replacement terms.');
  await editor.press('ControlOrMeta+A');
  await editor.press('ControlOrMeta+C');
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toContain('My unsaved replacement terms.');
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  expect(state.editorBodies).toHaveLength(1); // No automatic retry or stale resend.
  await page.screenshot({ path: testInfo.outputPath('draft-conflict-keeps-editor.png') });
  await page.getByRole('button', { name: 'Reload current draft', exact: true }).click();
  await expect(page.locator('[contenteditable="true"]')).toHaveCount(0);
  await expect(page.getByText('New server terms from another writer.', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Reload current draft', exact: true })).toHaveCount(0);
  await expect(page.getByText('Drafted a reply based on the three attached documents.', { exact: true })).toBeVisible();
});

test('draft persistence serializes queued editor saves using the latest acknowledged version', async ({ page }) => {
  let releaseSave;
  const gate = new Promise(resolve => { releaseSave = resolve; });
  const state = { completed: true, document: documentState(true), beforeEditorSave: () => gate };
  await openPersistenceWorkspace(page, state);
  await editDraftTitle(page, 'First pending title');
  await expect.poll(() => state.editorBodies?.length).toBe(1);
  const row = page.getByRole('button', { name: 'Save', exact: true }).locator('..');
  await row.getByRole('textbox').fill('Latest queued title');
  await row.getByRole('button', { name: 'Save', exact: true }).click();
  expect(state.editorBodies).toHaveLength(1);
  releaseSave();
  await expect.poll(() => state.editorBodies.length).toBe(2);
  await expect(page.getByRole('heading', { name: 'Latest queued title', exact: true })).toBeVisible();
  expect(state.editorBodies.map(body => body.expected_draft_version)).toEqual([1, 2]);
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Latest queued title', exact: true })).toBeVisible();
  expect(state.document.draft_version).toBe(3);
});

test('draft persistence keeps edits when a save response identifies another document', async ({ page }, testInfo) => {
  const state = { completed: true, document: documentState(true),
    editorResponseOverrides: { draft_document_id: 'document-another-chat' } };
  await openPersistenceWorkspace(page, state);
  await editDraftTitle(page, 'Keep my local title');
  await expect.poll(() => state.editorBodies?.length).toBe(1);
  const row = page.getByRole('button', { name: 'Save', exact: true }).locator('..');
  await expect(row.getByRole('textbox')).toHaveValue('Keep my local title');
  await expect(page.getByText('The save response was for another document. Your edits remain in the editor. Reload the draft.', { exact: true })).toBeVisible();
  expect(state.editorBodies[0]).toMatchObject({ chat_thread_id: 'e2e-case-assistant',
    draft_document_id: 'document-e2e-case-assistant' });
  await page.screenshot({ path: testInfo.outputPath('wrong-document-response-keeps-editor.png') });
});

test('draft persistence ignores a late editor response after switching workspaces', async ({ page }) => {
  const state = await mockMatterSwitchingApis(page, true);
  const first = documentState(true);
  const second = { ...documentState(true), draft: { title: 'Second workspace draft', sections: [
    { section_id: 'second-body', section_name: '', section_content: 'Second workspace content.' },
  ] }, draft_version: 8 };
  state.documents = { 'e2e-case-assistant': first, 'second-chat': second };
  let releaseSave;
  const gate = new Promise(resolve => { releaseSave = resolve; });
  state.beforeEditorSave = () => gate;
  await page.goto('/case-assistant/e2e-case-assistant');
  await expect(page.getByRole('heading', { name: first.draft.title, exact: true })).toBeVisible();
  await editDraftTitle(page, 'Late first-workspace title');
  await expect.poll(() => state.editorBodies?.length).toBe(1);
  await page.getByText('Second Matter', { exact: true }).click();
  await page.getByRole('listitem', { name: 'Double-click to edit name', exact: true }).getByText('Second chat', { exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Second workspace draft', exact: true })).toBeVisible();
  releaseSave();
  await expect.poll(() => state.documents['e2e-case-assistant'].draft_version).toBe(2);
  await expect(page.getByRole('heading', { name: 'Second workspace draft', exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Late first-workspace title', exact: true })).toHaveCount(0);
  expect(state.documents['second-chat'].draft_version).toBe(8);
});

for (const hasChat of [false, true]) {
  test(`switching from a chat to a ${hasChat ? 'populated' : 'empty'} matter keeps the selected matter`, async ({ page }) => {
    await mockMatterSwitchingApis(page, hasChat);

    await page.goto('/case-assistant/e2e-case-assistant');
    await expect(page.getByRole('heading', { name: 'Three Document Task', exact: true })).toBeVisible();
    await expect(page.getByTestId('composer-attachment')).toHaveCount(1);
    const matterLoaded = page.waitForResponse(response => response.url().endsWith('/api/list-drafts')
      && response.request().postDataJSON()['proj-id'] === 2);
    await page.getByText('Second Matter', { exact: true }).click();
    await matterLoaded;
    await expect(page).toHaveURL('/');
    await expect(page.getByRole('heading', { name: 'Second Matter', exact: true })).toBeVisible();
    await expect(page.getByTestId('composer-attachment')).toHaveCount(0);
    await expect(page.getByRole('heading', { name: 'Three Document Task', exact: true })).toHaveCount(0);
    if (hasChat) {
      await page.getByRole('listitem', { name: 'Double-click to edit name', exact: true })
        .getByText('Second chat', { exact: true }).click();
      await expect(page).toHaveURL('/case-assistant/second-chat');
      await expect(page.getByRole('heading', { name: 'Second chat', exact: true })).toBeVisible();
    }
    await page.reload();
    await expect(page.getByRole('heading', { name: hasChat ? 'Second chat' : 'Second Matter', exact: true })).toBeVisible();
    await expect(page.getByTestId('composer-attachment')).toHaveCount(0);
  });
}

test('a late chat metadata response cannot undo an explicit matter switch', async ({ page }) => {
  await mockMatterSwitchingApis(page);
  await page.route('**/api/list-drafts', route => {
    if (route.request().postDataJSON()['proj-id'] !== 1) return route.fallback();
    return fulfillJson(route, { drafts: [] });
  });
  let releaseMetadata, metadataStarted;
  const gate = new Promise(resolve => { releaseMetadata = resolve; });
  const pending = new Promise(resolve => { metadataStarted = resolve; });
  await page.route('**/api/draft-name/e2e-case-assistant', async route => {
    metadataStarted();
    await gate;
    return fulfillJson(route, {
      draftId: 'e2e-case-assistant', draftName: 'Three Document Task', projectId: 1, docCategory: 69,
    });
  });
  await page.goto('/case-assistant/e2e-case-assistant');
  await pending;
  await page.getByText('Second Matter', { exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Second Matter', exact: true })).toBeVisible();
  const oldResponse = page.waitForResponse(response => response.url().endsWith('/api/draft-name/e2e-case-assistant'));
  releaseMetadata();
  await oldResponse;
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await expect(page).toHaveURL('/');
  await expect(page.getByRole('heading', { name: 'Second Matter', exact: true })).toBeVisible();
  await expect(page.getByTestId('composer-attachment')).toHaveCount(0);
});

test('switching matters in one tab preserves the other tab chat and attachments', async ({ page, context }) => {
  await mockMatterSwitchingApis(page);
  await page.goto('/case-assistant/e2e-case-assistant');
  await expect(page.getByTestId('composer-attachment')).toHaveCount(1);
  const otherTab = await context.newPage();
  await mockMatterSwitchingApis(otherTab);
  await otherTab.goto('/case-assistant/e2e-case-assistant');
  await expect(otherTab.getByTestId('composer-attachment')).toHaveCount(1);

  await page.getByText('Second Matter', { exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Second Matter', exact: true })).toBeVisible();
  await expect(page.getByTestId('composer-attachment')).toHaveCount(0);
  await expect(otherTab).toHaveURL('/case-assistant/e2e-case-assistant');
  await expect(otherTab.getByRole('heading', { name: 'Three Document Task', exact: true })).toBeVisible();
  await expect(otherTab.getByTestId('composer-attachment')).toHaveAttribute('title', 'A-only.txt');
});

test('runs Case Assistant with three attached documents through confirmation and Word download', async ({ page }) => {
  const state = {
    completed: false,
    promptBody: null,
    searchBody: null,
    confirmationBody: null,
    downloadBody: null,
  };
  await mockCaseAssistantApis(page, state);

  await page.addInitScript(() => {
    localStorage.setItem('videoWelcomeSeen_v2.0', 'true');
    localStorage.setItem('li_selectedProject', '1');
    localStorage.setItem('li_selectedDraft', 'e2e-case-assistant');
  });

  await page.goto('/case-assistant/e2e-case-assistant');
  await expect(page.locator('textarea.chat-input')).toBeVisible();
  await expect(page.locator('#assistant-mode-select')).toHaveValue('deep');
  await page.locator('#assistant-mode-select').selectOption('fast');

  const files = [
    {
      name: 'opposition.pdf',
      mimeType: 'application/pdf',
      buffer: Buffer.from('Opposition fixture'),
    },
    {
      name: 'motion-to-dismiss.pdf',
      mimeType: 'application/pdf',
      buffer: Buffer.from('Motion fixture'),
    },
    {
      name: 'declaration.pdf',
      mimeType: 'application/pdf',
      buffer: Buffer.from('Declaration fixture'),
    },
  ];
  await page.locator('input[type="file"][accept=".pdf,.doc,.docx,.txt,.xlsx,.csv"]').setInputFiles(files);

  const prompt = 'Draft a reply to the Opposition to the Motion to Dismiss';
  await page.locator('textarea.chat-input').fill(prompt);
  for (const file of files) {
    await expect(page.getByText(file.name, { exact: true })).toBeVisible();
  }
  await expect(page.getByRole('button', { name: 'Send' })).toBeEnabled();

  await page.getByRole('button', { name: 'Send' }).click();

  await expect(page.getByLabel('Workflow question')).toBeVisible();
  await expect(page.getByText('How should this reply be handled?')).toBeVisible();
  await page.getByRole('button', { name: 'Draft a new reply' }).click();
  await page.getByRole('button', { name: 'Continue' }).click();

  await expect(page.locator('.draft-section').filter({ hasText: 'Introduction' })).toBeVisible();
  await expect(page.locator('.draft-section').filter({ hasText: 'Argument' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Download draft as Word document' })).toBeVisible();

  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Download draft as Word document' }).click();
  const download = await downloadPromise;

  expect(download.suggestedFilename()).toBe('Three_Document_Task.docx');
  expect(state.promptBody.query).toBe(prompt);
  expect(state.promptBody.assistant_mode).toBe('fast');
  expect(state.promptBody).not.toHaveProperty('model');
  expect(state.promptBody.chatFiles.map((file) => file.name)).toEqual([
    'file-1',
    'file-2',
    'file-3',
  ]);
  expect(state.promptBody.lastNewChatFileIds).toEqual(['file-1', 'file-2', 'file-3']);
  expect(state.promptBody.promptAttachments).toEqual([
    { file_id: 'file-1', file_name: 'opposition.pdf' },
    { file_id: 'file-2', file_name: 'motion-to-dismiss.pdf' },
    { file_id: 'file-3', file_name: 'declaration.pdf' },
  ]);
  expect(state.confirmationBody).toMatchObject({
    draftId: 'e2e-case-assistant',
    workflow_run_id: 'workflow-1',
    decision: {
      draft_action: 'new',
      chosen_label: 'Draft a new reply',
    },
  });
  expect(state.downloadBody.sectionOrder).toEqual(['Introduction', 'Argument']);

  await page.locator('.product-nav-item').filter({ hasText: 'Doc Search' }).click();
  await expect(page.getByRole('heading', { name: 'Doc Search' })).toBeVisible();
  await expect(page.locator('#assistant-mode-select')).toHaveCount(0);

  await page.locator('textarea.chat-input').fill('Find the termination notices.');
  await page.getByRole('button', { name: 'Search', exact: true }).click();
  await expect.poll(() => state.searchBody).not.toBeNull();
  expect(state.searchBody).not.toHaveProperty('model');
  expect(state.searchBody).not.toHaveProperty('assistant_mode');
});

test('unnamed draft bodies stay visible after reload while named sections still collapse', async ({ page }) => {
  await mockCaseAssistantApis(page, { completed: true });
  await page.route('**/api/get_document', route => fulfillJson(route, {
    chat_thread_id: 'e2e-case-assistant', draft_document_id: 'document-e2e-case-assistant',
    draft_version: 1,
    draft: { title: 'Order status.', sections: [
      { section_id: 'body', section_name: '', section_content: 'Agreed terms remain visible.' },
      { section_id: 'blank', section_name: '   ', section_content: 'Whitespace-only headings also stay visible.' },
      { section_id: 'notes', section_name: 'Named notes', section_content: 'Named sections retain their disclosure control.' },
    ] },
    chatHistory: [], chatHistoryMeta: [],
  }));
  await page.addInitScript(() => {
    localStorage.setItem('videoWelcomeSeen_v2.0', 'true');
    localStorage.setItem('li_selectedProject', '1');
    localStorage.setItem('li_selectedDraft', 'e2e-case-assistant');
  });
  await page.goto('/case-assistant/e2e-case-assistant');

  for (const afterReload of [false, true]) {
    if (afterReload) await page.reload();
    const body = page.locator('div.draft-section').filter({ hasText: 'Agreed terms remain visible.' });
    const whitespaceBody = page.locator('div.draft-section').filter({ hasText: 'Whitespace-only headings also stay visible.' });
    await expect(body.getByText('Agreed terms remain visible.', { exact: true })).toBeVisible();
    await expect(whitespaceBody.getByText('Whitespace-only headings also stay visible.', { exact: true })).toBeVisible();
    await expect(body.locator('summary')).toHaveCount(0);
    await expect(whitespaceBody.locator('summary')).toHaveCount(0);
    await expect(body.getByRole('button', { name: 'Copy Section', exact: true })).toBeVisible();

    const named = page.locator('details.draft-section').filter({ hasText: 'Named notes' });
    const namedText = named.getByText('Named sections retain their disclosure control.', { exact: true });
    await expect(namedText).toBeHidden();
    await named.locator('summary').click();
    await expect(namedText).toBeVisible();
    await named.locator('summary').click();
    await expect(namedText).toBeHidden();
    await expect(body.getByText('Agreed terms remain visible.', { exact: true })).toBeVisible();

    await body.getByRole('button', { name: 'Manual edit', exact: true }).click();
    await expect(body.locator('[contenteditable="true"]')).toBeVisible();
    await expect(body.locator('[contenteditable="true"]')).toHaveText('Agreed terms remain visible.');
    await body.getByRole('button', { name: 'Discard changes', exact: true }).click();
    await expect(body.getByText('Agreed terms remain visible.', { exact: true })).toBeVisible();
  }
});

test('agent candidates remain staged and free-text clarification survives reload', async ({ page }) => {
  const state = { durableWorkflow: true, completed: false, askQuestion: false };
  await mockCaseAssistantApis(page, state);
  const question = { kind: 'agent_question', message: 'Which source should the summary use?', options: [] };
  const progress = { execution_version: 1, steps: [
    { id: 'draft-step', operation: 'create_draft', status: 'ready', result: { artifact_id: 'candidate' } },
  ], current_operation: 'choose_next_action' };
  await page.route('**/api/everything/workflows/**', async (route) => {
    if (route.request().url().endsWith('/artifacts/candidate')) return fulfillJson(route, {
      id: 'candidate', kind: 'draft', content: { draft: { title: 'Candidate summary', sections: [
        { section_id: '1', section_name: 'Facts', section_content: 'Staged candidate text.' },
      ] } },
    });
    return fulfillJson(route, { workflow_run_id: state.workflowId, ready: state.askQuestion || state.completed,
      status: state.completed ? 'completed' : state.askQuestion ? 'awaiting_user_confirmation' : 'running',
      info: { status: state.completed ? 'Finished' : 'Choosing next action', agent: progress },
      value: state.completed ? { response: 'Published the requested summary.' } : state.askQuestion
        ? { awaiting_confirmation: true, workflow_run_id: state.workflowId, confirmation: question } : null });
  });
  await page.route('**/api/get_document', (route) => {
    const data = documentState(false);
    if (state.completed) {
      data.draft = { title: 'Final summary', sections: [{ section_id: '1', section_name: 'Facts', section_content: 'Final source B summary.' }] };
      data.chatHistory = [[state.promptBody.query, 'Published the requested summary.']];
      data.chatHistoryMeta = [{ workflow_run_id: state.workflowId, agent_run: true }];
    } else if (state.workflowId) {
      data.activeWorkflow = { workflow_run_id: state.workflowId, status: state.askQuestion ? 'awaiting_user_confirmation' : 'running' };
      data.pendingTurn = { input: state.promptBody.query, response: state.askQuestion ? question.message : '',
        draftId: 'e2e-case-assistant', workflowRunId: state.workflowId, agentRun: true,
        awaitingConfirmation: state.askQuestion, confirmation: state.askQuestion ? question : undefined, promptAttachments: [] };
    }
    return fulfillJson(route, data);
  });
  await page.route('**/api/everything/confirm-workflow', (route) => {
    state.confirmationBody = route.request().postDataJSON();
    state.completed = true;
    return fulfillJson(route, { result_id: state.workflowId });
  });
  await page.addInitScript(() => {
    localStorage.setItem('videoWelcomeSeen_v2.0', 'true');
    localStorage.setItem('li_selectedProject', '1');
    localStorage.setItem('li_selectedDraft', 'e2e-case-assistant');
  });
  await page.goto('/case-assistant/e2e-case-assistant');
  await page.locator('textarea.chat-input').fill('Compare the sources and draft a summary.');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await page.getByText('Steps and saved results', { exact: true }).click();
  await page.getByRole('button', { name: 'View saved result', exact: true }).click();
  await expect(page.getByText('Staged candidate text.', { exact: true })).toBeVisible();
  await expect(page.getByText('Final source B summary.', { exact: true })).toHaveCount(0);
  state.askQuestion = true;
  await expect(page.getByLabel('Your answer')).toBeVisible();
  await page.reload();
  await expect(page.getByLabel('Your answer')).toBeVisible();
  await page.getByLabel('Your answer').fill('Use only source B.');
  await page.getByRole('button', { name: 'Continue', exact: true }).click();
  await expect(page.getByText('Published the requested summary.', { exact: true })).toBeVisible();
  await page.locator('.draft-section').getByRole('heading', { name: 'Facts', exact: true }).click();
  await expect(page.getByText('Final source B summary.', { exact: true })).toBeVisible();
  expect(state.confirmationBody.decision.answer).toBe('Use only source B.');
  expect(state.promptCount).toBe(1);
});

test('saved workflow restores extraction progress, survives connection errors and exposes source evidence', async ({ page }) => {
  const state = { durableWorkflow: true, completed: false, admissionResponseLost: true };
  await mockCaseAssistantApis(page, state);
  await page.addInitScript(() => {
    localStorage.setItem('videoWelcomeSeen_v2.0', 'true');
    localStorage.setItem('li_selectedProject', '1');
    localStorage.setItem('li_selectedDraft', 'e2e-case-assistant');
  });
  await page.goto('/case-assistant/e2e-case-assistant');
  await page.locator('textarea.chat-input').fill('Find every deadline in the attachments.');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect(page.getByText('Extracting passages: 22 of 34 sections completed', { exact: false })).toBeVisible();
  expect(state.workflowId).toMatch(/^wf_/);
  expect(state.promptCount).toBe(1);
  await page.reload();
  await expect(page.getByText('Extracting passages: 22 of 34 sections completed', { exact: false })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Stop', exact: true })).toBeVisible();
  state.interruptProgress = true;
  await expect(page.getByText('Reconnecting to request progress…')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Stop', exact: true })).toBeVisible();
  state.cancelUnavailable = true;
  await page.getByRole('button', { name: 'Stop', exact: true }).click();
  await expect(page.getByText('Could not cancel the request. Please try Stop again.')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Stop', exact: true })).toBeVisible();
  state.workflowFinished = true;
  await expect(page.getByRole('button', { name: /Full extracted evidence/ })).toBeVisible();
  await page.getByRole('button', { name: /Full extracted evidence/ }).click();
  await expect(page.getByText('The extension was rejected on July 12.')).toBeVisible();
  await expect(page.getByText('Page 4', { exact: true })).toBeVisible();
  expect(state.promptCount).toBe(1);
});

for (const outcome of ['cancelled', 'completed', 'failed']) {
  test(`Stop displays the saved ${outcome} outcome instead of assuming cancellation`, async ({ page }) => {
    const state = { durableWorkflow: true, completed: false,
      completeOnCancel: outcome === 'completed', failOnCancel: outcome === 'failed' };
    await mockCaseAssistantApis(page, state);
    await page.addInitScript(() => {
      localStorage.setItem('videoWelcomeSeen_v2.0', 'true');
      localStorage.setItem('li_selectedProject', '1');
      localStorage.setItem('li_selectedDraft', 'e2e-case-assistant');
    });
    await page.goto('/case-assistant/e2e-case-assistant');
    await page.locator('textarea.chat-input').fill('Summarize the agreement.');
    await page.getByRole('button', { name: 'Send', exact: true }).click();
    await expect(page.getByText('Extracting passages: 22 of 34 sections completed', { exact: false })).toBeVisible();
    await page.getByRole('button', { name: 'Stop', exact: true }).click();
    if (outcome === 'completed') {
      await expect(page.getByText('Drafted a reply based on the three attached documents.', { exact: true })).toHaveCount(1);
      await page.locator('.draft-section').getByRole('heading', { name: 'Introduction', exact: true }).click();
      await expect(page.getByText('A reply grounded in the attached documents.', { exact: true })).toBeVisible();
    } else if (outcome === 'failed') {
      await expect(page.getByText('A document section failed. Retry to resume saved work.', { exact: true })).toBeVisible();
      await expect(page.getByRole('button', { name: 'Retry', exact: true })).toBeVisible();
    } else {
      await expect(page.getByText('Request cancelled.', { exact: true })).toBeVisible();
    }
    if (outcome !== 'cancelled') await expect(page.getByText('Request cancelled.', { exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Stop', exact: true })).toHaveCount(0);
    expect(state.cancelCount).toBe(1);
    expect(state.promptCount).toBe(1);
  });
}

for (const completed of [false, true]) {
  test(`confirmation Cancel ${completed ? 'restores already-published completion' : 'records actual cancellation'}`, async ({ page }) => {
    const state = { durableWorkflow: true, completed: false, awaitingQuestion: true, completeOnConfirmationCancel: completed };
    await mockCaseAssistantApis(page, state);
    await page.addInitScript(() => {
      localStorage.setItem('videoWelcomeSeen_v2.0', 'true');
      localStorage.setItem('li_selectedProject', '1');
      localStorage.setItem('li_selectedDraft', 'e2e-case-assistant');
    });
    await page.goto('/case-assistant/e2e-case-assistant');
    await page.locator('textarea.chat-input').fill('Summarize the agreement.');
    await page.getByRole('button', { name: 'Send', exact: true }).click();
    await expect(page.getByLabel('Your answer')).toBeVisible();
    await page.getByRole('button', { name: 'Cancel', exact: true }).click();
    if (completed) {
      await expect(page.getByText('Drafted a reply based on the three attached documents.', { exact: true })).toHaveCount(1);
      await expect(page.getByText('No draft changes made.', { exact: true })).toHaveCount(0);
      await page.locator('.draft-section').getByRole('heading', { name: 'Introduction', exact: true }).click();
      await expect(page.getByText('A reply grounded in the attached documents.', { exact: true })).toBeVisible();
    } else {
      await expect(page.getByText('Request cancelled.', { exact: true })).toBeVisible();
      await expect(page.getByRole('button', { name: 'Retry', exact: true })).toBeEnabled();
    }
    await expect(page.getByLabel('Your answer')).toHaveCount(0);
    expect(state.confirmationCancelCount).toBe(1);
    expect(state.promptCount).toBe(1);
  });
}

test('a failed confirmation cancellation preserves the question and supports retry', async ({ page }) => {
  const state = { durableWorkflow: true, completed: false, awaitingQuestion: true, confirmationCancelUnavailable: true };
  await mockCaseAssistantApis(page, state);
  await page.addInitScript(() => {
    localStorage.setItem('videoWelcomeSeen_v2.0', 'true');
    localStorage.setItem('li_selectedProject', '1');
    localStorage.setItem('li_selectedDraft', 'e2e-case-assistant');
  });
  await page.goto('/case-assistant/e2e-case-assistant');
  await page.locator('textarea.chat-input').fill('Summarize the agreement.');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect(page.getByLabel('Your answer')).toBeVisible();
  await page.getByLabel('Your answer').fill('Use Exhibit B.');
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(page.getByText('Could not cancel the request. Please try again.', { exact: true })).toBeVisible();
  await expect(page.getByLabel('Your answer')).toHaveValue('Use Exhibit B.');
  await expect(page.getByRole('button', { name: 'Cancel', exact: true })).toBeEnabled();
  await expect(page.getByText('No draft changes made.', { exact: true })).toHaveCount(0);
  state.confirmationCancelUnavailable = false;
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(page.getByText('Request cancelled.', { exact: true })).toBeVisible();
  await expect(page.getByLabel('Your answer')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Retry', exact: true })).toBeEnabled();
  expect(state.promptCount).toBe(1);
});

test('a delayed Stop response cannot cancel a newer assignment in the same chat', async ({ page }) => {
  const state = { durableWorkflow: true, completed: false };
  await mockCaseAssistantApis(page, state);
  let releaseCancellation, cancellationStarted;
  const gate = new Promise(resolve => { releaseCancellation = resolve; });
  const started = new Promise(resolve => { cancellationStarted = resolve; });
  await page.route('**/api/cancel/wf_*', async route => {
    const workflowId = route.request().url().split('/').pop();
    cancellationStarted();
    await gate;
    return fulfillJson(route, { workflow_run_id: workflowId, status: 'cancelled', cancelled: true });
  });
  await page.addInitScript(() => {
    localStorage.setItem('videoWelcomeSeen_v2.0', 'true');
    localStorage.setItem('li_selectedProject', '1');
    localStorage.setItem('li_selectedDraft', 'e2e-case-assistant');
  });
  await page.goto('/case-assistant/e2e-case-assistant');
  await page.locator('textarea.chat-input').fill('First assignment.');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await page.getByRole('button', { name: 'Stop', exact: true }).click();
  await started;
  const firstId = state.workflowId;
  state.workflowFinished = true;
  await expect(page.getByText('Drafted a reply based on the three attached documents.', { exact: true })).toBeVisible();
  state.workflowFinished = false;
  state.completed = false;
  await page.locator('textarea.chat-input').fill('Second assignment.');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect.poll(() => state.promptCount).toBe(2);
  expect(state.workflowId).not.toBe(firstId);
  releaseCancellation();
  state.completedResponse = 'Second assignment completed.';
  state.workflowFinished = true;
  await expect(page.getByText('Second assignment completed.', { exact: true })).toBeVisible();
  await expect(page.getByText('Request cancelled.', { exact: true })).toHaveCount(0);
});

test('retry after reload resumes the same submitted workflow without posting another prompt', async ({ page }) => {
  const state = { durableWorkflow: true, workflowFailed: true, completed: false };
  await mockCaseAssistantApis(page, state);
  await page.addInitScript(() => {
    localStorage.setItem('videoWelcomeSeen_v2.0', 'true');
    localStorage.setItem('li_selectedProject', '1');
    localStorage.setItem('li_selectedDraft', 'e2e-case-assistant');
  });
  await page.goto('/case-assistant/e2e-case-assistant');
  await page.locator('textarea.chat-input').fill('Find every deadline in the attachments.');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect(page.getByText('A document section failed. Retry to resume saved work.', { exact: true })).toHaveCount(1);
  await page.reload();
  await expect(page.getByText('A document section failed. Retry to resume saved work.', { exact: true })).toHaveCount(1);
  await expect(page.getByRole('button', { name: 'Retry', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Retry', exact: true }).click();
  await expect(page.getByText('Extracting passages: 22 of 34 sections completed', { exact: false })).toBeVisible();
  expect(state.retryWorkflowId).toBe(state.workflowId);
  expect(state.promptCount).toBe(1);
  state.workflowFinished = true;
  await expect(page.getByRole('button', { name: /Full extracted evidence/ })).toBeVisible();
});

test('a historical failure renders once before a later answer after reload', async ({ page }) => {
  const state = { completed: true };
  await mockCaseAssistantApis(page, state);
  await page.route('**/api/get_document', route => fulfillJson(route, {
    ...documentState(true),
    chatHistory: [['Earlier request', 'Earlier request failed.'], ['Later request', 'Later request succeeded.']],
    chatHistoryMeta: [{ workflow_run_id: 'wf_failed', workflow_failed: true },
      { workflow_run_id: 'wf_completed', workflow_completed: true }],
  }));
  await page.goto('/case-assistant/e2e-case-assistant');
  for (let reload = 0; reload < 2; reload += 1) {
    await expect(page.getByText('Earlier request failed.', { exact: true })).toHaveCount(1);
    await expect(page.getByText('Later request succeeded.', { exact: true })).toHaveCount(1);
    const turns = page.locator('.result-item');
    await expect(turns).toHaveCount(2);
    await expect(turns.nth(0)).toContainText('Earlier request failed.');
    await expect(turns.nth(0).getByRole('button', { name: 'Retry', exact: true })).toBeVisible();
    await expect(turns.nth(1)).toContainText('Later request succeeded.');
    await expect(turns.nth(1).getByRole('button', { name: 'Retry', exact: true })).toHaveCount(0);
    if (reload === 0) await page.reload();
  }
  expect(state.promptCount || 0).toBe(0);
});

test('13 staggered uploads keep 13 chips and wait for summaries before turning blue', async ({ page }) => {
  const state = { completed: false, completedUploadCount: 1, holdSummaries: true, summariesReady: false };
  const duplicateKeyErrors = [];
  page.on('console', message => {
    if (message.text().includes('same key')) duplicateKeyErrors.push(message.text());
  });
  await mockCaseAssistantApis(page, state);
  await page.addInitScript(() => {
    localStorage.setItem('videoWelcomeSeen_v2.0', 'true');
    localStorage.setItem('li_selectedProject', '1');
    localStorage.setItem('li_selectedDraft', 'e2e-case-assistant');
  });
  await page.goto('/case-assistant/e2e-case-assistant');
  await page.locator('input[type="file"][accept=".pdf,.doc,.docx,.txt,.xlsx,.csv"]').setInputFiles(
    Array.from({ length: 13 }, (_, index) => ({
      name: index < 2 ? 'same-name.txt' : `attachment-${index}.txt`,
      mimeType: 'text/plain', buffer: Buffer.from(`Document ${index}`),
    })));
  await expect.poll(() => state.listDuringUploads).toBe(true);
  const chips = page.getByTestId('composer-attachment');
  await expect(chips).toHaveCount(13);
  await expect(page.getByRole('button', { name: /Add Task-Specific Docs/ })).toContainText('(13/100)');
  await expect(chips.filter({ hasText: 'same-name.txt' })).toHaveCount(2);
  await expect(page.locator('[data-testid="composer-attachment"][data-status="ready"]')).toHaveCount(0);
  state.completedUploadCount = 13;
  await expect.poll(() => state.completedUploads?.size).toBe(13);
  await expect(page.locator('[data-testid="composer-attachment"][data-status="summarizing"]')).toHaveCount(13);
  await expect(chips).toHaveCount(13);
  await page.locator('textarea.chat-input').fill('Summarize these documents.');
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeDisabled();
  state.summariesReady = true;
  await expect(page.locator('[data-testid="composer-attachment"][data-status="ready"]')).toHaveCount(13);
  await expect(chips.first()).toHaveCSS('color', 'rgb(24, 97, 221)');
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeEnabled();
  await page.reload();
  await expect(chips).toHaveCount(13);
  await expect(page.getByRole('button', { name: /Add Task-Specific Docs/ })).toContainText('(13/100)');
  await expect(page.locator('[data-testid="composer-attachment"][data-status="ready"]')).toHaveCount(13);
  expect(await chips.evaluateAll(elements => new Set(elements.map(element => element.dataset.documentId)).size)).toBe(13);
  await page.locator('[data-document-id="file-1"]').getByRole('button', { name: 'Remove same-name.txt from prompt', exact: true }).click();
  await expect.poll(() => state.deletedDocumentId).toBe('file-1');
  await expect(chips).toHaveCount(12);
  await expect(chips.filter({ hasText: 'same-name.txt' })).toHaveCount(1);
  expect(duplicateKeyErrors).toEqual([]);
});

test('reload restores 13 saved uploads even without a previous browser selection', async ({ page }) => {
  const state = { completed: false, savedFiles: Array.from({ length: 13 }, (_, index) => ({
    unique_id: `saved-${index}`, filename: `saved-${index}.txt`, file_size: 4, metadata: { status: 'ready' },
  })) };
  await mockCaseAssistantApis(page, state);
  await page.addInitScript(() => {
    localStorage.setItem('videoWelcomeSeen_v2.0', 'true');
    localStorage.setItem('li_selectedProject', '1');
    localStorage.setItem('li_selectedDraft', 'e2e-case-assistant');
  });
  await page.goto('/case-assistant/e2e-case-assistant');
  await expect(page.getByTestId('composer-attachment')).toHaveCount(13);
  await page.reload();
  await expect(page.getByTestId('composer-attachment')).toHaveCount(13);
  await expect(page.getByRole('button', { name: /Add Task-Specific Docs/ })).toContainText('(13/100)');
  await page.locator('[data-document-id="saved-0"]').getByRole('button', { name: 'Remove saved-0.txt from prompt', exact: true }).click();
  await expect.poll(() => state.deletedDocumentId).toBe('saved-0');
  await page.reload();
  await expect(page.getByTestId('composer-attachment')).toHaveCount(12);
  await expect(page.getByRole('button', { name: /Add Task-Specific Docs/ })).toContainText('(12/100)');
  expect(state.uploadedCount || 0).toBe(0);
  await page.locator('textarea.chat-input').fill('Summarize the restored uploads.');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect.poll(() => state.promptBody?.promptAttachments.length).toBe(12);
  expect(state.promptBody.chatFiles).toHaveLength(12);
  expect(state.promptBody.promptAttachments.some(file => file.file_id === 'saved-0')).toBe(false);
});

test('reopening a chat restores only uploads not used in its earlier prompts', async ({ page }) => {
  const state = { completed: true, savedFiles: Array.from({ length: 4 }, (_, index) => ({
    unique_id: `file-${index + 1}`, filename: `saved-${index + 1}.txt`, file_size: 4, metadata: { status: 'ready' },
  })) };
  await mockCaseAssistantApis(page, state);
  await page.addInitScript(() => {
    localStorage.setItem('videoWelcomeSeen_v2.0', 'true');
    localStorage.setItem('li_selectedProject', '1');
    localStorage.setItem('li_selectedDraft', 'e2e-case-assistant');
  });
  await page.goto('/case-assistant/e2e-case-assistant');
  await expect(page.getByTestId('composer-attachment')).toHaveCount(1);
  await expect(page.getByTestId('composer-attachment')).toHaveAttribute('data-document-id', 'file-4');
  await expect(page.getByRole('button', { name: /Add Task-Specific Docs/ })).toContainText('(4/100)');
  await page.reload();
  await expect(page.getByTestId('composer-attachment')).toHaveCount(1);
  await page.locator('textarea.chat-input').fill('Review the additional upload.');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect.poll(() => state.promptBody?.promptAttachments.map(file => file.file_id)).toEqual(['file-4']);
  expect(state.promptBody.chatFiles.map(file => file.name).sort()).toEqual(['file-1', 'file-2', 'file-3', 'file-4']);
  await expect(page.getByTestId('composer-attachment')).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole('button', { name: /Add Task-Specific Docs/ })).toContainText('(4/100)');
  await expect(page.getByTestId('composer-attachment')).toHaveCount(0);
});

test('active upload flow accepts 100 attachments and rejects a selection of 101', async ({ page }) => {
  const state = { completed: false, uploadedCount: 0 };
  await mockCaseAssistantApis(page, state);
  await page.addInitScript(() => {
    localStorage.setItem('videoWelcomeSeen_v2.0', 'true');
    localStorage.setItem('li_selectedProject', '1');
    localStorage.setItem('li_selectedDraft', 'e2e-case-assistant');
  });
  await page.goto('/case-assistant/e2e-case-assistant');
  const input = page.locator('input[type="file"][accept=".pdf,.doc,.docx,.txt,.xlsx,.csv"]');
  const files = Array.from({ length: 101 }, (_, index) => ({
    name: `attachment-${index}.txt`, mimeType: 'text/plain', buffer: Buffer.from('Document fixture'),
  }));
  await input.setInputFiles(files);
  await expect(page.getByText(/This task allows 100 files/)).toBeVisible();
  expect(state.uploadedCount).toBe(0);
  await input.setInputFiles(files.slice(0, 100));
  await expect.poll(() => state.uploadedCount).toBe(100);
  await expect(page.getByRole('button', { name: /Add Task-Specific Docs/ })).toContainText('(100/100)');
  await expect(page.getByRole('button', { name: /Add Task-Specific Docs/ })).toBeDisabled();
});

test('groups uploads, restores preparation after reload and waits for summaries before Send', async ({ page }) => {
  const state = { completed: false, durablePreparation: true, preparationReady: false, promptBody: null };
  await mockCaseAssistantApis(page, state);
  await page.addInitScript(() => {
    localStorage.setItem('videoWelcomeSeen_v2.0', 'true');
    localStorage.setItem('li_selectedProject', '1');
    localStorage.setItem('li_selectedDraft', 'e2e-case-assistant');
  });
  await page.goto('/case-assistant/e2e-case-assistant');
  await page.locator('input[type="file"][accept=".pdf,.doc,.docx,.txt,.xlsx,.csv"]').setInputFiles([
    { name: 'opposition.pdf', mimeType: 'application/pdf', buffer: Buffer.from('Opposition fixture') },
    { name: 'motion-to-dismiss.pdf', mimeType: 'application/pdf', buffer: Buffer.from('Motion fixture') },
    { name: 'declaration.pdf', mimeType: 'application/pdf', buffer: Buffer.from('Declaration fixture') },
  ]);
  await expect.poll(() => state.transferGroups).toBe(2);
  await expect(page.getByRole('button', { name: /Add Task-Specific Docs/ })).toContainText('(3/100)');
  await page.locator('textarea.chat-input').fill('Summarize the attached documents.');
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeDisabled();
  await expect(page.locator('[data-testid="composer-attachment"][data-status="summarizing"]')).toHaveCount(3);
  await page.reload();
  await expect(page.locator('[data-testid="composer-attachment"][data-status="summarizing"]')).toHaveCount(3);
  await page.locator('textarea.chat-input').fill('Summarize the attached documents.');
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeDisabled();
  state.preparationReady = true;
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeEnabled({ timeout: 15000 });
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect.poll(() => state.promptBody).not.toBeNull();
  expect(state.promptBody.chatFiles).toHaveLength(3);
  expect(state.promptBody.promptAttachments).toHaveLength(3);
});

test('keeps Fast and Deep for Case Assistant but not Doc Search', async ({ page }) => {
  const state = {
    completed: false,
    promptBody: null,
    searchBody: null,
    confirmationBody: null,
    downloadBody: null,
  };
  await mockCaseAssistantApis(page, state);

  await page.addInitScript(() => {
    localStorage.setItem('videoWelcomeSeen_v2.0', 'true');
    localStorage.setItem('li_selectedProject', '1');
    localStorage.setItem('li_selectedDraft', 'e2e-case-assistant');
  });

  await page.goto('/case-assistant/e2e-case-assistant');
  await expect(page.locator('#assistant-mode-select')).toHaveValue('deep');
  await expect(page.locator('#assistant-mode-select option')).toHaveText(['Fast', 'Deep']);
  await page.locator('textarea.chat-input').fill('Summarize the case.');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect.poll(() => state.promptBody?.assistant_mode).toBe('deep');
  expect(state.promptBody).not.toHaveProperty('model');

  await page.locator('.product-nav-item').filter({ hasText: 'Doc Search' }).click();
  await expect(page.locator('#assistant-mode-select')).toHaveCount(0);
  await page.locator('textarea.chat-input').fill('Find the pleadings.');
  await page.getByRole('button', { name: 'Search', exact: true }).click();
  await expect.poll(() => state.searchBody).not.toBeNull();
  expect(state.searchBody).not.toHaveProperty('model');
  expect(state.searchBody).not.toHaveProperty('assistant_mode');
});

test('keeps polling beyond 200 checks and displays the server timeout', async ({ page }) => {
  const state = {
    completed: false,
    keepFirstTaskPending: true,
    pendingPollCount: 0,
    returnTaskTimeout: false,
  };
  await mockCaseAssistantApis(page, state);

  await page.addInitScript(() => {
    localStorage.setItem('videoWelcomeSeen_v2.0', 'true');
    localStorage.setItem('li_selectedProject', '1');
    localStorage.setItem('li_selectedDraft', 'e2e-case-assistant');
    // Accelerate the 3-second Case Assistant polling interval without changing app code.
    const nativeSetInterval = window.setInterval.bind(window);
    window.setInterval = (callback, delay, ...args) => (
      nativeSetInterval(callback, delay === 3000 ? 5 : delay, ...args)
    );
  });

  await page.goto('/case-assistant/e2e-case-assistant');
  await page.locator('textarea.chat-input').fill('Summarize the case.');
  await page.getByRole('button', { name: 'Send' }).click();

  await expect.poll(() => state.pendingPollCount, { timeout: 30000 }).toBeGreaterThan(200);
  await expect(page.getByRole('button', { name: 'Stop', exact: true })).toBeVisible();
  await expect(page.getByText('Request timed out.', { exact: false })).toHaveCount(0);

  state.returnTaskTimeout = true;
  await expect(page.getByText('Case Assistant exceeded its processing time limit.').first()).toBeVisible();
  await expect(page.getByRole('button', { name: 'Stop', exact: true })).toHaveCount(0);
});

test('ready partial files allow sending across reloads without coverage approval', async ({ page }) => {
  const state = { completed: false, coverageGaps: true, coverageVersion: 1, promptBody: null };
  await mockCaseAssistantApis(page, state);
  await page.addInitScript(() => {
    localStorage.setItem('videoWelcomeSeen_v2.0', 'true');
    localStorage.setItem('li_selectedProject', '1');
    localStorage.setItem('li_selectedDraft', 'e2e-case-assistant');
  });
  await page.goto('/case-assistant/e2e-case-assistant');
  await page.locator('input[type="file"][accept=".pdf,.doc,.docx,.txt,.xlsx,.csv"]').setInputFiles({
    name: 'context.txt', mimeType: 'text/plain', buffer: Buffer.from('Document fixture'),
  });
  await page.locator('textarea.chat-input').fill('Summarize this document.');
  await expect(page.getByTestId('composer-attachment')).toHaveAttribute('data-status', 'ready');
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeEnabled();
  await expect(page.getByRole('button', { name: 'Use readable content', exact: true })).toHaveCount(0);
  await expect(page.getByRole('dialog', { name: 'Review files' })).toHaveCount(0);
  await page.reload();
  await page.locator('textarea.chat-input').fill('Summarize this document.');
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeEnabled();
  state.coverageVersion = 2;
  await page.reload();
  await page.locator('textarea.chat-input').fill('Summarize this document.');
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect.poll(() => state.promptCount).toBe(1);
  expect(state.promptBody.acceptedCoverageGaps).toBeUndefined();
});


test('excluding a failed attachment preserves it for the next prompt after preparation completes', async ({ page }) => {
  const state = { completed: false, promptBody: null, savedFiles: [
    { unique_id: 'file-1', filename: 'first.txt', file_size: 4 },
    { unique_id: 'file-2', filename: 'later.txt', file_size: 4 },
  ] };
  const backend = await mockCaseAssistantApis(page, state);
  const later = backend.preparation().documents[1];
  Object.assign(later, { status: 'failed', stage: 'failed', error: 'Preparation failed' });
  await page.route('**/api/get_document', route => {
    const data = documentState(state.completed);
    if (state.completed) data.chatHistoryMeta[0].prompt_attachments = state.promptBody?.promptAttachments || [];
    return fulfillJson(route, data);
  });
  await page.route('**/api/result/assistant-task-1', route => fulfillJson(route, {
    ready: true, successful: true, value: { response: 'Source read.', text: { title: '', sections: [] } },
  }));
  await page.addInitScript(() => {
    localStorage.setItem('videoWelcomeSeen_v2.0', 'true');
    localStorage.setItem('li_selectedProject', '1');
    localStorage.setItem('li_selectedDraft', 'e2e-case-assistant');
  });
  await page.goto('/case-assistant/e2e-case-assistant');
  await page.getByTestId('composer-attachment').getByRole('button', { name: 'later.txt (failed)', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Review files' });
  await dialog.getByRole('button', { name: 'Exclude from this prompt', exact: true }).click();
  await dialog.getByRole('button', { name: 'Close file issues', exact: true }).click();
  state.completed = true;
  await page.locator('textarea.chat-input').fill('Read the first source.');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect.poll(() => state.promptBody?.promptAttachments.map(file => file.file_id)).toEqual(['file-1']);
  await expect(page.getByTestId('composer-attachment')).toHaveCount(1);
  await expect(page.getByTestId('composer-attachment')).toHaveAttribute('data-document-id', 'file-2');
  Object.assign(later, { status: 'ready', stage: 'ready', error: null });
  await page.reload();
  await expect(page.getByTestId('composer-attachment')).toHaveCount(1);
  await page.locator('textarea.chat-input').fill('Now read the remaining source.');
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect.poll(() => state.promptBody?.promptAttachments.map(file => file.file_id)).toEqual(['file-2']);
});

for (const committed of [false, true]) {
  test(`attachment restoration blocks Send through failure and Retry (${committed ? 'committed' : 'unsubmitted'})`, async ({ page }) => {
    const state = { completed: committed, savedFiles: [{ unique_id: 'file-1', filename: 'opposition.pdf', file_size: 4 }] };
    await mockCaseAssistantApis(page, state);
    let release;
    const gate = new Promise(resolve => { release = resolve; });
    let first = true, failPreparation = false;
    await page.route('**/api/get-uploaded-files-everything', async route => {
      if (first) { first = false; await gate; failPreparation = true; }
      await route.fallback();
    });
    await page.route('**/api/everything/document-preparation?**', async route => {
      if (failPreparation) { failPreparation = false; return fulfillJson(route, { msg: 'Temporary failure' }, 503); }
      await route.fallback();
    });
    await page.addInitScript(() => {
      localStorage.setItem('videoWelcomeSeen_v2.0', 'true');
      localStorage.setItem('li_selectedProject', '1');
      localStorage.setItem('li_selectedDraft', 'e2e-case-assistant');
    });
    await page.goto('/case-assistant/e2e-case-assistant');
    await expect.poll(() => state.preparationPolls || 0).toBeGreaterThan(0);
    const input = page.locator('textarea.chat-input');
    const send = page.getByRole('button', { name: 'Send', exact: true });
    await input.fill('Read my saved attachment.');
    await expect(send).toBeDisabled();
    await input.press('Enter');
    expect(state.promptCount || 0).toBe(0);
    release();
    await expect(page.getByRole('alert').filter({ hasText: 'Could not restore this chat' })).toBeVisible();
    await expect(send).toBeDisabled();
    await input.press('Enter');
    expect(state.promptCount || 0).toBe(0);
    await expect(input).toHaveValue('Read my saved attachment.');
    await page.getByRole('button', { name: 'Retry restoring attachments' }).click();
    await expect(send).toBeEnabled();
    await expect(page.getByTestId('composer-attachment')).toHaveCount(committed ? 0 : 1);
    expect(state.promptCount || 0).toBe(0);
    await send.click();
    await expect.poll(() => state.promptBody?.chatFiles.map(file => file.name)).toEqual(['file-1']);
    expect(state.promptBody.promptAttachments.map(file => file.file_id)).toEqual(committed ? [] : ['file-1']);
  });
}

for (const failLate of [false, true]) {
  test(`late attachment restoration ${failLate ? 'failure' : 'success'} cannot change another chat`, async ({ page }) => {
    const state = { completed: false, savedFiles: [{ unique_id: 'file-1', filename: 'opposition.pdf', file_size: 4 }] };
    await mockCaseAssistantApis(page, state);
    let release, started;
    const gate = new Promise(resolve => { release = resolve; });
    const pending = new Promise(resolve => { started = resolve; });
    await page.route('**/api/get-uploaded-files-everything', async route => {
      if (route.request().postDataJSON().draftId === 'e2e-case-assistant') {
        started(); await gate;
        if (failLate) return fulfillJson(route, { msg: 'Old chat failure' }, 503);
        return route.fallback();
      }
      return fulfillJson(route, { uploaded_files: [] });
    });
    await page.route('**/api/draft-name/second-chat', route => fulfillJson(route, {
      draftId: 'second-chat', draftName: 'Second chat', projectId: 1, docCategory: 69,
    }));
    await page.route('**/api/everything/document-preparation?**', route => {
      if (new URL(route.request().url()).searchParams.get('draftId') !== 'second-chat') return route.fallback();
      return fulfillJson(route, { enabled: true, documents: [], policy: { chat_files: 100 }, revision: 'empty' });
    });
    await page.addInitScript(() => {
      localStorage.setItem('videoWelcomeSeen_v2.0', 'true');
      localStorage.setItem('li_selectedProject', '1');
      localStorage.setItem('li_selectedDraft', 'e2e-case-assistant');
    });
    await page.goto('/case-assistant/e2e-case-assistant');
    await pending;
    await page.evaluate(() => {
      history.pushState({}, '', '/case-assistant/second-chat');
      window.dispatchEvent(new PopStateEvent('popstate'));
    });
    await page.locator('textarea.chat-input').fill('Second chat prompt.');
    const send = page.getByRole('button', { name: 'Send', exact: true });
    await expect(send).toBeEnabled();
    const response = page.waitForResponse(r => r.url().endsWith('/api/get-uploaded-files-everything')
      && r.request().postDataJSON().draftId === 'e2e-case-assistant');
    release(); await response;
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await expect(send).toBeEnabled();
    await expect(page.getByTestId('composer-attachment')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Retry restoring attachments' })).toHaveCount(0);
    await send.click();
    await expect.poll(() => state.promptBody?.chat_thread_id).toBe('second-chat');
    expect(state.promptBody.chatFiles).toEqual([]);
  });
}

test('attachment restoration permits the first prompt in a newly created empty task', async ({ page }) => {
  const state = { completed: false };
  await mockCaseAssistantApis(page, state);
  await page.route('**/api/create-draft', route => fulfillJson(route, {
    draftId: 'e2e-case-assistant', draftName: 'Untitled Task',
  }));
  await page.addInitScript(() => {
    localStorage.setItem('videoWelcomeSeen_v2.0', 'true');
    localStorage.setItem('li_selectedProject', '1');
    localStorage.removeItem('li_selectedDraft');
  });
  await page.goto('/');
  await expect(page.getByRole('button', { name: /Add Task-Specific Docs/ })).toBeEnabled();
  await page.locator('textarea.chat-input').fill('My first prompt.');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect.poll(() => state.promptBody?.chat_thread_id).toBe('e2e-case-assistant');
  expect(state.promptBody.query).toBe('My first prompt.');
  expect(state.promptBody.chatFiles).toEqual([]);
  expect(state.promptCount).toBe(1);
});

test('a late accepted submission in A preserves B document exclusions', async ({ page }) => {
  const state = { completed: false };
  const backend = await mockCaseAssistantApis(page, state);
  let releaseSubmission, submissionStarted;
  const gate = new Promise(resolve => { releaseSubmission = resolve; });
  const pending = new Promise(resolve => { submissionStarted = resolve; });
  await page.route('**/api/everything', async route => {
    submissionStarted();
    await gate;
    await route.fallback();
  });
  await page.route('**/api/draft-name/exclusion-chat-B', route => fulfillJson(route, {
    draftId: 'exclusion-chat-B', draftName: 'Chat B', projectId: 1, docCategory: 69,
  }));
  await page.route('**/api/get-uploaded-files-everything', route => {
    if (route.request().postDataJSON().draftId !== 'exclusion-chat-B') return route.fallback();
    return fulfillJson(route, { uploaded_files: [] });
  });
  await page.route('**/api/everything/document-preparation?**', route => {
    if (new URL(route.request().url()).searchParams.get('draftId') !== 'exclusion-chat-B') return route.fallback();
    return fulfillJson(route, { ...backend.preparation(), documents: [{
      document_id: 'b-failed', filename: 'B.txt', file_size: 4, scope: 'chat',
      status: 'failed', stage: 'failed', generation: 1, error: 'Preparation failed', committed: false,
    }] });
  });
  await page.addInitScript(() => {
    localStorage.setItem('videoWelcomeSeen_v2.0', 'true');
    localStorage.setItem('li_selectedProject', '1');
    localStorage.setItem('li_selectedDraft', 'e2e-case-assistant');
  });
  await page.goto('/case-assistant/e2e-case-assistant');
  await page.locator('textarea.chat-input').fill('Start A.');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await pending;
  await page.evaluate(() => {
    history.pushState({}, '', '/case-assistant/exclusion-chat-B');
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
  await page.getByTestId('composer-attachment').getByRole('button', { name: 'B.txt (failed)', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Review files' });
  await dialog.getByRole('button', { name: 'Exclude from this prompt', exact: true }).click();
  await expect(dialog.getByRole('button', { name: 'Include in this prompt', exact: true })).toBeEnabled();
  const refreshAfterAcceptance = page.waitForResponse(response => {
    const url = new URL(response.url());
    return url.pathname === '/api/everything/document-preparation'
      && url.searchParams.get('draftId') === 'e2e-case-assistant';
  });
  releaseSubmission();
  await refreshAfterAcceptance;
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await expect(dialog.getByRole('button', { name: 'Include in this prompt', exact: true })).toBeEnabled();
  expect(await page.evaluate(() => JSON.parse(sessionStorage.getItem('document-exclusions:1/exclusion-chat-B'))))
    .toEqual(['b-failed']);
  expect(state.promptCount).toBe(1);
});

for (const interrupted of [false, true]) {
  test(`cross-tab uploads are discovered after ${interrupted ? 'failed restoration and Retry' : 'reload'}`, async ({ page, context }) => {
    const state = { completed: false, savedFiles: [{ unique_id: 'file-1', filename: 'original.txt', file_size: 4 }] };
    const initialize = () => {
      localStorage.setItem('videoWelcomeSeen_v2.0', 'true');
      localStorage.setItem('li_selectedProject', '1');
      localStorage.setItem('li_selectedDraft', 'e2e-case-assistant');
    };
    await mockCaseAssistantApis(page, state);
    await page.addInitScript(initialize);
    await page.goto('/case-assistant/e2e-case-assistant');
    await expect(page.getByTestId('composer-attachment')).toHaveCount(1);
    const second = await context.newPage();
    const backend = await mockCaseAssistantApis(second, state);
    await second.addInitScript(initialize);
    await second.goto('/case-assistant/e2e-case-assistant');
    await expect(second.getByTestId('composer-attachment')).toHaveCount(1);
    await second.locator('input[type="file"][accept=".pdf,.doc,.docx,.txt,.xlsx,.csv"]').setInputFiles({
      name: 'second-tab.txt', mimeType: 'text/plain', buffer: Buffer.from('text'),
    });
    await expect(second.locator('[data-testid="composer-attachment"][data-status="ready"]')).toHaveCount(2);
    // Both tabs now read the server corpus changed by the real second-tab upload.
    let release, failPreparation = false, reads = 0;
    const gate = new Promise(resolve => { release = resolve; });
    let hold = interrupted;
    await page.route('**/api/get-uploaded-files-everything', async route => {
      if (hold) { hold = false; await gate; failPreparation = true; }
      return fulfillJson(route, { uploaded_files: backend.uploadedFiles() });
    });
    await page.route('**/api/everything/document-preparation?**', route => {
      reads += 1;
      if (failPreparation) { failPreparation = false; return fulfillJson(route, { msg: 'Temporary failure' }, 503); }
      return fulfillJson(route, backend.preparation());
    });
    await page.reload();
    const input = page.locator('textarea.chat-input');
    await input.fill('Use both documents.');
    if (interrupted) {
      await expect.poll(() => reads).toBeGreaterThan(0);
      await expect(page.getByRole('button', { name: /Add Task-Specific Docs/ })).toContainText('(2/100)');
      release();
      await expect(page.getByRole('button', { name: 'Retry restoring attachments' })).toBeVisible();
      await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeDisabled();
      await input.press('Enter');
      expect(state.promptCount || 0).toBe(0);
      const saved = await page.evaluate(() => JSON.parse(sessionStorage.getItem('case-assistant:attachment-selection:e2e-case-assistant')));
      expect(saved.knownIds).toEqual(['file-1']);
      await page.getByRole('button', { name: 'Retry restoring attachments' }).click();
    }
    await expect(page.getByTestId('composer-attachment')).toHaveCount(2);
    await expect(page.getByRole('button', { name: /Add Task-Specific Docs/ })).toContainText('(2/100)');
    await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeEnabled();
    await page.getByRole('button', { name: 'Send', exact: true }).click();
    await expect.poll(() => state.promptBody?.chatFiles.map(file => file.name).sort()).toEqual(['file-1', 'file-2']);
    expect(state.promptBody.promptAttachments.map(file => file.file_id).sort()).toEqual(['file-1', 'file-2']);
  });
}
