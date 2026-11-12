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

function generatedAdmissions() {
  return [
    {
      Topic: 'Copyright Ownership',
      'Admission Request': 'Admit that Quentin Tarantino owns the asserted screenplay copyright.',
      'Strategic Rationale': 'Narrows ownership issues.',
    },
    {
      Topic: 'Publication and Access',
      'Admission Request': 'Admit that the screenplay was provided to third parties before the alleged publication.',
      'Strategic Rationale': 'Narrows access and publication issues.',
    },
    {
      Topic: 'Damages',
      'Admission Request': 'Admit that no license fee was paid for the alleged use.',
      'Strategic Rationale': 'Narrows damages issues.',
    },
  ];
}

function reducedAdmissions() {
  return generatedAdmissions().slice(0, 2);
}

async function mockRfaApis(page, state) {
  let draftTaskCounter = 0;

  await page.route('**/e2e/gawker-rfa.pdf', (route) => route.fulfill({
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
        drafts: [['e2e-gawker-rfa', 'Gawker RFA E2E', '', 4]],
      });
    }
    if (request.method() === 'GET' && pathname === '/api/smart-search/sessions') {
      return fulfillJson(route, { sessions: [] });
    }
    if (request.method() === 'GET' && pathname === '/api/draft-name/e2e-gawker-rfa') {
      return fulfillJson(route, {
        draftId: 'e2e-gawker-rfa',
        draftName: 'Gawker RFA E2E',
        projectId: 1,
        docCategory: 4,
      });
    }
    if (request.method() === 'GET' && pathname === '/api/rfa-inputs/e2e-gawker-rfa') {
      return fulfillJson(route, {});
    }
    if (request.method() === 'POST' && pathname === '/api/rfa/upload-complaint') {
      const body = (request.postDataBuffer() || Buffer.alloc(0)).toString('latin1');
      state.uploadBody = body;
      return fulfillJson(route, {
        result_id: 'rfa-parse-task-1',
        uploaded_filename: COMPLAINT_FILENAME,
      });
    }
    if (request.method() === 'POST' && pathname === '/api/rfa/continue-step0') {
      state.continueBody = JSON.parse(request.postData() || '{}');
      return fulfillJson(route, { result_id: 'rfa-continue-task-1' });
    }
    if (request.method() === 'POST' && pathname === '/api/rfa/further-reduce') {
      state.reduceBody = JSON.parse(request.postData() || '{}');
      return fulfillJson(route, { result_id: 'rfa-reduce-task-1' });
    }
    if (request.method() === 'POST' && pathname === '/api/rfadrafter') {
      state.draftBodies.push(request.postData() || '');
      draftTaskCounter += 1;
      return fulfillJson(route, { result_id: `rfa-draft-task-${draftTaskCounter}` });
    }
    if (request.method() === 'GET' && pathname === '/api/result/rfa-parse-task-1') {
      return fulfillJson(route, {
        ready: true,
        successful: true,
        value: {
          has_parsed_complaint: true,
          uploaded_filename: COMPLAINT_FILENAME,
          file_name: '/e2e/gawker-rfa.pdf',
        },
      });
    }
    if (request.method() === 'GET' && pathname === '/api/result/rfa-continue-task-1') {
      return fulfillJson(route, {
        ready: true,
        successful: true,
        value: {
          admissions: generatedAdmissions(),
          file_name: '/e2e/gawker-rfa.pdf',
        },
      });
    }
    if (request.method() === 'GET' && pathname === '/api/result/rfa-reduce-task-1') {
      return fulfillJson(route, {
        ready: true,
        successful: true,
        value: { admissions: reducedAdmissions() },
      });
    }
    if (request.method() === 'GET' && pathname === '/api/result/rfa-draft-task-1') {
      return fulfillJson(route, {
        ready: true,
        successful: true,
        value: {
          intro: 'Gawker Media, LLC requests that Quentin Tarantino admit the following matters.',
          definition: '“SCREENPLAY” means the work identified in the First Amended Complaint.',
          instructions: 'Each matter must be admitted or specifically denied.',
          file_name: '/e2e/gawker-rfa.pdf',
        },
      });
    }
    if (request.method() === 'GET' && pathname === '/api/result/rfa-draft-task-2') {
      return fulfillJson(route, {
        ready: true,
        successful: true,
        value: { file_name: '/e2e/gawker-rfa.pdf' },
      });
    }
    if (request.method() === 'GET' && pathname === '/api/download-word-rfa/e2e-gawker-rfa') {
      state.downloadRequested = true;
      return route.fulfill({
        status: 200,
        headers: {
          'access-control-allow-origin': '*',
          'content-type': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        },
        body: Buffer.from('PK\x03\x04 E2E RFA fixture'),
      });
    }

    return fulfillJson(route, {
      error: `Unexpected E2E request: ${request.method()} ${pathname}`,
    }, 404);
  });
}

