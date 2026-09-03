import DOMPurify from 'dompurify';
import { marked } from 'marked';
import { useMemo } from 'react';
import { findFileReferences, resolveFileReference } from '../../../../shared/fileReferences';
import { useFileNavigation } from '../PathText';

marked.use({ gfm: true, breaks: true });

export function MarkdownMessage({ content }: { content: string }): React.JSX.Element {
  const navigation = useFileNavigation();
  const html = useMemo(() => {
    const clean = DOMPurify.sanitize(marked.parse(content, { async: false }) as string, {
      USE_PROFILES: { html: true }, FORBID_TAGS: ['style', 'iframe', 'object', 'embed', 'form'],
      FORBID_ATTR: ['style'], ALLOW_DATA_ATTR: false,
    });
    return decorateFileReferences(clean, navigation?.root || '');
  }, [content, navigation?.root]);
  return <div className="markdown-message" dangerouslySetInnerHTML={{ __html: html }} onClick={(event) => {
    const chip = (event.target as Element).closest<HTMLAnchorElement>('a[data-file-reference]');
    if (!chip || !event.currentTarget.contains(chip)) return;
    event.preventDefault(); event.stopPropagation();
    const reference = resolveFileReference(chip.dataset.fileReference || '', navigation?.root || '');
    if (reference) navigation?.open(reference);
  }} />;
}

/** Operate on a detached, sanitized document, never on React-owned DOM. */
export function decorateFileReferences(html: string, root: string): string {
  if (typeof DOMParser === 'undefined') return html;
  const body = new DOMParser().parseFromString(html, 'text/html').body;
  const chip = (path: string, label?: string): HTMLElement => {
    const ref = resolveFileReference(path, root);
    const el = body.ownerDocument.createElement(ref ? 'a' : 'span');
    el.className = `path-chip${ref ? '' : ' unavailable'}`;
    el.title = ref ? `Open ${ref.path}${ref.line ? ` at line ${ref.line}, column ${ref.column}` : ''}\n${path}` : `${path}\nOutside the current workspace or unavailable`;
    if (ref) { el.setAttribute('href', '#file:' + encodeURIComponent(path)); el.setAttribute('data-file-reference', path); el.setAttribute('aria-label', 'Open file ' + path); }
    const icon = body.ownerDocument.createElement('span'); icon.className = 'path-chip-icon'; icon.textContent = '▧'; icon.setAttribute('aria-hidden', 'true'); el.append(icon);
    const display = label || path.replaceAll('\\', '/');
    const split = display.lastIndexOf('/');
    if (split >= 0) { const dir = body.ownerDocument.createElement('span'); dir.className = 'path-chip-directory'; dir.textContent = display.slice(0, split + 1); el.append(dir); }
    const name = body.ownerDocument.createElement('span'); name.className = 'path-chip-name'; name.textContent = display.slice(split + 1); el.append(name);
    return el;
  };
  for (const link of body.querySelectorAll('a[href]')) {
    if (link.closest('pre')) continue;
    const href = link.getAttribute('href') || '';
    if (/^(?:https?:|mailto:|#)/i.test(href)) continue;
    let path = href;
    try { if (!href.startsWith('file://')) path = decodeURI(href); } catch { continue; }
    if (findFileReferences(path).length || resolveFileReference(path, root)) link.replaceWith(chip(path));
  }
  const visit = (node: Node): void => {
    if (node.nodeType === 1 && (node as Element).tagName === 'CODE' && !(node as Element).closest('pre')) {
      const value = node.textContent || '';
      const references = findFileReferences('`' + value + '`');
      if (references.length === 1 && references[0].text === value) {
        (node as Element).replaceChildren(chip(value)); return;
      }
    }
    if (node.nodeType === 1 && (node as Element).matches('a, pre, button, textarea, input, select, script, style, .path-chip')) return;
    if (node.nodeType === 3) {
      const text = node.textContent || '';
      const matches = findFileReferences(text);
      if (!matches.length) return;
      const fragment = body.ownerDocument.createDocumentFragment();
      let cursor = 0;
      for (const match of matches) { fragment.append(text.slice(cursor, match.start), chip(match.text)); cursor = match.end; }
      fragment.append(text.slice(cursor)); node.parentNode?.replaceChild(fragment, node);
    } else for (const child of [...node.childNodes]) visit(child);
  };
  visit(body);
  return body.innerHTML;
}
