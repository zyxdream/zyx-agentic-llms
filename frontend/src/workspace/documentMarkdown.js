import { Marked } from 'marked';

export const isMarkdownDraft = (draft) => draft?.format_version === 2
  && draft.source_format === 'markdown' && typeof draft.markdown === 'string';

const PREVIEW_TAGS = new Set([
  'P', 'DIV', 'SPAN', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'UL', 'OL', 'LI',
  'BLOCKQUOTE', 'PRE', 'CODE', 'STRONG', 'EM', 'B', 'I', 'U', 'S', 'DEL',
  'HR', 'BR', 'A', 'TABLE', 'THEAD', 'TBODY', 'TFOOT', 'TR', 'TH', 'TD',
  'CAPTION', 'SUP', 'SUB',
]);

const safeLink = (href) => /^(?:https?:|mailto:|#)/i.test(href || '');
const documentParser = new Marked({ gfm: true });

export function renderDocumentMarkdown(markdown) {
  // Canonical source goes directly to Marked. Chat-response cleanup deliberately
  // does not run here: an HTML/code example must remain an example.
  // Parse in an inert template: detached live elements can fire image/load events before cleanup.
  const template = document.createElement('template');
  template.innerHTML = String(documentParser.parse(String(markdown || '')) || '');
  const root = template.content;
  root.querySelectorAll('script,style,iframe,object,embed,form,input,button,svg,math,img,link,meta,base,audio,video,source,template')
    .forEach(element => element.remove());
  Array.from(root.querySelectorAll('*')).forEach(element => {
    if (!PREVIEW_TAGS.has(element.tagName)) {
      element.replaceWith(...element.childNodes);
      return;
    }
    Array.from(element.attributes).forEach(attribute => {
      const name = attribute.name.toLowerCase();
      const keepLink = element.tagName === 'A' && (name === 'title'
        || (name === 'href' && safeLink(attribute.value.trim())));
      const keepAlignment = ['TD', 'TH'].includes(element.tagName) && name === 'align'
        && ['left', 'center', 'right'].includes(attribute.value);
      const keepSpan = ['TD', 'TH'].includes(element.tagName) && ['rowspan', 'colspan'].includes(name)
        && /^[1-9]\d*$/.test(attribute.value);
      const keepLanguage = element.tagName === 'CODE' && name === 'class'
        && /^language-[\w-]+$/.test(attribute.value);
      if (!keepLink && !keepAlignment && !keepSpan && !keepLanguage) element.removeAttribute(attribute.name);
    });
    if (element.tagName === 'A') {
      if (!element.hasAttribute('href')) element.replaceWith(...element.childNodes);
      else {
        element.setAttribute('target', '_blank');
        element.setAttribute('rel', 'noopener noreferrer');
      }
    }
  });
  return template.innerHTML;
}
