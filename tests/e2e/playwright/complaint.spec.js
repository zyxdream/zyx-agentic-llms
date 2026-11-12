const path = require('path');
const { createRequire } = require('module');
const frontendRequire = createRequire(
  path.join(__dirname, '../../../frontend/lex-intel/package.json'),
);
const { test, expect } = frontendRequire('@playwright/test');
const complaints = require('../fixtures/complaints.json');

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

function resultFor(complaint, appState) {
  const pdfUrl = `/e2e/${complaint.slug}.pdf`;
  const cause = complaint.llm.causes_of_action_suggestions[0];
  const causeName = Object.keys(cause)[0];
  const causeData = cause[causeName];

  if (appState === 0) {
    return {
      url: pdfUrl,
      intro: complaint.llm.introduction,
      pfr_suggestions: complaint.llm.suggested_prayers,
      coa_suggestions: [{
        [causeName]: {
          description: causeData.description,
          citations: causeData.citations || [],
          filtered_citations: [],
          filtered_citations_info: [],
          citations_html: [],
        },
      }],
      halls: [],
    };
  }

  return { url: pdfUrl, halls: [] };
}

async function mockComplaintApis(page, complaint, submittedBodies) {
  const taskResults = new Map();
  let taskCounter = 0;

  await page.route('**/e2e/*.pdf', (route) => route.fulfill({
    status: 200,
    contentType: 'application/pdf',
    body: '%PDF-1.4\n% E2E fixture\n',
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
      return fulfillJson(route, { drafts: [] });
    }
    if (request.method() === 'GET' && pathname === '/api/smart-search/sessions') {
      return fulfillJson(route, { sessions: [] });
    }
    if (request.method() === 'GET' && pathname === `/api/draft-name/${complaint.slug}`) {
      return fulfillJson(route, { draftName: 'E2E Complaint' });
    }
    if (request.method() === 'GET' && pathname === `/api/complaint-inputs/${complaint.slug}`) {
      return fulfillJson(route, {});
    }
    if (request.method() === 'POST' && pathname === '/api/complaintdrafter') {
      const body = request.postData() || '';
      submittedBodies.push(body);
      const appStateMatch = body.match(/name="appState"\r?\n\r?\n(\d+)/);
      const appState = appStateMatch ? Number(appStateMatch[1]) : 0;
      const taskId = `e2e-task-${++taskCounter}`;
      taskResults.set(taskId, resultFor(complaint, appState));
      return fulfillJson(route, { result_id: taskId });
    }
    if (request.method() === 'GET' && pathname.startsWith('/api/result/')) {
      const taskId = pathname.substring('/api/result/'.length);
      return fulfillJson(route, {
        ready: true,
        successful: true,
        value: taskResults.get(taskId) || { url: `/e2e/${complaint.slug}.pdf`, halls: [] },
        info: null,
      });
    }
    if (request.method() === 'POST' && pathname === '/api/complaintdrafter/update-citation-selections') {
      return fulfillJson(route, {});
    }
    if (request.method() === 'GET' && pathname === `/api/download-word-complaint/${complaint.slug}`) {
      return route.fulfill({
        status: 200,
        headers: {
          'access-control-allow-origin': '*',
          'content-type': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        },
        body: Buffer.from('PK\x03\x04 E2E fixture'),
      });
    }

    return fulfillJson(route, { error: `Unexpected E2E request: ${request.method()} ${pathname}` }, 404);
  });

  return {
    taskCount: () => taskCounter,
  };
}

for (const complaint of complaints) {
  test(`drafts a complaint for ${complaint.venue}`, async ({ page }) => {
    const submittedBodies = [];
    const apiState = await mockComplaintApis(page, complaint, submittedBodies);

    await page.addInitScript(() => {
      localStorage.setItem('videoWelcomeSeen_v2.0', 'true');
    });

    await page.goto(`/ComplaintDrafter/${complaint.slug}`);
    await expect(page.getByRole('heading', { name: 'Complaint Drafter' })).toBeVisible();

    await page.locator('#firstSelector').selectOption('federal');
    const venueValue = await page.locator('#secondSelector option').evaluateAll((options, venue) => {
      const normalizedVenue = venue.trim().toUpperCase();
      return options.find((option) => option.textContent.trim().toUpperCase() === normalizedVenue)?.value;
    }, complaint.venue);
    expect(venueValue).toBeTruthy();
    await page.locator('#secondSelector').selectOption(venueValue);
    await page.locator('#plaintiff-0').fill(complaint.plaintiff);
    await page.locator('#defendant-0').fill(complaint.defendant);
    await page.locator('#factualAllegations').fill(complaint.factual_allegations_input);
    await page.getByRole('button', { name: 'Draft complaint' }).click();

    await expect(page.getByRole('heading', { name: 'Cause of Action 1' })).toBeVisible();
    await page.getByRole('button', { name: 'Update draft and continue' }).click();
    await expect(page.getByRole('heading', { name: 'Prayers for Relief' })).toBeVisible();
    await page.getByRole('button', { name: 'Update draft and continue' }).click();
    await expect(page.getByRole('heading', { name: 'Introduction' })).toBeVisible();
    await page.getByRole('button', { name: 'Update draft and continue' }).click();
    await expect(page.getByRole('heading', { name: 'Hallucination Guard' })).toBeVisible();
    await page.getByRole('button', { name: 'Review and finalize draft' }).click();
    await expect(page.getByRole('heading', { name: 'Review and Finalize' })).toBeVisible();

    await page.locator('.feedback-container button').first().click();
    const downloadPromise = page.waitForEvent('download');
    await page.getByRole('button', { name: 'Download as word document' }).click();
    const download = await downloadPromise;

    expect(download.suggestedFilename()).toBe('LexIntel_E2E_Complaint.docx');
    expect(apiState.taskCount()).toBe(4);
    expect(submittedBodies.some((body) => body.toUpperCase().includes(complaint.venue))).toBe(true);
  });
}
