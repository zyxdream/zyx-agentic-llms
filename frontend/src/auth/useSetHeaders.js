import axios from 'axios';
import {API_URL} from '../Constants';
let identity=null;
axios.defaults.withCredentials=true;
export function setSessionState(value){
 identity=value;
 delete axios.defaults.headers.common.Authorization;
 if(value?.csrf_token)axios.defaults.headers.common['X-Workspace-CSRF']=value.csrf_token;
 else delete axios.defaults.headers.common['X-Workspace-CSRF'];
}
export async function readSession(){
 const response=await axios.get(`${API_URL}auth/session`);
 setSessionState(response.data);return response.data;
}
export async function ensureHeaders(){
 const value=identity?.user?identity:await readSession();
 if(!value.user)throw new Error('Please sign in to Workspace again.');
}
export const setHeaders=ensureHeaders;
