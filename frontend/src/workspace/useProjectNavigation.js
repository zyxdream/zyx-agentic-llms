import {useCallback, useEffect, useRef, useState} from 'react';
import {readChoice, rememberChoice, workspaceApi as api} from './workspaceApi';

function savedExpansion() {
  try { const value = JSON.parse(readChoice('workspace.expanded', '[]')); return Array.isArray(value) ? value.map(String) : []; } catch { return []; }
}

export function useProjectNavigation({beforeNavigate, onError}) {
  const [projects, setProjects] = useState([]), [project, setProject] = useState(''), [chat, setChat] = useState('');
  const [lists, setLists] = useState({}), [expanded, setExpanded] = useState(savedExpansion), [loading, setLoading] = useState({}), [listErrors, setListErrors] = useState({}), [creating, setCreating] = useState({});
  const live = useRef(true), cache = useRef({}), generations = useRef({}), requests = useRef(new Map()), selection = useRef({project: '', chat: '', epoch: 0});
  const projectRef = useRef([]), gate = useRef(beforeNavigate), error = useRef(onError);
  gate.current = beforeNavigate; error.current = onError; projectRef.current = projects;
  const activate = useCallback((pid, cid = '') => {
    selection.current = {project: String(pid || ''), chat: cid, epoch: selection.current.epoch + 1};
    setProject(String(pid || '')); setChat(cid);
    rememberChoice('workspace.project', String(pid || '')); rememberChoice('workspace.conversation', cid);
  }, []);
  const expand = useCallback(pid => setExpanded(v => v.includes(String(pid)) ? v : [...v, String(pid)]), []);
  const load = useCallback(async (pid, force = false) => {
    pid = String(pid);
    if (!force && cache.current[pid]) return cache.current[pid];
    if (!force && requests.current.has(pid)) return requests.current.get(pid);
    const generation = generations.current[pid] = (generations.current[pid] || 0) + 1;
    setLoading(v => ({...v, [pid]: true})); setListErrors(v => ({...v, [pid]: ''}));
    const work = (async () => {
      try {
        const value = await api('get', `projects/${pid}/conversations`);
        if (!live.current || generations.current[pid] !== generation) return null;
        cache.current[pid] = value.conversations;
        setLists(v => ({...v, [pid]: value.conversations}));
        return value.conversations;
      } catch (failure) {
        if (live.current && generations.current[pid] === generation) setListErrors(v => ({...v, [pid]: 'Could not load conversations.'}));
        throw failure;
      } finally {
        if (live.current && generations.current[pid] === generation) setLoading(v => ({...v, [pid]: false}));
        if (requests.current.get(pid) === work) requests.current.delete(pid);
      }
    })();
    requests.current.set(pid, work);
    return work;
  }, []);
  useEffect(() => {
    live.current = true;
    const rememberedProject = readChoice('workspace.project'), rememberedChat = readChoice('workspace.conversation');
    api('get', 'projects').then(async value => {
      if (!live.current) return;
      projectRef.current = value.projects; setProjects(value.projects);
      const pid = String(value.projects.find(p => String(p.id) === rememberedProject)?.id || value.projects[0]?.id || '');
      const expandedIds = savedExpansion().map(String).filter(id => value.projects.some(p => String(p.id) === id));
      setExpanded([...new Set([...expandedIds, ...(pid ? [pid] : [])])]);
      if (!pid) return;
      activate(pid);
      const epoch = selection.current.epoch;
      expandedIds.filter(id => id !== pid).forEach(id => load(id).catch(() => {}));
      const rows = await load(pid);
      if (live.current && rows && selection.current.epoch === epoch) activate(pid, rows.find(c => c.id === rememberedChat)?.id || rows[0]?.id || '');
    }).catch(failure => { if (live.current) error.current(failure); });
    return () => { live.current = false; };
  }, [activate, load]);
  useEffect(() => rememberChoice('workspace.expanded', JSON.stringify(expanded)), [expanded]);

  const selectProject = useCallback(pid => gate.current(async () => {
    pid = String(pid); activate(pid); expand(pid);
    const epoch = selection.current.epoch;
    try {
      const rows = await load(pid);
      if (live.current && rows && selection.current.epoch === epoch) activate(pid, rows[0]?.id || '');
    } catch (failure) { if (live.current && selection.current.epoch === epoch) error.current(failure); }
  }), [activate, expand, load]);
  const selectChat = useCallback((pid, cid) => gate.current(() => { expand(pid); activate(pid, cid); }), [activate, expand]);
  const toggle = useCallback(pid => {
    pid = String(pid);
    setExpanded(v => v.includes(pid) ? v.filter(id => id !== pid) : [...v, pid]);
    load(pid).catch(() => {});
  }, [load]);
  const createProject = useCallback(async name => {
    const value = await api('post', 'projects', {name});
    if (!live.current) return;
    generations.current[value.id] = (generations.current[value.id] || 0) + 1;
    cache.current[value.id] = [];
    setLists(v => ({...v, [value.id]: []}));
    setProjects(v => [...v, value]); projectRef.current = [...projectRef.current, value];
    gate.current(() => { expand(value.id); activate(value.id); });
  }, [activate, expand]);
  const createChat = useCallback(pid => gate.current(async () => {
    pid = String(pid);
    if (requests.current.has(`create:${pid}`)) return;
    const epoch = selection.current.epoch;
    setCreating(v => ({...v, [pid]: true}));
    const work = (async () => { await load(pid); return api('post', `projects/${pid}/conversations`, {name:'New conversation'}); })();
    requests.current.set(`create:${pid}`, work);
    try {
      const value = await work;
      if (!live.current) return;
      generations.current[pid] = (generations.current[pid] || 0) + 1;
      const rows = [value, ...(cache.current[pid] || [])]; cache.current[pid] = rows;
      setLists(v => ({...v, [pid]: rows})); setLoading(v => ({...v, [pid]: false}));
      if (selection.current.epoch === epoch) { expand(pid); activate(pid, value.id); }
    } catch (failure) { if (live.current && selection.current.epoch === epoch) error.current(failure); }
    finally { requests.current.delete(`create:${pid}`); if (live.current) setCreating(v => ({...v, [pid]: false})); }
  }), [activate, expand, load]);
  const renameProject = useCallback(async (pid, name) => {
    const value = await api('patch', `projects/${pid}`, {name});
    if (live.current) setProjects(v => v.map(p => String(p.id) === String(pid) ? value : p));
  }, []);
  const renameChat = useCallback(async (pid, cid, name) => {
    await api('patch', `conversations/${cid}`, {name});
    if (!live.current) return;
    const rows = (cache.current[pid] || []).map(c => c.id === cid ? {...c, name} : c);
    cache.current[pid] = rows; setLists(v => ({...v, [pid]: rows}));
  }, []);
  const deleteProject = useCallback(async pid => {
    pid = String(pid);
    await api('delete', `projects/${pid}`);
    if (!live.current) return;
    const remaining = projectRef.current.filter(p => String(p.id) !== pid);
    projectRef.current = remaining; setProjects(remaining);
    generations.current[pid] = (generations.current[pid] || 0) + 1;
    delete cache.current[pid]; setLists(v => { const next = {...v}; delete next[pid]; return next; });
    setExpanded(v => v.filter(id => id !== pid));
    if (selection.current.project === pid) {
      const next = String(remaining[0]?.id || ''); activate(next);
      if (next) {
        expand(next); const epoch = selection.current.epoch;
        try { const rows = await load(next); if (live.current && rows && selection.current.epoch === epoch) activate(next, rows[0]?.id || ''); }
        catch (failure) { if (live.current) error.current(failure); }
      }
    }
  }, [activate, expand, load]);
  return {projects, project, chat, lists, expanded, loading, listErrors, creating, selectProject, selectChat, toggle, createProject, createChat, renameProject, renameChat, deleteProject, retryList: id => load(id, true).catch(() => {})};
}
