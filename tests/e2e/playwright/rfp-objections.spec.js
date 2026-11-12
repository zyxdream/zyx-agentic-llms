const path = require('path');
const { createRequire } = require('module');

const frontendRequire = createRequire(
  path.join(__dirname, '../../../frontend/lex-intel/package.json'),
);
const { test, expect } = frontendRequire('@playwright/test');

const RFP_FIXTURE = path.join(
  __dirname,
  '../fixtures/sample-rfp-uber.pdf',
);
const RFP_FILENAME = path.basename(RFP_FIXTURE);

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

function generatedResponses() {
  return [
    {
      request_id: 'Request for Production No. 1',
      request: 'Documents concerning the alleged Waymo trade secrets identified in the complaint.',
      objection: 'Plaintiff objects that this request is overbroad and seeks information protected from disclosure.',
    },
    {
      request_id: 'Request for Production No. 2',
      request: 'Documents concerning communications about the asserted LiDAR technology.',
      objection: 'Plaintiff objects to the extent the request seeks privileged communications.',
    },
  ];
}

async function mockRfpObjectionsApis(page, state) {
  let draftTaskCounter = 0;

  await page.route('**/e2e/uber-rfp-response.pdf', (route) => route.fulfill({
    status: 200,
    contentType: 'application/pdf',
    path: RFP_FIXTURE,
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
        drafts: [['e2e-uber-rfp-objections', 'Uber RFP Objections E2E', '', 5]],
      });
    }
    if (request.method() === 'GET' && pathname === '/api/smart-search/sessions') {
      return fulfillJson(route, { sessions: [] });
    }
    if (request.method() === 'GET' && pathname === '/api/draft-name/e2e-uber-rfp-objections') {
      return fulfillJson(route, {
        draftId: 'e2e-uber-rfp-objections',
        draftName: 'Uber RFP Objections E2E',
        projectId: 1,
        docCategory: 5,
      });
    }
    if (request.method() === 'GET' && pathname === '/api/objections-inputs/e2e-uber-rfp-objections') {
      return fulfillJson(route, {});
    }
    if (request.method() === 'POST' && pathname === '/api/rfp-response/upload-rfp') {
      const body = (request.postDataBuffer() || Buffer.alloc(0)).toString('latin1');
      state.uploadBody = body;
      return fulfillJson(route, {
        result_id: 'rfp-objections-parse-task-1',
        uploaded_filename: RFP_FILENAME,
      });
    }
    if (request.method() === 'POST' && pathname === '/api/rfp-response/continue-step0') {
      state.continueBody = JSON.parse(request.postData() || '{}');
      return fulfillJson(route, { result_id: 'rfp-objections-continue-task-1' });
    }
    if (request.method() === 'POST' && pathname === '/api/rfpresponsedrafter') {
      state.draftBodies.push(request.postData() || '');
      draftTaskCounter += 1;
      return fulfillJson(route, {
        result_id: `rfp-objections-draft-task-${draftTaskCounter}`,
      });
    }
    if (request.method() === 'GET' && pathname === '/api/result/rfp-objections-parse-task-1') {
      return fulfillJson(route, {
        ready: true,
        successful: true,
        value: {
          has_parsed_document: true,
          uploaded_filename: RFP_FILENAME,
          file_name: '/e2e/uber-rfp-response.pdf',
        },
      });
    }
    if (request.method() === 'GET' && pathname === '/api/result/rfp-objections-continue-task-1') {
      return fulfillJson(route, {
        ready: true,
        successful: true,
        value: {
          intro: 'Plaintiff Waymo LLC responds and objects to Defendants’ First Set of Requests for Production.',
          general: 'Plaintiff objects to each request to the extent it seeks privileged or irrelevant material.',
          definition_objections: 'Plaintiff objects to definitions that are vague, ambiguous, or overbroad.',
          instruction_objections: 'Plaintiff objects to instructions that exceed the Federal Rules of Civil Procedure.',
          file_name: '/e2e/uber-rfp-response.pdf',
        },
      });
    }
    if (request.method() === 'GET' && pathname === '/api/result/rfp-objections-draft-task-1') {
      return fulfillJson(route, {
        ready: true,
        successful: true,
        value: {
          rfp_response: generatedResponses(),
          file_name: '/e2e/uber-rfp-response.pdf',
        },
      });
    }
    if (request.method() === 'GET' && pathname === '/api/result/rfp-objections-draft-task-2') {
      return fulfillJson(route, {
        ready: true,
        successful: true,
        value: { file_name: '/e2e/uber-rfp-response.pdf' },
      });
    }
    if (request.method() === 'GET' && pathname === '/api/download-word-rfp-response/e2e-uber-rfp-objections') {
      state.downloadRequested = true;
      return route.fulfill({
        status: 200,
        headers: {
          'access-control-allow-origin': '*',
          'content-type': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        },
        body: Buffer.from('PK\x03\x04 E2E RFP objections fixture'),
      });
    }

    return fulfillJson(route, {
      error: `Unexpected E2E request: ${request.method()} ${pathname}`,
    }, 404);
  });
}

