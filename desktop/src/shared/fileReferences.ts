/** File references in prose; resolution never grants access outside the selected workspace. */
export interface FileReference { path: string; line?: number; column?: number }
export interface PathMatch { start: number; end: number; text: string }
const fileName = /(?:\.(?:[cm]?[jt]sx?|pyi?|mdx?|json[cl]?|ya?ml|toml|ini|cfg|conf|txt|log|csv|xml|html?|css|scss|sass|less|vue|svelte|sh|bash|zsh|fish|ps1|cmd|bat|c|cc|cpp|cxx|h|hpp|cs|go|rs|java|kt|swift|rb|php|sql|graphql|gql|proto|lock|svg|png|jpe?g|gif|webp|pdf|ipynb|env|properties)|(?:^|[/\\])(?:Dockerfile|Makefile|README|LICENSE|AGENTS\.md|\.gitignore|\.gitattributes|\.editorconfig|\.env(?:\.[\w-]+)?))$/i;
function location(value: string): FileReference {
  const suffix = /(?::(\d+)(?::(\d+))?|#L(\d+)(?:C(\d+))?|\((\d+)(?:,\s*(\d+))?\))$/.exec(value);
  return suffix ? { path: value.slice(0, suffix.index), line: Math.max(1, Number(suffix[1] || suffix[3] || suffix[5])), column: Math.max(1, Number(suffix[2] || suffix[4] || suffix[6] || 1)) } : { path: value };
}
function looksLikeFile(value: string): boolean {
  const path = location(value).path;
  if (/^(?:cat|node|python[23]?|py|git|npm|npx|cd|ls|rg|rm|touch|echo|const|let|import|from)\s/.test(path)) return false;
  if (/^(?:https?:|mailto:|data:|javascript:|ftp:|www\.)/i.test(path) || path.includes('://') && !path.startsWith('file://')) return false;
  if (/[<>{}|*?]/.test(path) || !fileName.test(path) && !/^(?:\/repo\/|[A-Za-z]:[\\/])/.test(path)) return false;
  return !path.includes('@') && !/^\d+(?:\.\d+)+$/.test(path);
}
export function findFileReferences(text: string): PathMatch[] {
  const found: PathMatch[] = [];
  // Quotes/backticks keep paths containing spaces together; ordinary prose uses token boundaries.
  for (const token of text.matchAll(/`([^`\n]+)`|"([^"\n]+)"|'([^'\n]+)'|[^\s<>`"']+/gu)) {
    const quoted = token[1] ?? token[2] ?? token[3];
    let value = quoted ?? token[0];
    let start = token.index! + (quoted === undefined ? 0 : 1);
    if (quoted === undefined) {
      const prefix = /^[([{]+/.exec(value)?.[0] || '';
      start += prefix.length;
      value = value.slice(prefix.length).replace(/[.,;!?\]}]+$/, '');
      if (!/\(\d+(?:,\d+)?\)$/.test(value)) value = value.replace(/\)+$/, '');
      value = value.replace(/:$/, '');
    }
    if (looksLikeFile(value)) found.push({ start, end: start + value.length, text: value });
    else if (quoted !== undefined) {
      for (const inner of findFileReferences(value)) found.push({ ...inner, start: start + inner.start, end: start + inner.end });
    }
  }
  return found;
}
function normalize(value: string): string | null {
  const parts: string[] = [];
  for (const part of value.split('/')) {
    if (!part || part === '.') continue;
    if (part === '..') { if (!parts.length) return null; parts.pop(); }
    else parts.push(part);
  }
  return parts.join('/');
}
export function resolveFileReference(raw: string, workspaceRoot: string): FileReference | null {
  if (!workspaceRoot) return null;
  let value = raw.trim().replace(/^([`"'])(.*)\1$/, '$2');
  if (value.startsWith('file://')) {
    try { const uri = new URL(value); if (uri.hostname && uri.hostname !== 'localhost') return null; value = decodeURIComponent(uri.pathname) + uri.hash; if (/^\/[A-Za-z]:\//.test(value)) value = value.slice(1); }
    catch { return null; }
  }
  const ref = location(value);
  let candidate = ref.path.replaceAll('\\', '/');
  const root = workspaceRoot.replaceAll('\\', '/').replace(/\/$/, '');
  if (!candidate || /[\u0000-\u001f<>"|*?]/.test(candidate) || candidate.includes('://')) return null;
  const windows = /^[A-Za-z]:\//.test(root) || root.startsWith('//');
  const compare = (path: string) => windows ? path.toLowerCase() : path;
  if (compare(candidate).startsWith(compare(root) + '/')) candidate = candidate.slice(root.length + 1);
  else if (candidate.startsWith('/repo/')) candidate = candidate.slice(6);
  else if (candidate.startsWith('/') || /^[A-Za-z]:/.test(candidate)) return null;
  if (candidate.includes(':') || candidate.includes('#')) return null;
  const path = normalize(candidate);
  return path ? { ...ref, path } : null;
}
