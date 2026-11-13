const {test}=require('../../../frontend/node_modules/@playwright/test');
const {Backend,source,open,showPanel,choose,selectProject,send,expect}=require('./backend.cjs');
const fs=require('fs');
const path=require('path');
let backend;
test.beforeEach(async({context})=>{backend=new Backend();await backend.mount(context);});
test.afterEach(()=>{for(const h of backend.releases||[])h();expect(backend.unknown).toEqual([]);});
const start=page=>open(page,backend,'a',{panel:null});
async function promptFile(page,name='note.txt') {
  const chooser=page.waitForEvent('filechooser');
  await page.locator('.composer-options').getByRole('button',{name:'Attach files',exact:true}).click();
  await (await chooser).setFiles({name,mimeType:'text/plain',buffer:Buffer.from('A document for this message.')});
  await expect(page.getByRole('button',{name:'Attachments',exact:true})).toBeVisible();
}
async function folder(page,info,files) {
  const directory=info.outputPath('Research');
  for(const [name,contents] of Object.entries(files)) {
    fs.mkdirSync(path.dirname(path.join(directory,name)),{recursive:true});
    fs.writeFileSync(path.join(directory,name),contents);
  }
  await showPanel(page,'project-documents');
  const chooser=page.waitForEvent('filechooser');
  await page.getByRole('button',{name:'Upload folder',exact:true}).click();
  await (await chooser).setFiles(directory);
  return page.getByRole('dialog',{name:'Upload folder',exact:true});
}

test('toolbar and input panels distinguish project documents, attachments and output',async({page})=>{
  backend.sources=[source('shared',null,'ready',{scope:'matter',project:1}),source('attached')];
  await start(page);
  await expect(page.getByRole('button',{name:'Sources',exact:true})).toHaveCount(0);
  await expect(page.getByRole('button',{name:'Document',exact:true})).toHaveCount(0);
  await expect(page.getByRole('button',{name:'Project documents',exact:true})).toHaveText('Project documents1');
  await showPanel(page,'project-documents');
  await expect(page.locator('.source:visible')).toHaveCount(1);await expect(page.locator('.source:visible')).toContainText('shared.txt');
  await expect(page.getByRole('combobox',{name:'Upload to'})).toHaveCount(0);
  await showPanel(page,'attachments');
  await expect(page.locator('.source:visible')).toHaveCount(1);await expect(page.locator('.source:visible')).toContainText('attached.txt');
  await showPanel(page,'output');
  await expect(page.getByRole('heading',{name:'Original a',exact:true})).toBeVisible();
  await expect(page.getByRole('tab',{name:'Output',exact:true})).toHaveAttribute('aria-selected','true');
});

test('paperclip opens files directly and submitted attachments survive reload and follow-ups',async({page})=>{
  backend.autoComplete=true;await start(page);await promptFile(page);
  await expect(page.getByRole('menu')).toHaveCount(0);
  await expect(page.locator('.workspace-inspector:visible')).toHaveCount(0);
  await expect(page.locator('.attachment-chip')).toContainText('note.txt');
  await send(page,'Read the attached file');
  await expect(page.locator('.message-attachments')).toContainText('note.txt');
  await page.reload();
  await expect(page.locator('.message-attachments')).toContainText('note.txt');
  await expect(page.locator('.attachment-chip')).toHaveCount(0);
  await send(page,'Explain that further');
  await expect.poll(()=>backend.lastSubmission?.promptAttachments).toEqual([]);
  expect(backend.lastSubmission.chatFiles).toHaveLength(1);
  await page.locator('.message-attachments').getByRole('button',{name:'note.txt',exact:true}).click();
  await expect(page.getByRole('region',{name:'File text',exact:true})).toContainText('Original source text');
});

test('removing a pending attachment fences a late transfer and excludes it after reload',async({page})=>{
  const h=backend.hold(q=>/^upload-groups\/.*\/files$/.test(q.path));(backend.releases||=[]).push(h.release);
  await start(page);await promptFile(page);await h.entered;
  await page.getByRole('button',{name:'Remove attachment note.txt',exact:true}).click();
  await expect(page.locator('.attachment-chip')).toHaveCount(0);
  h.release();
  await expect.poll(()=>backend.sources[0]?.status).toBe('cancelled');
  await page.reload();
  await send(page,'Continue without the file');
  await expect.poll(()=>backend.lastSubmission?.promptAttachments).toEqual([]);
  expect(backend.lastSubmission.chatFiles).toEqual([]);
});

