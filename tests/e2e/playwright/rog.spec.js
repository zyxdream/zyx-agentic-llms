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

function generatedInterrogatories() {
  return [
    {
      Topic: 'Copyright Ownership',
      Interrogatory: 'Identify every person with knowledge of Quentin Tarantino’s ownership of the asserted screenplay copyright.',
      Rationale: 'Identifies witnesses with knowledge of ownership.',
    },
    {
      Topic: 'Publication and Access',
      Interrogatory: 'Describe each disclosure of the screenplay to a third party before the alleged publication.',
      Rationale: 'Develops facts concerning access and publication.',
    },
    {
      Topic: 'Damages',
      Interrogatory: 'State the amount and basis of every category of damages claimed in the complaint.',
      Rationale: 'Clarifies the claimed damages and supporting calculations.',
    },
  ];
}

function reducedInterrogatories() {
  return generatedInterrogatories().slice(0, 2);
}

async function mockRogApis(page, state) {
  let draftTaskCounter = 0;

  await page.route('**/e2e/gawker-rog.pdf', (route) => route.fulfill({
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
        drafts: [['e2e-gawker-rog', 'Gawker ROG E2E', '', 3]],
      });
    }
    if (request.method() === 'GET' && pathname === '/api/smart-search/sessions') {
      return fulfillJson(route, { sessions: [] });
    }
    if (request.method() === 'GET' && pathname === '/api/draft-name/e2e-gawker-rog') {
      return fulfillJson(route, {
        draftId: 'e2e-gawker-rog',
        draftName: 'Gawker ROG E2E',
        projectId: 1,
        docCategory: 3,
      });
    }
    if (request.method() === 'GET' && pathname === '/api/rog-inputs/e2e-gawker-rog') {
      return fulfillJson(route, {});
    }
    if (request.method() === 'POST' && pathname === '/api/rog/upload-complaint') {
      const body = (request.postDataBuffer() || Buffer.alloc(0)).toString('latin1');
      state.uploadBody = body;
      return fulfillJson(route, {
        result_id: 'rog-parse-task-1',
        uploaded_filename: COMPLAINT_FILENAME,
      });
    }
    if (request.method() === 'POST' && pathname === '/api/rog/continue-step0') {
      state.continueBody = JSON.parse(request.postData() || '{}');
      return fulfillJson(route, { result_id: 'rog-continue-task-1' });
    }
    if (request.method() === 'POST' && pathname === '/api/rog/further-reduce') {
      state.reduceBody = JSON.parse(request.postData() || '{}');
      return fulfillJson(route, { result_id: 'rog-reduce-task-1' });
    }
    if (request.method() === 'POST' && pathname === '/api/rogdrafter') {
      state.draftBodies.push(request.postData() || '');
      draftTaskCounter += 1;
      return fulfillJson(route, { result_id: `rog-draft-task-${draftTaskCounter}` });
    }
    if (request.method() === 'GET' && pathname === '/api/result/rog-parse-task-1') {
      return fulfillJson(route, {
        ready: true,
        successful: true,
        value: {
          has_parsed_complaint: true,
          uploaded_filename: COMPLAINT_FILENAME,
          file_name: '/e2e/gawker-rog.pdf',
        },
      });
    }
    if (request.method() === 'GET' && pathname === '/api/result/rog-continue-task-1') {
      return fulfillJson(route, {
        ready: true,
        successful: true,
        value: {
          interrogatories: generatedInterrogatories(),
          file_name: '/e2e/gawker-rog.pdf',
        },
      });
    }
    if (request.method() === 'GET' && pathname === '/api/result/rog-reduce-task-1') {
      return fulfillJson(route, {
        ready: true,
        successful: true,
        value: { interrogatories: reducedInterrogatories() },
      });
    }
    if (request.method() === 'GET' && pathname === '/api/result/rog-draft-task-1') {
      return fulfillJson(route, {
        ready: true,
        successful: true,
        value: {
          intro: 'Gawker Media, LLC propounds the following interrogatories to Quentin Tarantino.',
          definition: '“SCREENPLAY” means the work identified in the First Amended Complaint.',
          instructions: 'Answer each interrogatory separately and fully in writing under oath.',
          file_name: '/e2e/gawker-rog.pdf',
        },
      });
    }
    if (request.method() === 'GET' && pathname === '/api/result/rog-draft-task-2') {
      return fulfillJson(route, {
        ready: true,
        successful: true,
        value: { file_name: '/e2e/gawker-rog.pdf' },
      });
    }
    if (request.method() === 'GET' && pathname === '/api/download-word-rog/e2e-gawker-rog') {
      state.downloadRequested = true;
      return route.fulfill({
        status: 200,
        headers: {
          'access-control-allow-origin': '*',
          'content-type': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        },
        body: Buffer.from('PK\x03\x04 E2E ROG fixture'),
      });
    }

    return fulfillJson(route, {
      error: `Unexpected E2E request: ${request.method()} ${pathname}`,
    }, 404);
  });
}

