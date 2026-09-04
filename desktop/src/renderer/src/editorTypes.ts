import type { FileDocument, GitFileDiff } from '../../shared/contracts';

export interface FileTab {
  id: string;
  kind: 'file';
  title: string;
  document: FileDocument;
  content: string;
  dirty: boolean;
  reveal?: { line: number; column: number; request: number };
}

export interface DiffTab {
  id: string;
  kind: 'diff';
  title: string;
  diff: GitFileDiff;
  revision?: number;
}

export type EditorTab = FileTab | DiffTab;

export type RefreshedEditorTab =
  | { id: string; kind: 'file'; document: FileDocument }
  | { id: string; kind: 'diff'; diff: GitFileDiff };

export function applyEditorRefresh(tabs: EditorTab[], refreshed: RefreshedEditorTab[]): EditorTab[] {
  const updates = new Map(refreshed.map((item) => [item.id, item]));
  return tabs.map((tab) => {
    const update = updates.get(tab.id);
    if (!update || update.kind !== tab.kind) return tab;
    if (tab.kind === 'file' && update.kind === 'file') {
      if (tab.dirty) return tab;
      return { ...tab, document: update.document, content: update.document.content, dirty: false };
    }
    if (tab.kind === 'diff' && update.kind === 'diff') return { ...tab, diff: update.diff, revision: (tab.revision || 0) + 1 };
    return tab;
  });
}