test('blockers lead to the correct input view and exclusions apply only to the next request',async({page})=>{
  backend.autoComplete=true;
  backend.sources=[source('project-failed',null,'failed',{scope:'matter',project:1}),source('prompt-failed','a','failed')];
  await start(page);
  await page.getByLabel('Message',{exact:true}).fill('Continue');
  await expect(page.getByRole('button',{name:'Send',exact:true})).toBeDisabled();
  await page.getByRole('button',{name:'View project documents',exact:true}).click();
  await page.getByRole('checkbox',{name:'Use project-failed.txt for next message'}).uncheck();
  await page.getByRole('button',{name:'View attachments',exact:true}).click();
  await page.getByRole('checkbox',{name:'Use prompt-failed.txt for next message'}).uncheck();
  await page.getByRole('button',{name:'Send',exact:true}).click();
  await expect.poll(()=>backend.lastSubmission?.excludedDocumentIds.sort()).toEqual(['project-failed','prompt-failed']);
  await expect(page.getByRole('checkbox',{name:'Use prompt-failed.txt for next message'})).toBeChecked();
});

test('folder review imports selected nested files into the persistent project library',async({page},info)=>{
  await start(page);
  const dialog=await folder(page,info,{'a.txt':'A','nested/b.csv':'x,y\n1,2','logo.png':'unsupported'});
  await expect(dialog.getByText('1 skipped file')).toBeVisible();
  await expect(dialog.getByText('nested/b.csv',{exact:true})).toBeVisible();
  await dialog.getByLabel('a.txt',{exact:true}).uncheck();
  expect(backend.count('upload-groups','POST')).toBe(0);
  await dialog.getByRole('button',{name:'Upload 1 file',exact:true}).click();
  await expect(page.locator('.source:visible')).toContainText('b.csv');
  await expect(page.getByRole('heading',{name:'Folder · Research',exact:true})).toBeVisible();
  const admission=backend.requests.find(q=>q.path==='upload-groups'&&q.method==='POST').body;
  expect(admission.scope).toBe('matter');expect(admission.scopeId).toBe('1');expect(admission.folderName).toBe('Research');
  expect(admission.documents).toHaveLength(1);expect(admission.documents[0].relative_path).toBe('nested/b.csv');
  expect(JSON.stringify(admission)).not.toContain(info.outputDir);
  await choose(page,'Chat B');await expect(page.locator('.source:visible')).toContainText('b.csv');
  await selectProject(page,'Project Two');await showPanel(page,'project-documents');
  await expect(page.locator('.source:visible')).toHaveCount(0);
});

test('cancelled folder review does not reserve or upload files',async({page},info)=>{
  await start(page);const dialog=await folder(page,info,{'a.txt':'A'});
  await dialog.getByRole('button',{name:'Cancel',exact:true}).click();
  expect(backend.count('upload-groups','POST')).toBe(0);
  await expect(page.getByRole('button',{name:'Upload folder',exact:true})).toBeFocused();
});

test('partial folder transfer failure keeps successful files and exposes a retry for the failed file',async({page},info)=>{
  backend.hooks.push(q=>{
    if (!/^upload-groups\/.*\/files$/.test(q.path)) return;
    const members=backend.groups[q.path.split('/')[1]].documents;
    const ready=members.find(d=>d.filename==='a.txt'),failed=members.find(d=>d.filename==='b.txt');
    ready.status='ready';ready.needs_reupload=false;
    failed.status='failed';failed.error='Transfer failed. Retry this file.';
    return backend.json(q,{failed:[{msg:'Transfer failed. Retry this file.'}]},202);
  });
  await start(page);const dialog=await folder(page,info,{'a.txt':'A','b.txt':'B'});
  await dialog.getByRole('button',{name:'Upload 2 files',exact:true}).click();
  await expect(page.locator('.source:visible')).toHaveCount(2);
  await expect(page.locator('.source:visible').filter({hasText:'b.txt'})).toContainText('Transfer failed');
  await page.locator('.source:visible').filter({hasText:'b.txt'}).getByRole('button',{name:'Retry preparation',exact:true}).click();
  await expect.poll(()=>backend.sources.filter(d=>d.status==='ready').length).toBe(2);
  expect(backend.count('upload-groups','POST')).toBe(1);
});

test('folder review enforces project capacity before admission',async({page},info)=>{
  backend.sources=Array.from({length:30},(_,i)=>source(`existing-${i}`,null,'ready',{scope:'matter',project:1}));
  await start(page);const dialog=await folder(page,info,{'a.txt':'A'});
  await expect(dialog.getByRole('alert')).toContainText('30 files');
  await expect(dialog.getByRole('button',{name:'Upload 1 file',exact:true})).toBeDisabled();
  expect(backend.count('upload-groups','POST')).toBe(0);
});

test('unsupported and empty folder entries are reported without an upload',async({page},info)=>{
  await start(page);const dialog=await folder(page,info,{'empty.txt':'','image.png':'X'});
  await expect(dialog.getByText('2 skipped files')).toBeVisible();
  await expect(dialog.getByRole('button',{name:'Upload 0 files',exact:true})).toBeDisabled();
  expect(backend.count('upload-groups','POST')).toBe(0);
});

