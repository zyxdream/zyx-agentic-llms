/* Normal signed-in Workspace DOM only; no credential export or auth bypass. */
const fs=require('fs'),path=require('path'),crypto=require('crypto'),{execFileSync}=require('child_process');
const ROOT=path.resolve(__dirname,'../../..');
const {chromium}=require(path.join(ROOT,'frontend/node_modules/playwright'));
const RUN=path.resolve(process.argv[2]);
const CFG=JSON.parse(fs.readFileSync(path.join(RUN,'scenarios.json')));
const info=JSON.parse(fs.readFileSync(path.join(RUN,'controlled-browser.json')));
let state=JSON.parse(fs.readFileSync(path.join(RUN,'state.json')));
const save=()=>fs.writeFileSync(path.join(RUN,'state.json'),JSON.stringify(state,null,2)+'\n');
const log=x=>{const item={at:new Date().toISOString(),...x};fs.appendFileSync(path.join(RUN,'browser-actions.jsonl'),JSON.stringify(item)+'\n');console.log(JSON.stringify(item));};
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const status=()=>JSON.parse(execFileSync(path.join(ROOT,'.venv/bin/python'),[path.join(__dirname,'status.py'),RUN],{encoding:'utf8'}));
const hash=b=>crypto.createHash('sha256').update(b).digest('hex');
(async()=>{
 const browser=await chromium.connectOverCDP('http://127.0.0.1:'+info.port);
 const page=browser.contexts()[0].pages().find(p=>p.url().startsWith('http://localhost:3010/'));
 if(!page)throw Error('Open Workspace in the signed-in evaluation browser.');
 page.setDefaultTimeout(25000);
 async function expand(){const b=page.getByRole('button',{name:'Expand sidebar',exact:true});if(await b.isVisible())await b.click();}
 async function open(c){await expand();await page.getByRole('button',{name:'Open project '+c.project_name,exact:true}).click();await page.getByRole('button',{name:c.chat_name,exact:true}).click();await page.getByRole('textbox',{name:'Message',exact:true}).waitFor();}
 async function upload(keys,scope){
  if(!keys.length)return;
  const entries=keys.map(k=>CFG.files[k]);
  for(const e of entries)if(hash(fs.readFileSync(path.join(ROOT,e.path)))!==e.sha256)throw Error('Input hash changed: '+e.filename);
  if(scope==='project')await page.locator('.workspace-main').getByRole('button',{name:'Project documents',exact:true}).click();
  const chooser=page.waitForEvent('filechooser');
  if(scope==='project')await page.getByRole('button',{name:'Upload files',exact:true}).click();
  else await page.locator('form.workspace-composer').getByRole('button',{name:'Attach files',exact:true}).click();
  const picker=await chooser;
  const received=page.waitForResponse(r=>/\/upload-groups\/[^/]+\/files(?:\?|$)/.test(r.url())&&r.request().method()==='POST',{timeout:60000});
  const started=Date.now();await picker.setFiles(entries.map(e=>path.join(ROOT,e.path)));
  const response=await received,data=await response.json();
  if(response.status()!==202||data.failed?.length)throw Error('Upload failed: '+JSON.stringify(data));
  log({event:'upload',scope,keys,http_status:response.status(),transfer_ms:Date.now()-started});
 }
 async function ready(id){
  const start=Date.now();
  for(;;){
   const s=status().cases[id],failed=s.preparations.filter(p=>['failed','cancelled'].includes(p.status));
   if(failed.length)throw Error('Preparation failed: '+JSON.stringify(failed));
   if(s.preparations.length&&s.preparations.every(p=>p.status==='ready'))break;
   if(Date.now()-start>20*60*1000)throw Error('Preparation timeout');
   if(Date.now()-start>30000&&(Date.now()-start)%30000<3500)log({event:'preparing',workflow:id,counts:s.preparations.map(p=>({filename:p.filename,status:p.status}))});
   await sleep(3000);
  }
  await page.getByRole('button',{name:'Send',exact:true}).waitFor();
 }
 async function prepare(w){
  let c=state.cases[w.id];
  if(!c){c=state.cases[w.id]={project_name:'Model baseline v1 - '+w.id+' - '+new Date().toISOString(),chat_name:'Benchmark '+w.id};save();}
  if(!c.project_id){
   await expand();await page.getByRole('button',{name:'New project',exact:true}).click();
   await page.getByRole('textbox',{name:'Project name',exact:true}).fill(c.project_name);
   const r=page.waitForResponse(r=>/\/api\/workspace\/projects$/.test(r.url())&&r.request().method()==='POST');
   await page.getByRole('button',{name:'Create project',exact:true}).click();const response=await r,v=await response.json();
   if(response.status()!==201)throw Error(JSON.stringify(v));c.project_id=v.id;save();
  }
  if(!c.chat_id){
   await expand();await page.getByRole('button',{name:'Open project '+c.project_name,exact:true}).click();
   const r=page.waitForResponse(r=>r.url().includes('/projects/'+c.project_id+'/conversations')&&r.request().method()==='POST');
   await page.getByRole('button',{name:'New conversation in '+c.project_name,exact:true}).click();
   const response=await r,v=await response.json();if(response.status()!==201)throw Error(JSON.stringify(v));
   c.chat_id=v.id;save();
  }
  if(!c.renamed){
   await page.getByRole('button',{name:'Open project '+c.project_name,exact:true}).click();
   await page.waitForLoadState('networkidle');
   const actions=page.locator('.selected-conversation').getByRole('button',{name:/^Conversation actions for /});
   await actions.scrollIntoViewIfNeeded();await sleep(500);await actions.click();
   await page.getByRole('menuitem',{name:'Rename',exact:true}).click();await page.getByRole('textbox',{name:'Conversation name',exact:true}).fill(c.chat_name);
   const renamed=page.waitForResponse(r=>r.url().includes('/conversations/'+c.chat_id)&&r.request().method()==='PATCH');
   await page.getByRole('button',{name:'Save name',exact:true}).click();if((await renamed).status()!==200)throw Error('Rename failed');c.renamed=true;save();
  }
  await open(c);
  if(!c.project_uploaded){await upload(w.project_documents,'project');c.project_uploaded=true;save();}
  await ready(w.id);log({event:'workflow_ready',workflow:w.id,project_id:c.project_id,chat_id:c.chat_id});return c;
 }
 async function waitRun(t){
  let last=0;const start=Date.now();
  for(;;){const s=status(),r=s.runs[t.run_id];if(!r)throw Error('Saved run missing');
   if(['completed','failed','cancelled','awaiting_user_confirmation'].includes(r.status)){
    t.status=r.status;t.usage_after=s.cases[t.workflow].usage;t.observed_finished_at=new Date().toISOString();save();log({event:'run_terminal',turn:t.id,...r});
    if(r.status!=='completed')throw Error('Benchmark stopped for '+r.status+'; preserve the attempt and inspect it.');return;
   }
   if(Date.now()-last>30000){last=Date.now();log({event:'progress',turn:t.id,status:r.status,phase:r.phase,last_step:r.last_step,tool_calls:r.tool_counts,tool_errors:r.tool_errors.length});}
   if(Date.now()-start>35*60*1000)throw Error('Observation timeout; do not resubmit the admitted run.');
   await sleep(3000);
  }
 }
 async function capture(c,t){
  await page.reload();await page.getByRole('textbox',{name:'Message',exact:true}).waitFor();await open(c);
  await page.locator('.workspace-main').getByRole('button',{name:'Output',exact:true}).click();
  const panel=page.locator('[role=tabpanel]:visible'),section=panel.getByRole('region',{name:'Output',exact:true});
  await section.getByRole('button',{name:'Edit Markdown',exact:true}).click();
  const markdown=await page.getByRole('textbox',{name:'Output Markdown',exact:true}).inputValue();
  fs.mkdirSync(path.join(RUN,'outputs'),{recursive:true});fs.writeFileSync(path.join(RUN,'outputs',t.id+'.md'),markdown);
  await section.getByRole('button',{name:'Discard edits',exact:true}).click();
  await page.screenshot({path:path.join(RUN,'outputs',t.id+'.png')});
  await panel.getByRole('combobox').selectOption('docx');
  const responsePromise=page.waitForResponse(r=>/\/download(?:\?|$)/.test(r.url())&&r.request().method()==='POST',{timeout:90000});
  const started=Date.now();await panel.getByRole('button',{name:'Download output',exact:true}).click();
  const response=await responsePromise,body=await response.body();
  if(response.status()!==200||body[0]!==80||body[1]!==75)throw Error('DOCX download failed');
  fs.writeFileSync(path.join(RUN,'outputs',t.id+'.docx'),body);
  t.output={characters:markdown.length,words:markdown.trim().split(/\s+/).length,markdown_sha256:hash(markdown),docx_bytes:body.length,docx_sha256:hash(body),download_ms:Date.now()-started,draft_version:status().cases[t.workflow].draft_version};
  t.captured_at=new Date().toISOString();save();log({event:'captured',turn:t.id,...t.output});
 }
 const action=process.argv[3]||'snapshot';
 if(action==='snapshot'){log({url:page.url(),text:(await page.locator('body').innerText()).slice(0,1800)});process.exit(0);}
 const workflows=process.argv[4]?CFG.workflows.filter(w=>w.id===process.argv[4]):CFG.workflows;
 for(const w of workflows){
  const c=await prepare(w);
  if(action==='prepare')continue;
  for(const spec of w.turns){
   let t=state.turns.find(x=>x.id===spec.id);
   if(t?.captured_at)continue;
   await open(c);
   if(!t){t={id:spec.id,workflow:w.id,prompt_sha256:spec.prompt_sha256};state.turns.push(t);save();}
   if(!t.run_id){
    if(spec.attachments.length&&!t.attached){await upload(spec.attachments,'prompt');t.attached=true;save();await ready(w.id);}
    const before=status().cases[w.id];t.usage_before=before.usage;t.draft_version_before=before.draft_version;save();
    const prompt=fs.readFileSync(path.join(RUN,spec.prompt_file),'utf8');if(hash(prompt)!==spec.prompt_sha256)throw Error('Prompt hash changed');
    await page.getByRole('textbox',{name:'Message',exact:true}).fill(prompt);
    const send=page.getByRole('button',{name:'Send',exact:true});await send.waitFor();
    for(let n=0;!await send.isEnabled();n++){if(n>60)throw Error('Send is disabled; inspect preparation blockers.');await sleep(1000);}
    const received=page.waitForResponse(r=>r.url().includes('/conversations/'+c.chat_id+'/runs')&&r.request().method()==='POST',{timeout:60000});
    t.clicked_at=new Date().toISOString();save();await send.click();
    const response=await received,v=await response.json();if(response.status()!==202)throw Error('Submission failed '+JSON.stringify(v));
    const body=response.request().postDataJSON();
    if(['assistant_mode','model','selected_model'].some(k=>k in body))throw Error('Unexpected frontend model field');
    t.run_id=v.run_id;t.admitted_at=new Date().toISOString();t.status=v.status;state.status='running';save();log({event:'submitted',workflow:w.id,turn:t.id,run_id:t.run_id});
   }
   await waitRun(t);await capture(c,t);
  }
  c.status='completed';save();
 }
 state.status=Object.values(state.cases).every(c=>c.status==='completed')&&state.turns.length===CFG.workflows.reduce((n,w)=>n+w.turns.length,0)?'completed':'partial';save();
 log({event:'driver_finished',status:state.status});process.exit(0);
})().catch(e=>{log({event:'driver_error',error:e.message});process.exit(1)});
