import React, {useEffect, useState} from 'react';
import {readChoice, rememberChoice} from './workspaceApi';

const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
export function panelSizes(width, expanded, wanted, left = 280, right = 400, force = false) {
  const rail = width < 768 ? 56 : 64;
  const sidebar = expanded && force && !wanted ? Math.min(left,Math.max(220,width-170)) : expanded && (!wanted ? left + 320 : left + right + 328) <= width ? left : rail;
  const available = width - sidebar - 328;
  const visible = wanted && available >= 280;
  return {sidebar, expanded:sidebar !== rail, right:visible ? Math.min(right, available) : 0, visible};
}
export function usePanelLayout(expanded, wanted) {
  const [width, setWidth] = useState(window.innerWidth);
  const [force, setForce] = useState(false);
  const [sizes, setSizes] = useState(() => {
    let saved; try { saved = JSON.parse(readChoice('workspace.widths', '{}')); } catch {}
    return {left:clamp(Number(saved?.left) || 280,220,360), right:clamp(Number(saved?.right) || 400,280,640)};
  });
  useEffect(() => {
    const resize = () => {setWidth(window.innerWidth);setForce(false);};
    window.addEventListener('resize',resize); return () => window.removeEventListener('resize',resize);
  },[]);
  const layout = panelSizes(width,expanded,wanted,sizes.left,sizes.right,force);
  function resize(side,value) {
    setSizes(previous => {
      const max = side === 'left' ? Math.min(360,width - (layout.visible ? layout.right + 328 : 320)) : Math.min(640,width - layout.sidebar - 328);
      const next = {...previous,[side]:clamp(value,side === 'left' ? 220 : 280,Math.max(side === 'left' ? 220 : 280,max))};
      rememberChoice('workspace.widths',JSON.stringify(next)); return next;
    });
  }
  return {...layout,sizes,resize,allowExpand:() => setForce(true)};
}
export function PanelDivider({side,value,onResize}) {
  const min=side === 'left' ? 220 : 280, max=side === 'left' ? 360 : 640;
  return <div className="panel-divider" role="separator" tabIndex={0} aria-label={`Resize ${side === 'left' ? 'projects' : 'right panel'}`} aria-orientation="vertical" aria-valuemin={min} aria-valuemax={max} aria-valuenow={Math.round(value)} onKeyDown={event => {
    const delta=event.key === 'ArrowRight' ? 10 : event.key === 'ArrowLeft' ? -10 : 0;
    if (delta || ['Home','End'].includes(event.key)) {event.preventDefault();onResize(event.key === 'Home' ? min : event.key === 'End' ? max : value + delta*(side === 'left' ? 1 : -1));}
  }} onPointerDown={event => {
    event.preventDefault();event.currentTarget.focus();event.currentTarget.setPointerCapture(event.pointerId);
    event.currentTarget.dataset.start=event.clientX;event.currentTarget.dataset.width=value;
  }} onPointerMove={event => {
    if (event.currentTarget.hasPointerCapture(event.pointerId)) onResize(Number(event.currentTarget.dataset.width)+(event.clientX-Number(event.currentTarget.dataset.start))*(side === 'left' ? 1 : -1));
  }} onPointerUp={event => event.currentTarget.releasePointerCapture(event.pointerId)}/>;
}