test('individual project uploads cannot become prompt attachments',async({page})=>{
  await start(page);await showPanel(page,'project-documents');
  const chooser=page.waitForEvent('filechooser');await page.getByRole('button',{name:'Upload files',exact:true}).click();
  await (await chooser).setFiles({name:'shared.txt',mimeType:'text/plain',buffer:Buffer.from('Shared')});
  await expect(page.locator('.source:visible')).toContainText('shared.txt');
  await expect.poll(()=>backend.sources[0]?.status).toBe('ready');
  await send(page);
  await expect.poll(()=>backend.lastSubmission?.promptAttachments).toEqual([]);expect(backend.lastSubmission.chatFiles).toEqual([]);
});

test('output edits survive both input views and closing the inspector',async({page})=>{
  backend.sources=[source('attached')];await start(page);await showPanel(page,'output');
  await page.getByRole('button',{name:'Edit Markdown',exact:true}).click();await page.getByLabel('Output Markdown').fill('# Unsaved');
  await showPanel(page,'project-documents');await showPanel(page,'attachments');await showPanel(page,'output');
  await expect(page.getByLabel('Output Markdown')).toHaveValue('# Unsaved');
  await page.getByRole('button',{name:'Hide right panel',exact:true}).click();await showPanel(page,'output');
  await expect(page.getByLabel('Output Markdown')).toHaveValue('# Unsaved');
});

test('historical attachment names render without rewriting saved metadata',async({page})=>{
  const run=backend.run('a','completed');run.attachments=[{fileId:'old-a',fileName:'Legacy A.pdf'},{file_id:'old-b',filename:'Legacy B.txt'},{file_id:'old-c',file_name:'Legacy C.csv'}];
  const before=JSON.stringify(run.attachments);await start(page);
  for(const name of ['Legacy A.pdf','Legacy B.txt','Legacy C.csv']) await expect(page.locator('.message-attachments').getByRole('button',{name,exact:true})).toBeVisible();
  expect(JSON.stringify(run.attachments)).toBe(before);
});

for(const [old,view] of [['sources','Project documents'],['document','Output']]) test(`restores the old ${old} preference as ${view}`,async({page})=>{
  await page.addInitScript(old=>sessionStorage.setItem('workspace.panel:a',JSON.stringify({open:true,tab:old})),old);
  await start(page);await expect(page.locator('.workspace-inspector:not([hidden]) .inspector-heading h2')).toHaveText(view);
});

test('opening input panels refreshes one catalog without adding status polling loops',async({page})=>{
  backend.sources=[source('attached','a','ready',{committed:true})];backend.run('a');await start(page);
  const before=backend.count('document-preparation');
  await showPanel(page,'project-documents');await showPanel(page,'attachments');await showPanel(page,'output');
  expect(backend.count('document-preparation')-before).toBe(2);
  await page.waitForTimeout(3300);
  expect(backend.count('document-preparation')-before).toBe(2);
  expect(backend.count('runs/wf_a','GET')).toBeLessThanOrEqual(2);
});

