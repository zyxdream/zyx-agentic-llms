const path = require('path');
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

function generatedRequests() {
  return [
    {
      Topic: 'Copyright Ownership',
      Request: 'Produce all documents concerning ownership of the asserted screenplay copyright.',
      Justification: 'Ownership is an element of the asserted copyright claim.',
    },
    {
      Topic: 'Publication and Access',
      Request: 'Produce all documents concerning publication of or access to the screenplay.',
      Justification: 'The materials bear on access, publication, and the alleged infringement.',
    },
    {
      Topic: 'Damages',
      Request: 'Produce all documents supporting the damages claimed in the complaint.',
      Justification: 'The request tests the amount and basis of claimed damages.',
    },
  ];
}

function reducedRequests() {
  return generatedRequests().slice(0, 2);
}

async function mockRfpApis(page, state) {
  let draftTaskCounter = 0;

  await page.route('**/e2e/gawker-rfp.pdf', (route) => route.fulfill({
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
        drafts: [['e2e-gawker-rfp', 'Gawker RFP E2E', '', 2]],
      });
    }
    if (request.method() === 'GET' && pathname === '/api/smart-search/sessions') {
      return fulfillJson(route, { sessions: [] });
    }
    if (request.method() === 'GET' && pathname === '/api/draft-name/e2e-gawker-rfp') {
      return fulfillJson(route, {
        draftId: 'e2e-gawker-rfp',
        draftName: 'Gawker RFP E2E',
        projectId: 1,
        docCategory: 2,
      });
    }
    if (request.method() === 'GET' && pathname === '/api/rfp-inputs/e2e-gawker-rfp') {
      return fulfillJson(route, {});
    }
    if (request.method() === 'POST' && pathname === '/api/rfp/upload-complaint') {
      const body = (request.postDataBuffer() || Buffer.alloc(0)).toString('latin1');
      state.uploadBody = body;
      return fulfillJson(route, {
        result_id: 'rfp-parse-task-1',
        uploaded_filename: COMPLAINT_FILENAME,
      });
    }
    if (request.method() === 'POST' && pathname === '/api/rfp/generate-requests') {
      state.generateBody = JSON.parse(request.postData() || '{}');
      return fulfillJson(route, { result_id: 'rfp-generate-task-1' });
    }
    if (request.method() === 'POST' && pathname === '/api/rfp/further-reduce') {
      state.reduceBody = JSON.parse(request.postData() || '{}');
      return fulfillJson(route, { result_id: 'rfp-reduce-task-1' });
    }
    if (request.method() === 'POST' && pathname === '/api/rfpdrafter') {
      state.draftBodies.push(request.postData() || '');
      draftTaskCounter += 1;
      return fulfillJson(route, { result_id: `rfp-draft-task-${draftTaskCounter}` });
    }
    if (request.method() === 'GET' && pathname === '/api/result/rfp-parse-task-1') {
      return fulfillJson(route, {
        ready: true,
        successful: true,
        value: {
          has_parsed_complaint: true,
          uploaded_filename: COMPLAINT_FILENAME,
          file_name: '/e2e/gawker-rfp.pdf',
        },
      });
    }
    if (request.method() === 'GET' && pathname === '/api/result/rfp-generate-task-1') {
      return fulfillJson(route, {
        ready: true,
        successful: true,
        value: {
          requests: generatedRequests(),
          file_name: '/e2e/gawker-rfp.pdf',
        },
      });
    }
    if (request.method() === 'GET' && pathname === '/api/result/rfp-reduce-task-1') {
      return fulfillJson(route, {
        ready: true,
        successful: true,
        value: { requests: reducedRequests() },
      });
    }
    if (request.method() === 'GET' && pathname === '/api/result/rfp-draft-task-1') {
      return fulfillJson(route, {
        ready: true,
        successful: true,
        value: {
          intro: 'Gawker Media, LLC requests that Quentin Tarantino produce the following materials.',
          definition: '“SCREENPLAY” means the work identified in the First Amended Complaint.',
          instructions: 'Produce responsive documents in their native format.',
          file_name: '/e2e/gawker-rfp.pdf',
        },
      });
    }
    if (request.method() === 'GET' && pathname === '/api/result/rfp-draft-task-2') {
      return fulfillJson(route, {
        ready: true,
        successful: true,
        value: { file_name: '/e2e/gawker-rfp.pdf' },
      });
    }
    if (request.method() === 'GET' && pathname === '/api/download-word-rfp/e2e-gawker-rfp') {
      state.downloadRequested = true;
      return route.fulfill({
        status: 200,
        headers: {
          'access-control-allow-origin': '*',
          'content-type': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        },
        body: Buffer.from('PK\x03\x04 E2E RFP fixture'),
      });
    }

    return fulfillJson(route, {
      error: `Unexpected E2E request: ${request.method()} ${pathname}`,
    }, 404);
  });
}

