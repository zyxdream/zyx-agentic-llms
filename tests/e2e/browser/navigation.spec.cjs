const {test}=require('../../../frontend/node_modules/@playwright/test');
const {Backend,source,open,showPanel,selectProject,expect}=require('./backend.cjs');
const fs=require('fs');
const path=require('path');
let backend;
test.beforeEach(async({context})=>{backend=new Backend();await backend.mount(context);});
test.afterEach(()=>{for(const h of backend.releases||[])h();expect(backend.unknown).toEqual([]);});
const start=page=>open(page,backend,'a',{panel:null});
async function newProject(page,name){
  await page.getByRole('button',{name:'New project',exact:true}).click();
  await page.getByLabel('Project name',{exact:true}).fill(name);
  await page.getByRole('button',{name:'Create project',exact:true}).click();
}
async function projectMenu(page,name,action){
  await page.getByRole('button',{name:`Project actions for ${name}`,exact:true}).click();
  await page.getByRole('menuitem',{name:action,exact:true}).click();
}

test('chat-first layout has a 280px project tree and panels open only on demand',async({page},info)=>{
  await start(page);
  await expect(page.getByLabel('Assignment',{exact:true})).toBeVisible();
  await expect(page.getByRole('region',{name:'Document',exact:true})).toHaveCount(0);
  await expect(page.getByRole('combobox',{name:'Project',exact:true})).toHaveCount(0);
  expect((await page.getByLabel('Project navigation',{exact:true}).boundingBox()).width).toBe(280);
  const row=await page.getByRole('button',{name:'Chat A',exact:true}).boundingBox();
  const project=await page.getByRole('button',{name:'Open project Project One',exact:true}).boundingBox();
  expect(row.x).toBeGreaterThan(project.x);
  await page.screenshot({path:info.outputPath('chat-first.png')});
  await showPanel(page,'document');
  await expect(page.getByRole('heading',{name:'Original a',exact:true})).toBeVisible();
  await page.getByRole('button',{name:'Close panel',exact:true}).click();
  await expect(page.getByRole('region',{name:'Document',exact:true})).toHaveCount(0);
});

test('a manual project needs no folder and creates no upload or model request',async({page})=>{
  await start(page);await newProject(page,'Personal research');
  await expect(page.getByRole('button',{name:'Open project Personal research',exact:true})).toHaveAttribute('aria-current','true');
  expect(backend.projects.at(-1).name).toBe('Personal research');
  expect(backend.requests.filter(r=>r.path==='upload-groups'||r.path.endsWith('/runs'))).toHaveLength(0);
});

for(const custom of [false,true])test(`native folder naming uses ${custom?'a preserved custom name':'only the folder name'}`,async({page})=>{
  await start(page);
  await page.evaluate(()=>{window.folderEnumerated=false;window.showDirectoryPicker=async()=>({name:'Client files',entries:()=>{window.folderEnumerated=true;throw Error('Do not read files');}});});
  await page.getByRole('button',{name:'New project',exact:true}).click();
  await page.getByRole('button',{name:/Choose folder/}).click();
  await expect(page.getByLabel('Project name',{exact:true})).toHaveValue('Client files');
  if(custom){
    await page.getByLabel('Custom name',{exact:true}).check();
    await page.getByLabel('Project name',{exact:true}).fill('Renewal analysis');
    await page.getByLabel('Use folder name',{exact:true}).check();
    await page.getByLabel('Custom name',{exact:true}).check();
    await expect(page.getByLabel('Project name',{exact:true})).toHaveValue('Renewal analysis');
  }
  await page.getByRole('button',{name:'Create project',exact:true}).click();
  await expect(page.getByRole('button',{name:`Open project ${custom?'Renewal analysis':'Client files'}`,exact:true})).toBeVisible();
  expect(await page.evaluate(()=>window.folderEnumerated)).toBe(false);
  const post=backend.requests.filter(r=>r.path==='projects'&&r.method==='POST').at(-1);
  expect(Object.keys(post.body)).toEqual(['name']);
  expect(backend.count('upload-groups')).toBe(0);
});

