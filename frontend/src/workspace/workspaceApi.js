import axios from 'axios';
import {API_URL} from '../Constants';
import {ensureHeaders} from '../auth/useSetHeaders';

export async function workspaceApi(method, path, data) {
  await ensureHeaders();
  return (await axios({method, url: `${API_URL}workspace/${path}`, data})).data;
}

export function readChoice(key, fallback = '') {
  try { return sessionStorage.getItem(key) ?? fallback; } catch { return fallback; }
}
export function rememberChoice(key, value) {
  try { sessionStorage.setItem(key, value); } catch { /* Private browsing can disable storage. */ }
}
