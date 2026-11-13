import React, {useEffect, useId, useRef, useState} from 'react';
import {createPortal} from 'react-dom';

const paths = {
  folder: 'M3 7h6l2 2h10v10H3z M3 7V5h6l2 2h8v2',
  plus: 'M12 5v14 M5 12h14',
  more: 'M5 12h.01 M12 12h.01 M19 12h.01',
  chevron: 'm9 5 7 7-7 7',
  close: 'm6 6 12 12 M18 6 6 18',
  menu: 'M4 6h16 M4 12h16 M4 18h16',
  sidebar: 'M4 4h16v16H4z M9 4v16',
  document: 'M6 3h8l4 4v14H6z M14 3v5h4 M9 12h6 M9 16h6',
  attach: 'm9 13 7-7a3 3 0 0 1 4 4L10 20a5 5 0 0 1-7-7L13 3 M6 15l9-9',
  send: 'M12 19V5 m-6 6 6-6 6 6',
  stop: 'M6 6h12v12H6z',
  logout: 'M9 4H4v16h5 M10 12h10 m-4-4 4 4-4 4',
  edit: 'm4 16 12-12 4 4L8 20H4z M13 7l4 4',
  trash: 'M3 6h18 M9 6V3h6v3 M6 6l1 15h10l1-15 M10 10v7 M14 10v7',
};
export function Icon({name, className = ''}) {
  return <svg className={`icon ${className}`} width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={name === 'more' ? 3.5 : 1.6} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name] || paths.document}/></svg>;
}
export function IconButton({icon, label, className = '', ...props}) {
  return <button type="button" className={`icon-button ${className}`} aria-label={label} title={label} {...props}><Icon name={icon}/></button>;
}

export function useFocusBoundary(ref, close, enabled = true) {
  const closeRef = useRef(close);
  closeRef.current = close;
  useEffect(() => {
    if (!enabled) return;
    const previous = document.activeElement;
    const node = ref.current;
    if (!node) return;
    const items = () => [...node.querySelectorAll('button:not(:disabled), input:not(:disabled):not([type="hidden"]), textarea:not(:disabled), select:not(:disabled), [href], [tabindex="0"]')].filter(el => el.tabIndex >= 0 && !el.closest('[hidden]'));
    (node.querySelector('[data-autofocus]') || items()[0] || node).focus();
    const keydown = event => {
      if (event.defaultPrevented) return;
      const dialogs = [...document.querySelectorAll('[role="dialog"][aria-modal="true"]')];
      const top = dialogs.filter(el => el.closest('.modal-backdrop')).at(-1) || dialogs.at(-1);
      if (top && top !== node) return;
      if (event.key === 'Escape') { event.preventDefault(); closeRef.current?.(); }
      if (event.key === 'Tab') {
        const list = items(), first = list[0], last = list[list.length - 1];
        if (!first) { event.preventDefault(); node.focus(); }
        else if (event.shiftKey && (document.activeElement === first || !node.contains(document.activeElement))) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && (document.activeElement === last || !node.contains(document.activeElement))) { event.preventDefault(); first.focus(); }
      }
    };
    document.addEventListener('keydown', keydown);
    return () => {
      document.removeEventListener('keydown', keydown);
      const target = previous?.isConnected ? previous : document.querySelector('[aria-label="New project"]');
      target?.focus();
    };
  }, [ref, enabled]);
}

export function Modal({title, children, onClose, busy = false, className = ''}) {
  const ref = useRef(null), id = useId();
  useFocusBoundary(ref, () => { if (!busy) onClose(); });
  return <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget && !busy) onClose(); }}>
    <section ref={ref} tabIndex={-1} role="dialog" aria-modal="true" aria-labelledby={id} className={`workspace-modal ${className}`}>
      <div className="modal-heading"><h2 id={id}>{title}</h2><IconButton icon="close" label={`Close ${title}`} onClick={onClose} disabled={busy}/></div>
      {children}
    </section>
  </div>;
}

export function ActionMenu({label, items, disabled = false, icon = 'more'}) {
  const [open, setOpen] = useState(false), host = useRef(null), button = useRef(null), menu = useRef(null);
  const [position, setPosition] = useState({});
  useEffect(() => {
    if (!open) return;
    menu.current?.querySelector('button')?.focus();
    const outside = event => { if (!host.current?.contains(event.target) && !menu.current?.contains(event.target)) setOpen(false); };
    const dismiss = () => setOpen(false);
    document.addEventListener('pointerdown', outside);
    window.addEventListener('resize', dismiss);
    document.addEventListener('scroll', dismiss, true);
    return () => { document.removeEventListener('pointerdown', outside); window.removeEventListener('resize', dismiss); document.removeEventListener('scroll', dismiss, true); };
  }, [open]);
  return <div className="action-menu" ref={host} onKeyDown={event => {
    if (event.key === 'Escape' && open) { event.preventDefault(); event.stopPropagation(); setOpen(false); button.current?.focus(); }
    if (event.key === 'Tab') { setOpen(false); button.current?.focus(); }
    if (open && ['ArrowDown','ArrowUp','Home','End'].includes(event.key)) {
      event.preventDefault();
      const list = [...menu.current.querySelectorAll('button')], index = list.indexOf(document.activeElement);
      list[event.key === 'Home' ? 0 : event.key === 'End' ? list.length - 1 : (index + (event.key === 'ArrowUp' ? -1 : 1) + list.length) % list.length]?.focus();
    }
  }}>
    <button ref={button} type="button" className="icon-button" aria-label={label} title={label} aria-haspopup="menu" aria-expanded={open} disabled={disabled} onClick={() => {
      const bounds = button.current.getBoundingClientRect(), height = items.length * 38 + 12;
      setPosition({position:'fixed',right:'auto',bottom:'auto',left:Math.max(8,Math.min(bounds.right - 210,window.innerWidth - 218)),top:bounds.bottom + height > window.innerHeight - 12 ? Math.max(8,bounds.top - height - 6) : bounds.bottom + 6});
      setOpen(v => !v);
    }}><Icon name={icon}/></button>
    {open && createPortal(<div ref={menu} className="workspace-menu" style={position} role="menu" aria-label={label}>{items.map(item => <button key={item.label} type="button" role="menuitem" disabled={item.disabled} className={item.danger ? 'danger-text' : ''} onClick={() => { button.current?.focus(); setOpen(false); item.onClick(); }}><Icon name={item.icon || 'edit'}/>{item.label}</button>)}</div>,document.body)}
  </div>;
}

export function useMediaQuery(query) {
  const [matches, setMatches] = useState(() => !!window.matchMedia?.(query).matches);
  useEffect(() => {
    const media = window.matchMedia?.(query);
    if (!media) return;
    const changed = () => setMatches(media.matches);
    changed(); media.addEventListener?.('change', changed);
    return () => media.removeEventListener?.('change', changed);
  }, [query]);
  return matches;
}