test('drafts objections and responses to the Uber RFPs through Word download', async ({ page }) => {
  const state = {
    uploadBody: '',
    continueBody: null,
    draftBodies: [],
    downloadRequested: false,
  };
  await mockRfpObjectionsApis(page, state);

  await page.addInitScript(() => {
    localStorage.setItem('videoWelcomeSeen_v2.0', 'true');
    localStorage.setItem('li_selectedProject', '1');
    localStorage.setItem('li_selectedDraft', 'e2e-uber-rfp-objections');
  });

  await page.goto('/ObjectionsDrafter/e2e-uber-rfp-objections');
  await expect(page.getByRole('heading', { name: 'Objection Drafter' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Upload a Request for Production' })).toBeVisible();

  await page.locator('#side').selectOption('plaintiff');
  await page.locator('#fileUpload').setInputFiles(RFP_FIXTURE);
  await expect(page.getByRole('button', { name: 'Upload RFPs' })).toBeEnabled();
  await expect(page.getByRole('button', { name: 'Continue' })).toBeDisabled();
  await page.getByRole('button', { name: 'Upload RFPs' }).click();

  await expect(page.getByText(`Uploaded: ${RFP_FILENAME}`)).toBeVisible();
  await expect(page.getByRole('button', { name: 'Continue' })).toBeEnabled();
  await page.getByRole('button', { name: 'Continue' }).click();

  await expect(page.getByRole('heading', { name: 'Supporting Sections' })).toBeVisible();
  await page.locator('#introBody').fill(
    'Plaintiff Waymo LLC responds and objects to Defendants’ First Set of Requests for Production.',
  );
  await page.locator('#generalBody').fill(
    'Plaintiff objects to each request to the extent it seeks privileged, irrelevant, or disproportionate material.',
  );
  await page.locator('#objectionBody').fill(
    'Plaintiff objects to vague definitions and to instructions that exceed the Federal Rules of Civil Procedure.',
  );
  await page.getByRole('button', { name: 'Update Draft' }).click();

  await expect(page.getByRole('heading', { name: 'Specific Objections and Responses' })).toBeVisible();
  await expect(page.getByText('Request for Production No. 1', { exact: true })).toBeVisible();
  await expect(page.locator('select[name^="req-"]')).toHaveCount(2);
  await page.locator('#objection-0').fill(
    'Plaintiff objects that Request No. 1 is overbroad and seeks protected information.',
  );
  await page.locator('#req-0').selectOption('Custom');
  await page.locator('textarea[name="custom-0"]').fill(
    'After a reasonable search, Plaintiff will produce nonprivileged responsive documents in its possession.',
  );
  await page.locator('#req-1').selectOption('Full Production');
  await page.getByRole('button', { name: 'Update Draft' }).click();

  await expect(page.getByRole('heading', { name: 'Review and Finalize' })).toBeVisible();
  await page.locator('.feedback-container button').first().click();
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Download as word document' }).click();
  const download = await downloadPromise;

  expect(download.suggestedFilename()).toBe(
    'LexIntel_draft_e2e-uber-rfp-objections.docx',
  );
  expect(state.uploadBody).toContain(`filename="${RFP_FILENAME}"`);
  expect(state.uploadBody).toContain('name="side"');
  expect(state.uploadBody).toContain('plaintiff');
  expect(state.continueBody).toEqual({ draftId: 'e2e-uber-rfp-objections' });
  expect(state.draftBodies).toHaveLength(2);
  expect(state.draftBodies[0]).toContain('name="appState"');
  expect(state.draftBodies[0]).toContain(
    'Plaintiff objects to each request to the extent it seeks privileged, irrelevant, or disproportionate material.',
  );
  expect(state.draftBodies[1]).toContain(
    'Plaintiff objects that Request No. 1 is overbroad and seeks protected information.',
  );
  expect(state.draftBodies[1]).toContain(
    'After a reasonable search, Plaintiff will produce nonprivileged responsive documents in its possession.',
  );
  expect(state.downloadRequested).toBe(true);
});
