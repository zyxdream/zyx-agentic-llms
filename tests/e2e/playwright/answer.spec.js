const path = require('path');
const fs = require('fs');
const { createRequire } = require('module');

const frontendRequire = createRequire(
  path.join(__dirname, '../../../frontend/lex-intel/package.json'),
);
const { test, expect } = frontendRequire('@playwright/test');

const COMPLAINT_FIXTURE = path.join(
  __dirname,
  '../fixtures/sample-complaint-gawker.pdf',
);
const COMPLAINT_FILENAME = path.basename(COMPLAINT_FIXTURE);

const JSON_HEADERS = {
  'access-control-allow-origin': '*',
  'content-type': 'application/json',
};

function fulfillJson(route, payload, status = 200) {
  return route.fulfill({
    status,
    headers: JSON_HEADERS,
    body: JSON.stringify(payload),
  });
}

function answerSetupResult() {
  return {
    ads_suggestions: {
      'Fair Use': {
        1: {
          cause_of_action: 'Copyright Infringement',
          description: 'The challenged publication constitutes fair use.',
          filtered_citation_html: [],
        },
      },
      'Failure to State a Claim': {
        1: {
          cause_of_action: 'Copyright Infringement',
          description: 'The complaint does not allege facts sufficient to state a claim.',
          filtered_citation_html: [],
        },
      },
    },
    outline: [
      ['Jurisdiction and Venue', null],
      ['Paragraph 1', 1],
      ['Paragraph 2', 2],
      ['Paragraph 3', 3],
    ],
    paragraph_warnings: null,
    intro: 'Gawker Media, LLC answers Quentin Tarantino’s First Amended Complaint.',
    general_denial: null,
    defendant: ['Gawker Media, LLC'],
    halls: [],
    file_name: '/e2e/gawker-answer.pdf',
  };
}

async function mockAnswerApis(page, state) {
  let answerTaskCounter = 0;

  await page.route('**/e2e/gawker-answer.pdf', (route) => route.fulfill({
    status: 200,
    contentType: 'application/pdf',
    path: COMPLAINT_FIXTURE,
  }));

  await page.route('https://script.google.com/**', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: '{}',
  }));

  await page.route('http://127.0.0.1:5000/api/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const pathname = url.pathname;

    if (request.method() === 'POST' && pathname === '/api/list-projects') {
      return fulfillJson(route, { projects: [['E2E Matter', 1]] });
    }
    if (request.method() === 'POST' && pathname === '/api/list-drafts') {
      return fulfillJson(route, {
        drafts: [['e2e-gawker-answer', 'Gawker Answer E2E', '', 1]],
      });
    }
    if (request.method() === 'GET' && pathname === '/api/smart-search/sessions') {
      return fulfillJson(route, { sessions: [] });
    }
    if (request.method() === 'GET' && pathname === '/api/draft-name/e2e-gawker-answer') {
      return fulfillJson(route, {
        draftId: 'e2e-gawker-answer',
        draftName: 'Gawker Answer E2E',
        projectId: 1,
        docCategory: 1,
      });
    }
    if (request.method() === 'GET' && pathname === '/api/answer-inputs/e2e-gawker-answer') {
      return fulfillJson(route, {});
    }
    if (request.method() === 'POST' && pathname === '/api/answer/upload-complaint') {
      const body = request.postDataBuffer() || Buffer.alloc(0);
      state.uploadedFixtureName = body.includes(
        Buffer.from(`filename="${COMPLAINT_FILENAME}"`),
      );
      return fulfillJson(route, { result_id: 'parse-task-1' });
    }
    if (request.method() === 'POST' && pathname === '/api/answer/continue-step0') {
      state.continueBody = JSON.parse(request.postData() || '{}');
      return fulfillJson(route, { result_id: 'continue-task-1' });
    }
    if (request.method() === 'POST' && pathname === '/api/answerdrafter') {
      const body = request.postData() || '';
      state.answerBodies.push(body);
      answerTaskCounter += 1;
      return fulfillJson(route, { result_id: `answer-task-${answerTaskCounter}` });
    }
    if (request.method() === 'GET' && pathname === '/api/result/parse-task-1') {
      return fulfillJson(route, {
        ready: true,
        successful: true,
        value: {
          uploaded_filename: COMPLAINT_FILENAME,
          file_name: '/e2e/gawker-answer.pdf',
        },
      });
    }
    if (request.method() === 'GET' && pathname === '/api/result/continue-task-1') {
      return fulfillJson(route, {
        ready: true,
        successful: true,
        value: answerSetupResult(),
      });
    }
    if (request.method() === 'GET' && pathname.startsWith('/api/result/answer-task-')) {
      return fulfillJson(route, {
        ready: true,
        successful: true,
        value: { file_name: '/e2e/gawker-answer.pdf' },
      });
    }
    if (request.method() === 'GET' && pathname === '/api/download-word-answer/e2e-gawker-answer') {
      state.downloadRequested = true;
      return route.fulfill({
        status: 200,
        headers: {
          'access-control-allow-origin': '*',
          'content-type': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        },
        body: Buffer.from('PK\x03\x04 E2E Answer fixture'),
      });
    }

    return fulfillJson(route, {
      error: `Unexpected E2E request: ${request.method()} ${pathname}`,
    }, 404);
  });
}

