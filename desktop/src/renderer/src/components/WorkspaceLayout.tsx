import { useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react';
import { agentWidthBounds, clampAgentWidth, DEFAULT_WORKSPACE_VIEW, PANEL_DIVIDER_WIDTH, type WorkspaceView } from '../../../shared/workspaceView';
import { PanelDivider } from './PanelDivider';

export function WorkspaceLayout({ inert, view, onAgentResize, sidebar, editor, dock, agent }: {
  inert?: boolean;
  view: WorkspaceView;
  onAgentResize: (width: number) => void;
  sidebar: ReactNode;
  editor: ReactNode;
  dock: ReactNode;
  agent: ReactNode;
}): React.JSX.Element {
  const container = useRef<HTMLDivElement>(null);
  const sidebarSlot = useRef<HTMLDivElement>(null);
  const [bounds, setBounds] = useState({ min: 300, max: DEFAULT_WORKSPACE_VIEW.agentWidth });
  useLayoutEffect(() => {
    const element = container.current;
    const sidebarElement = sidebarSlot.current;
    if (!element || !sidebarElement) return;
    const measure = (): void => {
      const next = agentWidthBounds(element.clientWidth, sidebarElement.getBoundingClientRect().width);
      setBounds((current) => current.min === next.min && current.max === next.max ? current : next);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    observer.observe(sidebarElement);
    return () => observer.disconnect();
  }, []);
  // A smaller window temporarily clamps the visible width, not the saved preference.
  const width = clampAgentWidth(view.agentWidth, bounds);
  const style = { '--agent-width': `${width}px`, '--divider-width': `${PANEL_DIVIDER_WIDTH}px` } as CSSProperties;
  return <div inert={inert} ref={container} className={`workbench${view.editorVisible ? '' : ' editor-hidden'}`} style={style}>
    <div className="workspace-sidebar-slot" ref={sidebarSlot}>{sidebar}</div>
    <main id="workspace-editor" className="workspace-editor-slot" hidden={!view.editorVisible}>{editor}</main>
    <div className="workspace-dock-slot">{dock}</div>
    <PanelDivider width={width} bounds={bounds} hidden={!view.editorVisible} onResize={onAgentResize} />
    <div id="workspace-agent" className="workspace-agent-slot">{agent}</div>
  </div>;
}