test('drafts RFPs from the Gawker complaint through reduction and Word download', async ({ page }) => {
  const state = {
    uploadBody: '',
    generateBody: null,
    reduceBody: null,
    draftBodies: [],
    downloadRequested: false,
  };
  await mockRfpApis(page, state);

  await page.addInitScript(() => {
    localStorage.setItem('videoWelcomeSeen_v2.0', 'true');
    localStorage.setItem('li_selectedProject', '1');
    localStorage.setItem('li_selectedDraft', 'e2e-gawker-rfp');
  });

  await page.goto('/RfpDrafter/e2e-gawker-rfp');
  await expect(page.getByRole('heading', { name: 'RFP' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Upload a Complaint' })).toBeVisible();

  await page.locator('#side').selectOption('defendant');
  await page.locator('#fileUpload').setInputFiles(COMPLAINT_FIXTURE);
  await expect(page.getByRole('button', { name: 'Upload Complaint' })).toBeEnabled();
  await expect(page.getByRole('button', { name: 'Generate Requests' })).toBeDisabled();
  await page.getByRole('button', { name: 'Upload Complaint' }).click();

  await expect(page.getByText(`Uploaded: ${COMPLAINT_FILENAME}`)).toBeVisible();
  await expect(page.getByRole('button', { name: 'Generate Requests' })).toBeEnabled();
  await page.getByRole('button', { name: 'Generate Requests' }).click();

  await expect(page.getByRole('heading', { name: 'Requests for Production (Preview)' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Copyright Ownership' })).toBeVisible();
  await expect(page.locator('textarea[name^="coa"]')).toHaveCount(3);
  await page.locator('input[type="number"]').fill('2');
  await page.getByRole('button', { name: 'Apply' }).click();

  await expect(page.getByRole('heading', { name: 'Requests for Production', exact: true })).toBeVisible();
  await expect(page.locator('textarea[name^="coa"]')).toHaveCount(2);
  await page.locator('#coa0').fill(
    'Produce all documents establishing ownership of the screenplay identified in the complaint.',
  );
  await page.getByRole('button', { name: 'Update Draft' }).click();

  await expect(page.getByRole('heading', { name: 'Supporting Sections' })).toBeVisible();
  await page.locator('#introBody').fill(
    'Gawker Media, LLC requests that Quentin Tarantino produce the following materials.',
  );
  await page.locator('#defsBody').fill(
    '“SCREENPLAY” means the work identified in the First Amended Complaint.',
  );
  await page.locator('#instructionsBody').fill(
    'Produce responsive documents in their native format with metadata intact.',
  );
  await page.getByRole('button', { name: 'Update Draft' }).click();

  await expect(page.getByRole('heading', { name: 'Review and Finalize' })).toBeVisible();
  await page.locator('.feedback-container button').first().click();
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Download as word document' }).click();
  const download = await downloadPromise;

  expect(download.suggestedFilename()).toBe('LexIntel_draft_e2e-gawker-rfp.docx');
  expect(state.uploadBody).toContain(`filename="${COMPLAINT_FILENAME}"`);
  expect(state.uploadBody).toContain('name="side"');
  expect(state.uploadBody).toContain('defendant');
  expect(state.generateBody).toEqual({ draftId: 'e2e-gawker-rfp' });
  expect(state.reduceBody).toEqual({ draftId: 'e2e-gawker-rfp', targetN: 2 });
  expect(state.draftBodies).toHaveLength(2);
  expect(state.draftBodies[0]).toContain('name="appState"');
  expect(state.draftBodies[0]).toContain(
    'Produce all documents establishing ownership of the screenplay identified in the complaint.',
  );
  expect(state.draftBodies[1]).toContain(
    'Produce responsive documents in their native format with metadata intact.',
  );
  expect(state.downloadRequested).toBe(true);
});