test('drafts an Answer from the Gawker complaint through Word download', async ({ page }) => {
  const state = {
    uploadedFixtureName: false,
    continueBody: null,
    answerBodies: [],
    downloadRequested: false,
  };
  await mockAnswerApis(page, state);

  await page.addInitScript(() => {
    localStorage.setItem('videoWelcomeSeen_v2.0', 'true');
    localStorage.setItem('li_selectedProject', '1');
    localStorage.setItem('li_selectedDraft', 'e2e-gawker-answer');
  });

  await page.goto('/AnswerDrafter/e2e-gawker-answer');
  await expect(page.getByRole('heading', { name: 'Answer Drafter' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Upload a Complaint' })).toBeVisible();

  const fixture = fs.readFileSync(COMPLAINT_FIXTURE);
  expect(fixture.length).toBe(152820);
  expect(fixture.subarray(0, 8).toString('ascii')).toBe('%PDF-1.4');
  await page.locator('#fileUpload').setInputFiles(COMPLAINT_FIXTURE);
  await expect(page.getByText(`Uploaded: ${COMPLAINT_FILENAME}`)).toBeVisible();
  await expect(page.getByRole('button', { name: 'Continue' })).toBeEnabled();
  await page.getByRole('button', { name: 'Continue' }).click();

  await expect(page.getByRole('heading', { name: 'Defendants' })).toBeVisible();
  await expect(page.getByText('Gawker Media, LLC', { exact: true })).toBeVisible();
  await page.locator('#para0001').selectOption('admit');
  await page.locator('#para0002').selectOption('notEnoughInfo');
  await page.locator('#para0003').selectOption('custom');
  await page.locator('[name="customResponse-para0003"]').fill(
    'Gawker denies that the excerpt infringed any enforceable copyright.',
  );
  await page.getByRole('button', { name: 'Next: Affirmative defenses' }).click();

  await expect(page.getByRole('heading', { name: 'Suggested Affirmative Defenses' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Fair Use' })).toBeVisible();
  await page.getByRole('button', { name: 'Next: Counterclaim' }).click();

  await expect(page.getByRole('heading', { name: 'Writing the Counterclaim' })).toBeVisible();
  await page.locator('#ccFacts').fill('No counterclaim should be asserted for this test.');
  await page.getByRole('button', { name: 'Next: Introduction' }).click();

  await expect(page.getByRole('heading', { name: 'Introduction' })).toBeVisible();
  await page.locator('#introBody').fill(
    'Gawker Media, LLC answers the First Amended Complaint as follows.',
  );
  await page.getByRole('button', { name: 'Next: Hallucination Guard' }).click();

  await expect(page.getByRole('heading', { name: 'Hallucination Guard' })).toBeVisible();
  await expect(page.getByText('No citation issues found.')).toBeVisible();
  await page.getByRole('button', { name: 'Review and finalize draft' }).click();

  await expect(page.getByRole('heading', { name: 'Review and Finalize' })).toBeVisible();
  await page.locator('.feedback-container button').first().click();
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Download as word document' }).click();
  const download = await downloadPromise;

  expect(download.suggestedFilename()).toBe('LexIntel_draft_e2e-gawker-answer.docx');
  expect(state.uploadedFixtureName).toBe(true);
  expect(state.continueBody).toEqual({ draftId: 'e2e-gawker-answer' });
  expect(state.answerBodies).toHaveLength(4);
  // Step 1 used to send multipart para#### fields; compact JSON avoids 413s for long complaints.
  const step1Body = JSON.parse(state.answerBodies[0]);
  expect(step1Body).toEqual({
    draftId: 'e2e-gawker-answer',
    appState: 1,
    defendants: ['Gawker Media, LLC'],
    default_choice: 'deny',
    paragraph_selections: [
      { outline_index: 1, paragraph_number: 1, choice: 'admit' },
      { outline_index: 2, paragraph_number: 2, choice: 'notEnoughInfo' },
      {
        outline_index: 3,
        paragraph_number: 3,
        choice: 'custom',
        custom_response: 'Gawker denies that the excerpt infringed any enforceable copyright.',
      },
    ],
  });
  expect(state.answerBodies[2]).toContain('No counterclaim should be asserted for this test.');
  expect(state.answerBodies[3]).toContain(
    'Gawker Media, LLC answers the First Amended Complaint as follows.',
  );
  expect(state.downloadRequested).toBe(true);
});
