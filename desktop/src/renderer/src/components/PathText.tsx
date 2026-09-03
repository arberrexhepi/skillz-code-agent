import { Children, cloneElement, createContext, isValidElement, useContext, type ReactNode } from 'react';
import { findFileReferences, resolveFileReference, type FileReference } from '../../../shared/fileReferences';
export interface FileNavigation { root: string; open: (reference: FileReference) => void }
export const FileNavigationContext = createContext<FileNavigation | null>(null);
export const useFileNavigation = (): FileNavigation | null => useContext(FileNavigationContext);
export function PathChip({ path, label }: { path: string; label?: string }): React.JSX.Element {
  const navigation = useFileNavigation();
  const reference = resolveFileReference(path, navigation?.root || '');
  const display = label || path.replaceAll('\\', '/');
  const split = display.lastIndexOf('/');
  const title = reference ? `Open ${reference.path}${reference.line ? ` at line ${reference.line}, column ${reference.column}` : ''}\n${path}` : `${path}\nOutside the current workspace or unavailable`;
  const content = <><span className="path-chip-icon" aria-hidden="true">▧</span>{split >= 0 && <span className="path-chip-directory">{display.slice(0, split + 1)}</span>}<span className="path-chip-name">{display.slice(split + 1)}</span></>;
  return reference && navigation ? <a className="path-chip" href={`#file:${encodeURIComponent(path)}`} title={title} aria-label={`Open file ${path}`} onClick={(event) => { event.preventDefault(); event.stopPropagation(); navigation.open(reference); }}>{content}</a>
    : <span className="path-chip unavailable" title={title}>{content}</span>;
}
function textWithPaths(value: string): ReactNode {
  const matches = findFileReferences(value);
  if (!matches.length) return value;
  const nodes: ReactNode[] = [];
  let cursor = 0;
  for (const match of matches) {
    nodes.push(value.slice(cursor, match.start), <PathChip key={match.start} path={match.text} />);
    cursor = match.end;
  }
  nodes.push(value.slice(cursor));
  return nodes;
}
/** Apply to rendered prose, preserving inputs, existing links/actions, and literal code blocks. */
export function PathText({ children }: { children: ReactNode }): React.JSX.Element {
  const transform = (nodes: ReactNode): ReactNode => Children.map(nodes, (child) => {
    if (typeof child === 'string') return textWithPaths(child);
    if (!isValidElement<{ children?: ReactNode; dangerouslySetInnerHTML?: unknown }>(child) || typeof child.type !== 'string') return child;
    if (['a', 'button', 'pre', 'textarea', 'input', 'select', 'script', 'style', 'svg'].includes(child.type) || child.props.dangerouslySetInnerHTML) return child;
    return cloneElement(child, {}, transform(child.props.children));
  });
  return <>{transform(children)}</>;
}