test('a delayed input-panel refresh cannot put its failure into another conversation',async({page})=>{
  await start(page);
  const h=backend.hold(q=>q.path==='document-preparation'&&q.url.searchParams.get('conversation_id')==='a',q=>backend.json(q,{msg:'Old project document failure'},503));
  (backend.releases||=[]).push(h.release);
  await showPanel(page,'project-documents');await h.entered;
  await page.getByRole('button',{name:'Chat B',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Chat B',exact:true})).toBeVisible();
  h.release();
  await page.getByLabel('Message',{exact:true}).fill('Work in B');
  await expect(page.getByRole('button',{name:'Send',exact:true})).toBeEnabled();
  await expect(page.getByText('Old project document failure',{exact:true})).toHaveCount(0);
});

test('project documents can be uploaded before creating a conversation',async({page})=>{
  await start(page);await page.getByRole('button',{name:'New project',exact:true}).click();
  await page.getByLabel('Project name',{exact:true}).fill('Knowledge library');
  await page.getByRole('button',{name:'Create project',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Knowledge library',exact:true})).toBeVisible();
  await showPanel(page,'project-documents');
  const chooser=page.waitForEvent('filechooser');await page.getByRole('button',{name:'Upload files',exact:true}).click();
  await (await chooser).setFiles({name:'knowledge.txt',mimeType:'text/plain',buffer:Buffer.from('Knowledge')});
  await expect(page.locator('.source:visible')).toContainText('knowledge.txt');
  const admission=backend.requests.find(q=>q.path==='upload-groups'&&q.method==='POST').body;
  expect(admission.scope).toBe('matter');expect(admission.scopeId).toBe('3');
  expect(Object.keys(backend.chats)).toHaveLength(3);
});

test('narrow windows tuck away tabs and restore them without covering the conversation',async({page})=>{
  await start(page);await showPanel(page,'project-documents');await showPanel(page,'output');
  await page.setViewportSize({width:390,height:844});
  await expect(page.getByLabel('Message',{exact:true})).toBeVisible();
  await expect(page.locator('.workspace-inspector:visible')).toHaveCount(0);
  await expect(page.getByText('Widen the window to show the right panel beside the conversation.')).toBeVisible();
  await page.setViewportSize({width:1280,height:800});
  await expect(page.getByRole('tab',{name:'Output',exact:true})).toHaveAttribute('aria-selected','true');
  await expect(page.getByRole('tab',{name:'Project documents',exact:true})).toBeVisible();
});

test('tabs deduplicate, close individually and retain unsaved output when reopened',async({page})=>{
  await start(page);await showPanel(page,'project-documents');await showPanel(page,'output');await showPanel(page,'output');
  await expect(page.getByRole('tab')).toHaveCount(2);
  await page.getByRole('button',{name:'Edit Markdown',exact:true}).click();await page.getByLabel('Output Markdown').fill('# Keep this edit');
  await page.getByRole('button',{name:'Close Output tab',exact:true}).click();
  await expect(page.getByRole('tab',{name:'Project documents',exact:true})).toHaveAttribute('aria-selected','true');
  await showPanel(page,'output');await expect(page.getByLabel('Output Markdown')).toHaveValue('# Keep this edit');
  await page.getByRole('button',{name:'Hide right panel',exact:true}).click();
  await expect(page.getByLabel('Message',{exact:true})).toBeVisible();
  await showPanel(page,'project-documents');await page.getByRole('tab',{name:'Output',exact:true}).click();
  await expect(page.getByLabel('Output Markdown')).toHaveValue('# Keep this edit');
});

test('divider keyboard and pointer resizing preserve messages and saved preferred widths',async({page})=>{
  await start(page);await showPanel(page,'output');await page.getByLabel('Message',{exact:true}).fill('Keep my message');
  const left=page.getByRole('separator',{name:'Resize projects',exact:true});
  await left.press('ArrowRight');await expect(left).toHaveAttribute('aria-valuenow','290');
  const right=page.getByRole('separator',{name:'Resize right panel',exact:true});
  const bounds=await right.boundingBox();await page.mouse.move(bounds.x+2,bounds.y+80);await page.mouse.down();await page.mouse.move(bounds.x-38,bounds.y+80);await page.mouse.up();
  await expect(right).toHaveAttribute('aria-valuenow','440');
  await expect(page.getByLabel('Message',{exact:true})).toHaveValue('Keep my message');
  await page.reload();await expect(page.getByRole('separator',{name:'Resize projects',exact:true})).toHaveAttribute('aria-valuenow','290');
  await expect(page.getByRole('separator',{name:'Resize right panel',exact:true})).toHaveAttribute('aria-valuenow','440');
  await expect(page.getByRole('tab',{name:'Output',exact:true})).toHaveAttribute('aria-selected','true');
});

test('tab switching keeps independent file readers and keyboard selection',async({page})=>{
  backend.sources=[source('shared',null,'ready',{scope:'matter',project:1}),source('attached')];
  await start(page);await showPanel(page,'project-documents');await page.getByRole('button',{name:'Read file',exact:true}).click();
  await showPanel(page,'attachments');await page.getByRole('button',{name:'Read file',exact:true}).click();
  const project=page.getByRole('tab',{name:'Project documents',exact:true});await project.click();
  await expect(page.getByRole('region',{name:'File text'})).toContainText('shared.txt');
  await project.press('ArrowRight');await expect(page.getByRole('tab',{name:'Attachments',exact:true})).toBeFocused();
  await expect(page.getByRole('region',{name:'File text'})).toContainText('attached.txt');
});

test('Enter adds a new line and Shift+Enter sends with a compact button hint',async({page})=>{
  await start(page);
  const input=page.getByLabel('Message',{exact:true});
  await input.fill('First line');await input.press('Enter');await input.press('a');
  await expect(input).toHaveValue('First line\na');
  expect(backend.count('conversations/a/runs','POST')).toBe(0);
  await expect(page.getByText('Enter to send · Shift + Enter for a new line',{exact:true})).toHaveCount(0);
  await expect(page.locator('.send-control .send-shortcut')).toHaveText('shift+enter');
  await input.press('Shift+Enter');
  await expect.poll(()=>backend.lastSubmission?.query).toBe('First line\na');
  expect(backend.count('conversations/a/runs','POST')).toBe(1);
});
