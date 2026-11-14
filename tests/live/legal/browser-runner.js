const base=new URL('./',import.meta.url).pathname;
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const L=window.liveLegal ||= {projects:[],runs:[]};
export async function connect(){
 const s=await(await fetch('/api/auth/session')).json();if(!s.user)throw Error('Sign in to Workspace first.');
 L.csrf=s.csrf_token;
 L.api=async(path,body,method='POST')=>{const r=await fetch('/api/workspace/'+path,{method,headers:{'Content-Type':'application/json','X-Workspace-CSRF':L.csrf},...(body===undefined?{}:{body:JSON.stringify(body)})});const v=await r.json();if(!r.ok)throw Error(r.status+' '+JSON.stringify(v));return v;};
 return {signedIn:true};
}
export async function startCase(name){
 if(!L.api)await connect();
 const cfg=(await(await fetch(base+'scenarios.json')).json())[name];
 const state=L[name]={phase:'creating',started:Date.now(),transfers:[],runs:[]};
 try{
 const p=await L.api('projects',{name:cfg.project_name+' '+new Date().toISOString()});
 const c=await L.api('projects/'+p.id+'/conversations',{name:name==='commercial'?'40-file contract reconciliation and demand':'Overtime workbook and legal memorandum'});
 state.project=p.id;state.chat=c.id;L.projects.push({id:p.id,chat:c.id,case:name});
 for(const [scope,paths] of [['matter',cfg.project_files],['chat',cfg.prompt_files]]){
  state.phase='uploading '+scope;
  const files=await Promise.all(paths.map(async rel=>{const r=await fetch(base+rel);if(!r.ok)throw Error('Missing fixture '+rel);return {file:new File([await r.blob()],rel.split('/').at(-1)),id:crypto.randomUUID()};}));
  const g=await L.api('upload-groups',{scope,scopeId:scope==='matter'?p.id:c.id,selectionId:crypto.randomUUID(),documents:files.map(f=>({client_upload_id:f.id,filename:f.file.name,file_size:f.file.size}))});
  for(let i=0;i<files.length;i+=5){const form=new FormData();const chunk=files.slice(i,i+5);form.append('clientUploadIds',JSON.stringify(chunk.map(f=>f.id)));chunk.forEach(f=>form.append('files',f.file));const t=Date.now();const r=await fetch('/api/workspace/upload-groups/'+g.group_id+'/files',{method:'POST',headers:{'X-Workspace-CSRF':L.csrf},body:form});const v=await r.json();if(!r.ok||v.failed?.length)throw Error(JSON.stringify(v));state.transfers.push({scope,count:chunk.length,ms:Date.now()-t});}
 }
 state.phase='preparing';state.uploaded=Date.now();
 for(let i=0;i<600;i++){const cat=await L.api('document-preparation?conversation_id='+c.id,undefined,'GET');state.catalog=cat.documents.map(d=>({id:d.document_id,name:d.filename,status:d.status,error:d.error,scope:d.scope}));const failed=cat.documents.filter(d=>['failed','cancelled'].includes(d.status));if(failed.length)throw Error('Preparation failures '+JSON.stringify(failed));if(cat.documents.length===cfg.project_files.length+cfg.prompt_files.length&&cat.documents.every(d=>d.status==='ready')){state.ready=Date.now();await submit(name,cfg.initial_prompt);return;}await sleep(3000);}
 throw Error('Preparation timeout');
 }catch(e){state.phase='error';state.error=String(e);}
}
export async function submit(name,query){
 const state=L[name];const cat=await L.api('document-preparation?conversation_id='+state.chat,undefined,'GET');const id='wf_live_'+crypto.randomUUID();
 const r=await L.api('conversations/'+state.chat+'/runs',{query,run_id:id,excludedDocumentIds:[],chatFiles:cat.documents.filter(d=>d.scope==='chat').map(d=>({name:d.document_id,filename:d.filename})),promptAttachments:cat.documents.filter(d=>d.scope==='chat'&&!d.committed).map(d=>({file_id:d.document_id,filename:d.filename}))});
 const entry={id,chat:state.chat,project:state.project,case:name,start:Date.now(),status:r.status};state.runs.push(entry);L.runs.push(entry);state.phase='running';state.current=id;
}
export async function revision(name){const cfg=(await(await fetch(base+'scenarios.json')).json())[name];await submit(name,cfg.revision_prompt);}
export async function snapshot(){
 for(const entry of L.runs){try{const r=await L.api('runs/'+entry.id,undefined,'GET');entry.status=r.status;entry.info=r.info?.status;entry.progress=r.info?.agent;entry.error=r.error;entry.value=r.value;if(['completed','failed','cancelled'].includes(r.status)&&!entry.finished)entry.finished=Date.now();}catch(e){entry.pollError=String(e);}}
 return {projects:L.projects,runs:L.runs,...Object.fromEntries(['commercial','employment'].filter(k=>L[k]).map(k=>[k,L[k]]))};
}