test('directory-input fallback uses the root name and discards selected file handles',async({page},info)=>{
  await start(page);await page.evaluate(()=>{window.showDirectoryPicker=undefined;});
  const directory=info.outputPath('Folder from computer');fs.mkdirSync(directory,{recursive:true});fs.writeFileSync(path.join(directory,'notes.txt'),'Never upload this fixture while naming a project');
  await page.getByRole('button',{name:'New project',exact:true}).click();
  await page.getByLabel('Choose project folder',{exact:true}).setInputFiles(directory);
  await expect(page.getByLabel('Project name',{exact:true})).toHaveValue('Folder from computer');
  expect(await page.getByLabel('Choose project folder',{exact:true}).evaluate(input=>input.files.length)).toBe(0);
  await page.getByRole('button',{name:'Create project',exact:true}).click();
  await expect(page.getByRole('button',{name:'Open project Folder from computer',exact:true})).toBeVisible();
  expect(backend.count('upload-groups')).toBe(0);
});

test('cancelled picker preserves manual naming and Escape restores focus',async({page})=>{
  await start(page);
  await page.evaluate(()=>{window.showDirectoryPicker=async()=>{throw new DOMException('Cancelled','AbortError');};});
  const trigger=page.getByRole('button',{name:'New project',exact:true});await trigger.click();
  await page.getByLabel('Project name',{exact:true}).fill('Keep this name');
  await page.getByRole('button',{name:/Choose folder/}).click();
  await expect(page.getByLabel('Project name',{exact:true})).toHaveValue('Keep this name');
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(trigger).toBeFocused();
  expect(backend.count('projects','POST')).toBe(0);
});

test('duplicate names keep creation and rename dialogs editable',async({page})=>{
  await start(page);await newProject(page,'Project One');
  await expect(page.getByRole('alert')).toContainText('already have a project');
  await expect(page.getByLabel('Project name',{exact:true})).toHaveValue('Project One');
  await page.getByRole('button',{name:'Cancel',exact:true}).click();
  await projectMenu(page,'Project Two','Rename');
  await page.getByLabel('Project name',{exact:true}).fill('Project One');
  await page.getByRole('button',{name:'Save name',exact:true}).click();
  await expect(page.getByRole('alert')).toContainText('already have a project');
  await expect(page.getByRole('button',{name:'Open project Project Two',exact:true})).toBeVisible();
});

test('project and conversation rename update the sidebar and toolbar',async({page})=>{
  await start(page);await projectMenu(page,'Project One','Rename');
  await page.getByLabel('Project name',{exact:true}).fill('Claims review');
  await page.getByRole('button',{name:'Save name',exact:true}).click();
  await expect(page.getByRole('button',{name:'Open project Claims review',exact:true})).toHaveAttribute('aria-current','true');
  await page.getByRole('button',{name:'Conversation actions for Chat A',exact:true}).click();
  await page.getByRole('menuitem',{name:'Rename',exact:true}).click();
  await page.getByLabel('Conversation name',{exact:true}).fill('Review policy');
  await page.getByRole('button',{name:'Save name',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Review policy',exact:true})).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading',{name:'Review policy',exact:true})).toBeVisible();
});

test('collapsed projects load lazily and reuse their conversation list',async({page})=>{
  await start(page);expect(backend.count('projects/2/conversations','GET')).toBe(0);
  await page.getByRole('button',{name:'Expand Project Two',exact:true}).click();
  await expect(page.getByRole('button',{name:'Chat C',exact:true})).toBeVisible();
  await page.getByRole('button',{name:'Collapse Project Two',exact:true}).click();
  await page.getByRole('button',{name:'Expand Project Two',exact:true}).click();
  await selectProject(page,'Project Two');
  await expect(page.getByRole('heading',{name:'Chat C',exact:true})).toBeVisible();
  expect(backend.count('projects/2/conversations','GET')).toBe(1);
  await page.reload();
  await expect(page.getByRole('button',{name:'Collapse Project Two',exact:true})).toBeVisible();
});

test('plus creates a conversation in its own project and preserves existing conversations',async({page})=>{
  backend.run('a');await start(page);
  await page.getByRole('button',{name:'New conversation in Project Two',exact:true}).click();
  await expect(page.getByRole('heading',{name:'New conversation',exact:true})).toBeVisible();
  await expect(page.getByRole('button',{name:'Chat C',exact:true})).toBeVisible();
  expect(backend.chats['new-chat'].project_id).toBe(2);
  expect(backend.runs.wf_a.status).toBe('running');
  expect(backend.count('runs/wf_a/cancel','POST')).toBe(0);
});

