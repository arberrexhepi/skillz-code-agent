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
}

export type EditorTab = FileTab | DiffTab;