test('drafts RFAs from the Gawker complaint through reduction and Word download', async ({ page }) => {
  const state = {
    uploadBody: '',
    continueBody: null,
    reduceBody: null,
    draftBodies: [],
    downloadRequested: false,
  };
  await mockRfaApis(page, state);

  await page.addInitScript(() => {
    localStorage.setItem('videoWelcomeSeen_v2.0', 'true');
    localStorage.setItem('li_selectedProject', '1');
    localStorage.setItem('li_selectedDraft', 'e2e-gawker-rfa');
  });

  await page.goto('/RfaDrafter/e2e-gawker-rfa');
  await expect(page.getByRole('heading', { name: 'RFA' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Upload a Complaint' })).toBeVisible();

  await page.locator('#side').selectOption('defendant');
  await page.locator('#fileUpload').setInputFiles(COMPLAINT_FIXTURE);
  await expect(page.getByRole('button', { name: 'Upload Complaint' })).toBeEnabled();
  await expect(page.getByRole('button', { name: 'Continue' })).toBeDisabled();
  await page.getByRole('button', { name: 'Upload Complaint' }).click();

  await expect(page.getByText(`Uploaded: ${COMPLAINT_FILENAME}`)).toBeVisible();
  await expect(page.getByRole('button', { name: 'Continue' })).toBeEnabled();
  await page.getByRole('button', { name: 'Continue' }).click();

  await expect(page.getByRole('heading', { name: 'Requests for Admission (Preview)' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Copyright Ownership' })).toBeVisible();
  await expect(page.locator('textarea[name^="rfa"]')).toHaveCount(3);
  await page.locator('input[type="number"]').fill('2');
  await page.getByRole('button', { name: 'Further Reduce' }).click();

  await expect(page.getByRole('heading', { name: 'Requests for Admission', exact: true })).toBeVisible();
  await expect(page.locator('textarea[name^="rfa"]')).toHaveCount(2);
  await page.locator('#rfa0').fill(
    'Admit that Quentin Tarantino owns the screenplay copyright asserted in the complaint.',
  );
  await page.getByRole('button', { name: 'Update Draft' }).click();

  await expect(page.getByRole('heading', { name: 'Supporting Sections' })).toBeVisible();
  await page.locator('#introBody').fill(
    'Gawker Media, LLC requests that Quentin Tarantino admit the following matters.',
  );
  await page.locator('#defsBody').fill(
    '“SCREENPLAY” means the work identified in the First Amended Complaint.',
  );
  await page.locator('#instructionsBody').fill(
    'Each matter must be admitted or specifically denied under the applicable rules.',
  );
  await page.getByRole('button', { name: 'Update Draft' }).click();

  await expect(page.getByRole('heading', { name: 'Review and Finalize' })).toBeVisible();
  await page.locator('.feedback-container button').first().click();
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Download as word document' }).click();
  const download = await downloadPromise;

  expect(download.suggestedFilename()).toBe('LexIntel_draft_e2e-gawker-rfa.docx');
  expect(state.uploadBody).toContain(`filename="${COMPLAINT_FILENAME}"`);
  expect(state.uploadBody).toContain('name="side"');
  expect(state.uploadBody).toContain('defendant');
  expect(state.continueBody).toEqual({ draftId: 'e2e-gawker-rfa' });
  expect(state.reduceBody).toEqual({ draftId: 'e2e-gawker-rfa', targetN: 2 });
  expect(state.draftBodies).toHaveLength(2);
  expect(state.draftBodies[0]).toContain('name="appState"');
  expect(state.draftBodies[0]).toContain(
    'Admit that Quentin Tarantino owns the screenplay copyright asserted in the complaint.',
  );
  expect(state.draftBodies[1]).toContain(
    'Each matter must be admitted or specifically denied under the applicable rules.',
  );
  expect(state.downloadRequested).toBe(true);
});