test('late conversation creation updates its project without undoing navigation',async({page})=>{
  await start(page);
  const h=backend.hold(q=>q.path==='projects/1/conversations'&&q.method==='POST');(backend.releases||=[]).push(h.release);
  await page.getByRole('button',{name:'New conversation in Project One',exact:true}).click();await h.entered;
  await selectProject(page,'Project Two');
  await expect(page.getByRole('heading',{name:'Chat C',exact:true})).toBeVisible();
  h.release();
  await expect(page.getByRole('button',{name:'New conversation',exact:true})).toBeVisible();
  await expect(page.getByRole('heading',{name:'Chat C',exact:true})).toBeVisible();
});

test('deleting a project requires confirmation and retains it when cancelled',async({page})=>{
  await start(page);await projectMenu(page,'Project Two','Delete');
  await page.getByRole('button',{name:'Cancel',exact:true}).click();
  expect(backend.count('projects/2','DELETE')).toBe(0);
  await expect(page.getByRole('button',{name:'Open project Project Two',exact:true})).toBeVisible();
});

for(const work of ['request','preparation'])test(`active ${work} blocks project deletion without cancelling it`,async({page})=>{
  if(work==='request')backend.run('a','awaiting_user_confirmation');else backend.sources=[source('pending','a','summarizing')];
  await start(page);await projectMenu(page,'Project One','Delete');
  await page.getByRole('button',{name:'Delete project',exact:true}).click();
  await expect(page.getByRole('alert')).toContainText('Finish or cancel this work');
  await expect(page.getByRole('button',{name:'Open project Project One',exact:true})).toBeVisible();
  expect(backend.requests.filter(r=>r.path.endsWith('/cancel'))).toHaveLength(0);
});

