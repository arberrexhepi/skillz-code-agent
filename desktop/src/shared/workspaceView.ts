export interface WorkspaceView {
  editorVisible: boolean;
  agentWidth: number;
}

export const DEFAULT_WORKSPACE_VIEW: WorkspaceView = { editorVisible: true, agentWidth: 390 };
export const PANEL_DIVIDER_WIDTH = 6;
export const MIN_AGENT_WIDTH = 300;
export const MIN_EDITOR_WIDTH = 320;

export function normalizeWorkspaceView(value: unknown): WorkspaceView {
  const candidate = value && typeof value === 'object' ? value as Partial<WorkspaceView> : {};
  return {
    editorVisible: typeof candidate.editorVisible === 'boolean' ? candidate.editorVisible : true,
    agentWidth: typeof candidate.agentWidth === 'number' && Number.isFinite(candidate.agentWidth)
      ? Math.max(MIN_AGENT_WIDTH, Math.round(candidate.agentWidth)) : DEFAULT_WORKSPACE_VIEW.agentWidth,
  };
}

export function agentWidthBounds(workbenchWidth: number, sidebarWidth: number): { min: number; max: number } {
  const available = Math.max(0, workbenchWidth - sidebarWidth - PANEL_DIVIDER_WIDTH);
  // At unusually small sizes, share the available space instead of overflowing.
  const min = Math.min(MIN_AGENT_WIDTH, Math.floor(available / 2));
  return { min, max: Math.max(min, available - MIN_EDITOR_WIDTH) };
}

export function clampAgentWidth(width: number, bounds: { min: number; max: number }): number {
  return Math.round(Math.min(bounds.max, Math.max(bounds.min, width)));
}

export function keyboardAgentWidth(key: string, width: number, bounds: { min: number; max: number }, largeStep = false): number | undefined {
  const step = largeStep ? 50 : 10;
  // The agent sits on the right: moving the divider left makes it wider.
  if (key === 'ArrowLeft') return clampAgentWidth(width + step, bounds);
  if (key === 'ArrowRight') return clampAgentWidth(width - step, bounds);
  if (key === 'Home') return bounds.min;
  if (key === 'End') return bounds.max;
  return undefined;
}

export function workspaceViewKey(root: string): string {
  return `skillz:workspace-view:v1:${root}`;
}

export function readWorkspaceView(storage: Pick<Storage, 'getItem'>, root: string): WorkspaceView {
  try { return normalizeWorkspaceView(JSON.parse(storage.getItem(workspaceViewKey(root)) || 'null')); }
  catch { return { ...DEFAULT_WORKSPACE_VIEW }; }
}

export function writeWorkspaceView(storage: Pick<Storage, 'setItem'>, root: string, view: WorkspaceView): void {
  try { storage.setItem(workspaceViewKey(root), JSON.stringify(normalizeWorkspaceView(view))); }
  catch { /* Layout controls still work when persistence is unavailable. */ }
}
