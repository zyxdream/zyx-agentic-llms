/* Drive a live suite through the normal signed-in Workspace browser.
 *
 * Usage:
 *   node tests/live/suite_browser.cjs <run-directory> [snapshot|prepare|run] [project-id]
 *
 * The run directory contains scenarios.json, state.json and controlled-browser.json.
 * No credentials are exported. Uploads are limited to the suite's declared files.
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { execFileSync } = require('child_process');

const ROOT = path.resolve(__dirname, '../..');
const { chromium } = require(path.join(ROOT, 'frontend/node_modules/playwright'));
const RUN = path.resolve(process.argv[2]);
const ACTION = process.argv[3] || 'run';
const FILTER = process.argv[4];
const CFG = JSON.parse(fs.readFileSync(path.join(RUN, 'scenarios.json'), 'utf8'));
const INFO = JSON.parse(fs.readFileSync(path.join(RUN, 'controlled-browser.json'), 'utf8'));
let state = JSON.parse(fs.readFileSync(path.join(RUN, 'state.json'), 'utf8'));
const save = () => fs.writeFileSync(path.join(RUN, 'state.json'), JSON.stringify(state, null, 2) + '\n');
const log = item => {
  const entry = { at: new Date().toISOString(), ...item };
  fs.appendFileSync(path.join(RUN, 'browser-actions.jsonl'), JSON.stringify(entry) + '\n');
  console.log(JSON.stringify(entry));
};
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const status = () => JSON.parse(execFileSync(
  path.join(ROOT, '.venv/bin/python'),
  [path.join(__dirname, 'suite_status.py'), RUN], { encoding: 'utf8' }));
const hash = value => crypto.createHash('sha256').update(value).digest('hex');

(async () => {
  const browser = await chromium.connectOverCDP('http://127.0.0.1:' + INFO.port);
  const page = browser.contexts()[0].pages().find(p => p.url().startsWith('http://localhost:3010/'));
  if (!page) throw Error('Open Workspace in the signed-in evaluation browser.');
  page.setDefaultTimeout(30000);

  async function expand() {
    const button = page.getByRole('button', { name: 'Expand sidebar', exact: true });
    if (await button.isVisible()) await button.click();
  }
  async function openProject(project) {
    await expand();
    await page.getByRole('button', { name: 'Open project ' + project.project_name, exact: true }).click();
  }
  async function openChat(project, conversation) {
    await openProject(project);
    const chat = project.chats[conversation];
    await page.getByRole('list', { name: 'Conversations in ' + project.project_name })
      .getByRole('button', { name: chat.name, exact: true }).click();
    await page.getByRole('textbox', { name: 'Message', exact: true }).waitFor();
  }
  async function uploadProject(project) {
    const entries = project.documents.map(name => path.join(ROOT, CFG.input_root, name));
    for (const entry of entries) if (!fs.existsSync(entry)) throw Error('Missing suite input: ' + entry);
    await page.locator('.workspace-main').getByRole('button', { name: 'Project documents', exact: true }).click();
    const chooser = page.waitForEvent('filechooser');
    await page.getByRole('button', { name: 'Upload files', exact: true }).click();
    const picker = await chooser;
    const received = page.waitForResponse(
      response => /\/upload-groups\/[^/]+\/files(?:\?|$)/.test(response.url())
        && response.request().method() === 'POST', { timeout: 600000 });
    await picker.setFiles(entries);
    const response = await received;
    const data = await response.json();
    if (response.status() !== 202 || data.failed?.length) throw Error('Upload failed: ' + JSON.stringify(data));
    log({ event: 'upload', project: project.id, count: entries.length, http_status: response.status() });
  }
  async function ready(project) {
    const started = Date.now();
    for (;;) {
      const snapshot = status().projects[project.id];
      const failed = (snapshot?.preparations || []).filter(item => ['failed', 'cancelled'].includes(item.status));
      if (failed.length) throw Error('Preparation failed: ' + JSON.stringify(failed));
      const expected = project.documents.length;
      if ((snapshot?.preparations || []).length >= expected
          && snapshot.preparations.every(item => item.status === 'ready')) return;
      if (Date.now() - started > 20 * 60 * 1000) throw Error('Preparation timeout for ' + project.id);
      await sleep(3000);
    }
  }
  async function createProject(project) {
    await expand();
    await page.getByRole('button', { name: 'New project', exact: true }).click();
    await page.getByRole('textbox', { name: 'Project name', exact: true }).fill(project.project_name);
    const received = page.waitForResponse(
      response => /\/api\/workspace\/projects$/.test(response.url())
        && response.request().method() === 'POST');
    await page.getByRole('button', { name: 'Create project', exact: true }).click();
    const response = await received;
    const value = await response.json();
    if (response.status() !== 201) throw Error(JSON.stringify(value));
    project.project_id = value.id;
    save();
  }
  async function createChat(project, conversation) {
    await openProject(project);
    const name = project.id + ' - ' + conversation;
    const received = page.waitForResponse(
      response => response.url().includes('/projects/' + project.project_id + '/conversations')
        && response.request().method() === 'POST');
    await page.getByRole('button', { name: 'New conversation in ' + project.project_name, exact: true }).click();
    const response = await received;
    const value = await response.json();
    if (response.status() !== 201) throw Error(JSON.stringify(value));
    project.chats[conversation] = { name, chat_id: value.id, renamed: false };
    save();
    await page.waitForLoadState('networkidle');
    const actions = page.locator('.selected-conversation')
      .getByRole('button', { name: /^Conversation actions for / });
    await actions.scrollIntoViewIfNeeded();
    await sleep(400);
    await actions.click();
    await page.getByRole('menuitem', { name: 'Rename', exact: true }).click();
    await page.getByRole('textbox', { name: 'Conversation name', exact: true }).fill(name);
    const renamed = page.waitForResponse(
      response => response.url().includes('/conversations/' + value.id)
        && response.request().method() === 'PATCH');
    await page.getByRole('button', { name: 'Save name', exact: true }).click();
    if ((await renamed).status() !== 200) throw Error('Conversation rename failed');
    project.chats[conversation].renamed = true;
    save();
  }
  async function waitRun(turn) {
    let last = 0;
    const started = Date.now();
    for (;;) {
      const run = status().runs[turn.run_id];
      if (!run) throw Error('Saved run missing: ' + turn.run_id);
      if (['completed', 'failed', 'cancelled', 'awaiting_user_confirmation'].includes(run.status)) {
        turn.status = run.status;
        turn.finished_at = new Date().toISOString();
        turn.run = run;
        save();
        log({ event: 'run_terminal', turn: turn.id, status: run.status, tool_errors: run.tool_errors.length });
        if (run.status !== 'completed') throw Error('Run ended as ' + run.status);
        return;
      }
      if (Date.now() - last > 30000) {
        last = Date.now();
        log({ event: 'progress', turn: turn.id, status: run.status, phase: run.phase,
              last_step: run.last_step, tool_calls: run.tool_counts, tool_errors: run.tool_errors.length });
      }
      if (Date.now() - started > 40 * 60 * 1000) throw Error('Observation timeout for ' + turn.id);
      await sleep(3000);
    }
  }
  async function submit(project, conversation, spec) {
    const chat = project.chats[conversation];
    let turn = state.turns.find(item => item.id === spec.id);
    if (turn?.captured_at) return;
    await openChat(project, conversation);
    if (!turn) {
      turn = { id: spec.id, project: project.id, conversation, prompt_file: spec.prompt_file };
      state.turns.push(turn);
      save();
    }
    if (!turn.run_id) {
      const prompt = fs.readFileSync(path.join(RUN, spec.prompt_file), 'utf8');
      if (hash(prompt) !== spec.prompt_sha256) throw Error('Prompt hash changed: ' + spec.prompt_file);
      await page.getByRole('textbox', { name: 'Message', exact: true }).fill(prompt);
      const send = page.getByRole('button', { name: 'Send', exact: true });
      await send.waitFor();
      for (let attempt = 0; !await send.isEnabled(); attempt++) {
        if (attempt > 90) throw Error('Send is disabled; inspect preparation blockers.');
        await sleep(1000);
      }
      const received = page.waitForResponse(
        response => response.url().includes('/conversations/' + chat.chat_id + '/runs')
          && response.request().method() === 'POST', { timeout: 60000 });
      await send.click();
      const response = await received;
      const value = await response.json();
      if (response.status() !== 202) throw Error('Submission failed: ' + JSON.stringify(value));
      turn.run_id = value.run_id;
      turn.admitted_at = new Date().toISOString();
      save();
      log({ event: 'submitted', turn: turn.id, project: project.id, conversation, run_id: turn.run_id });
    }
    await waitRun(turn);
    await capture(project, conversation, turn);
  }
  async function capture(project, conversation, turn) {
    await page.reload();
    await page.getByRole('textbox', { name: 'Message', exact: true }).waitFor();
    await openChat(project, conversation);
    const turns = page.locator('.conversation-turn');
    await turns.last().waitFor({ timeout: 30000 });
    const count = await turns.count();
    if (!count) throw Error('No conversation turns to capture for ' + turn.id);
    const last = turns.nth(count - 1);
    const assistant = last.locator('.assistant-message .markdown-body');
    const text = await assistant.innerText();
    const html = await assistant.innerHTML();
    fs.mkdirSync(path.join(RUN, 'outputs'), { recursive: true });
    fs.writeFileSync(path.join(RUN, 'outputs', turn.id + '.md'), text + '\n');
    fs.writeFileSync(path.join(RUN, 'outputs', turn.id + '.html'), html + '\n');
    await page.screenshot({ path: path.join(RUN, 'outputs', turn.id + '.png'), fullPage: false });
    turn.response = { characters: text.length, words: text.trim().split(/\s+/).length, sha256: hash(text) };
    turn.captured_at = new Date().toISOString();
    save();
    log({ event: 'captured', turn: turn.id, words: turn.response.words });
  }

  if (ACTION === 'snapshot') {
    log({ url: page.url(), text: (await page.locator('body').innerText()).slice(0, 2000) });
    process.exit(0);
  }

  const projects = (FILTER ? CFG.projects.filter(item => item.id === FILTER) : CFG.projects);
  for (const spec of projects) {
    let project = state.projects[spec.id];
    if (!project) {
      project = state.projects[spec.id] = {
        id: spec.id,
        project_name: CFG.suite + ' - ' + spec.id + ' - ' + new Date().toISOString(),
        documents: spec.documents,
        chats: {},
      };
      save();
    }
    if (!project.project_id) await createProject(project);
    if (!project.project_uploaded) {
      await uploadProject(project);
      project.project_uploaded = true;
      save();
    }
    await ready(project);
    if (ACTION === 'prepare') continue;
    const conversations = [...new Set(spec.turns.map(turn => turn.conversation))];
    for (const conversation of conversations) {
      if (!project.chats[conversation]) await createChat(project, conversation);
      let failed = false;
      for (const turnSpec of spec.turns.filter(item => item.conversation === conversation)) {
        if (failed) {
          log({ event: 'skipped_after_failure', turn: turnSpec.id, conversation });
          continue;
        }
        try {
          await submit(project, conversation, turnSpec);
        } catch (error) {
          failed = true;
          const turn = state.turns.find(item => item.id === turnSpec.id);
          if (turn) {
            turn.error = error.message;
            turn.failed_at = new Date().toISOString();
            save();
          }
          log({ event: 'turn_error', turn: turnSpec.id, conversation, error: error.message });
        }
      }
    }
  }
  state.status = state.turns.every(turn => turn.captured_at) ? 'completed' : 'partial';
  save();
  log({ event: 'driver_finished', status: state.status });
  process.exit(0);
})().catch(error => {
  log({ event: 'driver_error', error: error.message });
  process.exit(1);
});
