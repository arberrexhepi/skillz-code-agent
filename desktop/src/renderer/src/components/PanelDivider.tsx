import { useRef, useState, type PointerEvent } from 'react';
import { clampAgentWidth, DEFAULT_WORKSPACE_VIEW, keyboardAgentWidth } from '../../../shared/workspaceView';

export function PanelDivider({ width, bounds, hidden, onResize }: {
  width: number;
  bounds: { min: number; max: number };
  hidden: boolean;
  onResize: (width: number) => void;
}): React.JSX.Element {
  const drag = useRef<{ pointerId: number; x: number; width: number } | null>(null);
  const [dragging, setDragging] = useState(false);
  const finish = (event: PointerEvent<HTMLDivElement>): void => {
    if (drag.current?.pointerId !== event.pointerId) return;
    drag.current = null;
    setDragging(false);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  };

  return <div className="panel-divider" role="separator" tabIndex={hidden ? -1 : 0} hidden={hidden}
    aria-label="Agent panel width" aria-orientation="vertical" aria-controls="workspace-agent"
    aria-valuemin={bounds.min} aria-valuemax={bounds.max} aria-valuenow={width} aria-valuetext={`${width} pixels`}
    title="Drag to resize agent · Arrow keys to adjust · Double-click to reset"
    data-resizing={dragging}
    onPointerDown={(event) => {
      if (event.button !== 0 || !event.isPrimary || drag.current) return;
      event.preventDefault();
      event.currentTarget.focus();
      event.currentTarget.setPointerCapture(event.pointerId);
      drag.current = { pointerId: event.pointerId, x: event.clientX, width };
      setDragging(true);
    }}
    onPointerMove={(event) => {
      const start = drag.current;
      if (start?.pointerId !== event.pointerId) return;
      onResize(clampAgentWidth(start.width + start.x - event.clientX, bounds));
    }}
    onPointerUp={finish} onPointerCancel={finish} onLostPointerCapture={finish}
    onDoubleClick={() => onResize(clampAgentWidth(DEFAULT_WORKSPACE_VIEW.agentWidth, bounds))}
    onKeyDown={(event) => {
      const next = keyboardAgentWidth(event.key, width, bounds, event.shiftKey);
      if (next === undefined) return;
      event.preventDefault();
      onResize(next);
    }}
  />;
}
