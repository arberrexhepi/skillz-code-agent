import { useEffect, useState } from 'react';
import type { AgentProgressMessage } from '../../../../shared/agentTypes';

export function TurnThought({ active, action, thought }: { active: boolean; action: string; thought?: AgentProgressMessage }): React.JSX.Element | null {
  const [elapsed, setElapsed] = useState(0);
  const [collapsed, setCollapsed] = useState(false);
  useEffect(() => setCollapsed(false), [active]);
  useEffect(() => {
    if (!active) return;
    const started = Date.now();
    setElapsed(0);
    const timer = window.setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1000)), 1000);
    return () => window.clearInterval(timer);
  }, [active, action]);
  const content = thought?.thought?.trim();
  if (!active && !content) return null;
  if (!active) return <details className="turn-thought last-thought"><summary>Last turn thought{thought?.turn ? ` · Turn ${thought.turn}` : ''}</summary><p>{content}</p></details>;
  return <section className="turn-thought live-thought" aria-label="Current turn thought">
    <header><i aria-hidden="true" /><strong>{content ? 'Turn thought' : 'Working'}</strong>{thought?.turn ? <span>Turn {thought.turn}</span> : null}<time>{elapsed}s</time><button type="button" aria-label={collapsed ? 'Expand turn thought' : 'Collapse turn thought'} aria-expanded={!collapsed} onClick={() => setCollapsed((value) => !value)}>{collapsed ? '⌄' : '⌃'}</button></header>
    {!collapsed && <><div className="turn-thought-copy" aria-live="polite" aria-atomic="true">{content ? <p>{content}</p> : <p>{action.replaceAll('_', ' ') || 'Waiting for the agent’s next update…'}</p>}</div>
      {content && <small>{action.replaceAll('_', ' ')}</small>}</>}
  </section>;
}
