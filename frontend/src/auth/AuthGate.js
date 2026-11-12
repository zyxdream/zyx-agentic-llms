import React,{useCallback,useEffect,useState} from 'react';
import axios from 'axios';
import {API_URL} from '../Constants';
import {readSession,setSessionState} from './useSetHeaders';
import Workspace from '../workspace/Workspace';

export default function AuthGate(){
 const [session,setSession]=useState(null),[email,setEmail]=useState(''),[password,setPassword]=useState('');
 const [error,setError]=useState(''),[busy,setBusy]=useState(false);
 const accept=useCallback(value=>{setSessionState(value);setSession(value);},[]);
 useEffect(()=>{
  let alive=true;
  readSession().then(value=>alive&&accept(value)).catch(()=>alive&&setError('The Workspace backend is unavailable. Check the local services and reload.'));
  const interceptor=axios.interceptors.response.use(response=>response,error=>{
   if(alive&&error.response?.status===401&&!error.config?.url?.includes('auth/login')){
    accept(null);
    readSession().then(value=>alive&&accept(value)).catch(()=>alive&&setError('Refresh Workspace to sign in again.'));
   }
   return Promise.reject(error);
  });
  return()=>{alive=false;axios.interceptors.response.eject(interceptor);};
 },[accept]);
 async function submit(event){
  event.preventDefault();setBusy(true);setError('');
  try{
   const response=await axios.post(`${API_URL}auth/${session?.setup_required?'setup':'login'}`,{email,password});
   accept(response.data);setPassword('');
  }catch(failure){setError(failure.response?.data?.msg||'Sign-in could not be completed.');}
  finally{setBusy(false);}
 }
 async function signOut(){
  try{await axios.post(`${API_URL}auth/logout`);accept(await readSession());}
  catch{setError('Sign-out could not be completed. Please retry.');}
 }
 if(session?.user)return <><Workspace onSignOut={signOut}/>{error&&<p role="alert">{error}</p>}</>;
 return <main className="auth-shell"><section aria-label="Workspace sign in"><h1>Workspace</h1>
 <p>{session?.setup_required?'Create your local Workspace account.':'Sign in to your Workspace account.'}</p>
 {error&&<p role="alert">{error}</p>}
 {!session&&!error&&<p role="status">Connecting to Workspace…</p>}
 {session&&<form onSubmit={submit}>
 <label>Email<input aria-label="Email" type="email" value={email} onChange={event=>setEmail(event.target.value)} autoComplete="username" required/></label>
 <label>Password<input aria-label="Password" type="password" value={password} onChange={event=>setPassword(event.target.value)} autoComplete={session.setup_required?'new-password':'current-password'} minLength={12} maxLength={128} required/></label>
 <p>Use at least 12 characters. Your account is stored only in this project's database.</p>
 <button disabled={busy}>{session.setup_required?'Create local account':'Sign in'}</button>
 </form>}
 </section></main>;
}