test('drafts ROGs from the Gawker complaint through reduction and Word download', async ({ page }) => {
  const state = {
    uploadBody: '',
    continueBody: null,
    reduceBody: null,
    draftBodies: [],
    downloadRequested: false,
  };
  await mockRogApis(page, state);

  await page.addInitScript(() => {
    localStorage.setItem('videoWelcomeSeen_v2.0', 'true');
    localStorage.setItem('li_selectedProject', '1');
    localStorage.setItem('li_selectedDraft', 'e2e-gawker-rog');
  });

  await page.goto('/RogDrafter/e2e-gawker-rog');
  await expect(page.getByRole('heading', { name: 'ROG' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Upload a Complaint' })).toBeVisible();

  await page.locator('#side').selectOption('defendant');
  await page.locator('#fileUpload').setInputFiles(COMPLAINT_FIXTURE);
  await expect(page.getByRole('button', { name: 'Upload Complaint' })).toBeEnabled();
  await expect(page.getByRole('button', { name: 'Continue' })).toBeDisabled();
  await page.getByRole('button', { name: 'Upload Complaint' }).click();

  await expect(page.getByText(`Uploaded: ${COMPLAINT_FILENAME}`)).toBeVisible();
  await expect(page.getByRole('button', { name: 'Continue' })).toBeEnabled();
  await page.getByRole('button', { name: 'Continue' }).click();

  await expect(page.getByRole('heading', { name: 'Interrogatories (Preview)' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Copyright Ownership' })).toBeVisible();
  await expect(page.locator('textarea[name^="coa"]')).toHaveCount(3);
  await page.locator('input[type="number"]').fill('2');
  await page.getByRole('button', { name: 'Further Reduce' }).click();

  await expect(page.getByRole('heading', { name: 'Interrogatories', exact: true })).toBeVisible();
  await expect(page.locator('textarea[name^="coa"]')).toHaveCount(2);
  await page.locator('#coa0').fill(
    'Identify every person with knowledge of the screenplay copyright asserted in the complaint.',
  );
  await page.getByRole('button', { name: 'Update Draft' }).click();

  await expect(page.getByRole('heading', { name: 'Supporting Sections' })).toBeVisible();
  await page.locator('#introBody').fill(
    'Gawker Media, LLC propounds the following interrogatories to Quentin Tarantino.',
  );
  await page.locator('#defsBody').fill(
    '“SCREENPLAY” means the work identified in the First Amended Complaint.',
  );
  await page.locator('#instructionsBody').fill(
    'Answer each interrogatory separately and fully in writing under oath and within the required time.',
  );
  await page.getByRole('button', { name: 'Update Draft' }).click();

  await expect(page.getByRole('heading', { name: 'Review and Finalize' })).toBeVisible();
  await page.locator('.feedback-container button').first().click();
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Download as word document' }).click();
  const download = await downloadPromise;

  expect(download.suggestedFilename()).toBe('LexIntel_draft_e2e-gawker-rog.docx');
  expect(state.uploadBody).toContain(`filename="${COMPLAINT_FILENAME}"`);
  expect(state.uploadBody).toContain('name="side"');
  expect(state.uploadBody).toContain('defendant');
  expect(state.continueBody).toEqual({ draftId: 'e2e-gawker-rog' });
  expect(state.reduceBody).toEqual({ draftId: 'e2e-gawker-rog', targetN: 2 });
  expect(state.draftBodies).toHaveLength(2);
  expect(state.draftBodies[0]).toContain('name="appState"');
  expect(state.draftBodies[0]).toContain(
    'Identify every person with knowledge of the screenplay copyright asserted in the complaint.',
  );
  expect(state.draftBodies[1]).toContain(
    'Answer each interrogatory separately and fully in writing under oath and within the required time.',
  );
  expect(state.downloadRequested).toBe(true);
});