test('successful deletion selects a remaining project and restores focus safely',async({page})=>{
  await start(page);await projectMenu(page,'Project One','Delete');
  await page.getByRole('button',{name:'Delete project',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Chat C',exact:true})).toBeVisible();
  await expect(page.getByRole('button',{name:'Open project Project One',exact:true})).toHaveCount(0);
  await expect(page.getByRole('button',{name:'New project',exact:true})).toBeFocused();
});

test('a late conversation-list response cannot restore a deleted project',async({page})=>{
  await start(page);
  const h=backend.hold(q=>q.path==='projects/2/conversations'&&q.method==='GET');(backend.releases||=[]).push(h.release);
  await page.getByRole('button',{name:'Expand Project Two',exact:true}).click();await h.entered;
  await projectMenu(page,'Project Two','Delete');
  await page.getByRole('button',{name:'Delete project',exact:true}).click();
  await expect(page.getByRole('button',{name:'Open project Project Two',exact:true})).toHaveCount(0);h.release();
  await expect(page.getByRole('heading',{name:'Chat A',exact:true})).toBeVisible();
  await expect(page.getByRole('button',{name:'Chat C',exact:true})).toHaveCount(0);
});

test('document edits survive panel closing and tab changes; navigation requires confirmation',async({page})=>{
  await start(page);await showPanel(page,'document');
  await page.getByRole('button',{name:'Edit Markdown',exact:true}).click();
  await page.getByLabel('Document Markdown',{exact:true}).fill('# Keep this draft');
  await page.getByRole('button',{name:'Close panel',exact:true}).click();
  await showPanel(page,'sources');await page.getByRole('tab',{name:'Document',exact:true}).click();
  await expect(page.getByLabel('Document Markdown',{exact:true})).toHaveValue('# Keep this draft');
  await page.getByRole('button',{name:'Chat B',exact:true}).click();
  await page.getByRole('button',{name:'Keep editing',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Chat A',exact:true})).toBeVisible();
  await expect(page.getByLabel('Document Markdown',{exact:true})).toHaveValue('# Keep this draft');
  await page.getByRole('button',{name:'Chat B',exact:true}).click();
  await page.getByRole('button',{name:'Discard changes',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Chat B',exact:true})).toBeVisible();
});

test('panel visibility is restored per conversation and across reload',async({page})=>{
  await start(page);await showPanel(page,'document');
  await page.getByRole('button',{name:'Chat B',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Chat B',exact:true})).toBeVisible();
  await expect(page.getByRole('region',{name:'Document',exact:true})).toHaveCount(0);
  await showPanel(page,'sources');
  await page.getByRole('button',{name:'Chat A',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Original a',exact:true})).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading',{name:'Original a',exact:true})).toBeVisible();
});

test('narrow sidebar stays inline and its toggle preserves the conversation',async({page},info)=>{
  await page.setViewportSize({width:390,height:844});await start(page);
  const sidebar=page.getByRole('complementary',{name:'Project navigation',exact:true});
  await expect(sidebar).toBeVisible();
  await expect(page.getByRole('button',{name:'Expand sidebar',exact:true})).toBeVisible();
  await page.getByRole('button',{name:'Expand sidebar',exact:true}).click();
  await expect(page.getByRole('button',{name:'Collapse sidebar',exact:true})).toHaveAttribute('aria-expanded','true');
  await expect.poll(async()=>(await sidebar.boundingBox()).width).toBe(220);
  const bounds=await sidebar.boundingBox(),main=await page.locator('.workspace-main').boundingBox();
  expect(main.x).toBe(bounds.width);
  await expect(page.getByRole('dialog',{name:'Project navigation',exact:true})).toHaveCount(0);
  await expect(page.locator('.sidebar-scrim')).toHaveCount(0);
  await page.getByLabel('Assignment',{exact:true}).fill('Keep this assignment while toggling');
  await page.getByRole('button',{name:'Collapse sidebar',exact:true}).click();
  await expect(page.getByRole('button',{name:'Expand sidebar',exact:true})).toBeFocused();
  await expect.poll(async()=>(await sidebar.boundingBox()).width).toBe(56);
  await expect(page.getByLabel('Assignment',{exact:true})).toHaveValue('Keep this assignment while toggling');
  await showPanel(page,'sources');
  const panel=page.getByRole('dialog',{name:'Workspace inspector',exact:true});
  await expect(page.getByRole('tab',{name:'Sources',exact:true})).toBeFocused();
  expect((await panel.boundingBox()).width).toBe(390);
  await page.keyboard.press('Escape');
  await expect(page.getByRole('button',{name:'Sources',exact:true})).toBeFocused();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({path:info.outputPath('mobile-chat.png')});
});

test('calm palette uses near-black text on light surfaces and keyboard menus work',async({page})=>{
  await start(page);
  expect(await page.evaluate(()=>getComputedStyle(document.body).color)).toBe('rgb(28, 27, 31)');
  expect(await page.evaluate(()=>getComputedStyle(document.documentElement).getPropertyValue('--accent').trim())).toBe('#62516f');
  const menu=page.getByRole('button',{name:'Project actions for Project One',exact:true});await menu.focus();await page.keyboard.press('Enter');
  await expect(page.getByRole('menuitem',{name:'Rename',exact:true})).toBeFocused();
  await page.keyboard.press('ArrowDown');await expect(page.getByRole('menuitem',{name:'Delete',exact:true})).toBeFocused();
  await page.keyboard.press('Escape');await expect(menu).toBeFocused();
  await menu.click();await page.setViewportSize({width:1200,height:720});
  await expect(page.getByRole('menu')).toHaveCount(0);
});

test('rename dialog keeps focus while the narrow sidebar is expanded',async({page})=>{
  await page.setViewportSize({width:390,height:844});await start(page);
  await page.getByRole('button',{name:'Expand sidebar',exact:true}).click();
  await projectMenu(page,'Project One','Rename');
  const dialog=page.getByRole('dialog',{name:'Rename project',exact:true});
  await expect(page.getByLabel('Project name',{exact:true})).toBeFocused();
  for(let index=0;index<8;index++){
    await page.keyboard.press('Tab');
    expect(await dialog.evaluate(element=>element.contains(document.activeElement))).toBe(true);
  }
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole('complementary',{name:'Project navigation',exact:true})).toBeVisible();
  await expect(page.getByRole('button',{name:'Project actions for Project One',exact:true})).toBeFocused();
  await page.getByRole('button',{name:'Collapse sidebar',exact:true}).click();
  await expect(page.getByRole('button',{name:'Expand sidebar',exact:true})).toBeFocused();
});

test('desktop sidebar collapses to a persistent rail and remembers the choice',async({page},info)=>{
  await start(page);
  const sidebar=page.getByRole('complementary',{name:'Project navigation',exact:true});
  await page.getByLabel('Assignment',{exact:true}).fill('Do not reset this text');
  const reads=backend.count('conversations/a','GET');
  await page.getByRole('button',{name:'Collapse sidebar',exact:true}).click();
  await expect.poll(async()=>(await sidebar.boundingBox()).width).toBe(64);
  await expect(sidebar).toBeVisible();
  await expect(page.getByRole('button',{name:'New project',exact:true})).toBeVisible();
  expect((await page.locator('.workspace-main').boundingBox()).x).toBe(64);
  await expect(page.getByLabel('Assignment',{exact:true})).toHaveValue('Do not reset this text');
  expect(backend.count('conversations/a','GET')).toBe(reads);
  await page.screenshot({path:info.outputPath('sidebar-collapsed.png')});
  await page.getByRole('button',{name:'Expand sidebar',exact:true}).press('Enter');
  await expect.poll(async()=>(await sidebar.boundingBox()).width).toBe(280);
  await expect(page.getByRole('button',{name:'Collapse sidebar',exact:true})).toBeFocused();
  await page.screenshot({path:info.outputPath('sidebar-expanded.png')});
  await page.getByRole('button',{name:'Chat B',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Chat B',exact:true})).toBeVisible();
  await expect(page.getByRole('button',{name:'Collapse sidebar',exact:true})).toBeVisible();
  await page.getByRole('button',{name:'Collapse sidebar',exact:true}).click();
  await page.reload();
  await expect(page.getByRole('button',{name:'Expand sidebar',exact:true})).toHaveAttribute('aria-expanded','false');
  await expect(page.getByRole('heading',{name:'Chat B',exact:true})).toBeVisible();
});

test('an active request remains cancellable from a collapsed sidebar layout',async({page})=>{
  backend.run('a');await start(page);
  await page.getByRole('button',{name:'Collapse sidebar',exact:true}).click();
  await expect(page.getByRole('status')).toContainText('wf_a');
  const stop=page.locator('.workspace-composer').getByRole('button',{name:'Cancel request',exact:true});
  await expect(stop).toBeEnabled();await stop.click();
  await expect(page.getByRole('status')).toHaveCount(0);
  await expect(page.getByRole('button',{name:'Expand sidebar',exact:true})).toBeVisible();
});

test('populated document and source panels fit the reading layout',async({page},info)=>{
  const run=backend.run('a','completed');
  run.query='Summarize the renewal terms and draft a short review brief.';
  run.response='## Renewal analysis\n\nThe policy summary is ready. Review the exclusions and the proposed wording before sharing it.\n\n- Compare renewal terms\n- Confirm the supporting documents';
  backend.documents.a={...backend.documents.a,title:'Renewal brief',markdown:'# Renewal brief\n\nA concise comparison of the proposed terms and supporting documents.\n\n| Item | Review |\n|---|---|\n| Coverage | Confirm limits |\n| Exclusions | Clarify wording |'};
  backend.sources=[source('policy-summary'),source('renewal-notes')];
  await start(page);await showPanel(page,'document');
  await expect(page.getByRole('heading',{name:'Renewal brief',level:2,exact:true})).toBeVisible();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(1280);
  await page.screenshot({path:info.outputPath('document-inspector.png')});
  await page.getByRole('tab',{name:'Sources',exact:true}).click();
  await expect(page.locator('.source')).toHaveCount(2);
  await page.screenshot({path:info.outputPath('sources-inspector.png')});
  await page.getByRole('button',{name:'New project',exact:true}).click();
  await expect(page.getByLabel('Choose project folder',{exact:true})).toBeHidden();
  await page.screenshot({path:info.outputPath('new-project-dialog.png')});
  await page.getByRole('button',{name:'Cancel',exact:true}).click();
});
