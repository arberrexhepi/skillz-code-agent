import { useLayoutEffect, useRef } from 'react';

export function DraftStrategyDropdown({ entries }: { entries?: string[] }): React.JSX.Element {
  const scroller = useRef<HTMLDivElement>(null);
  const follow = useRef(true);

  const showLatest = () => {
    const view = scroller.current;
    if (view && follow.current) view.scrollTop = view.scrollHeight;
  };

  useLayoutEffect(() => {
    if (!entries?.length) follow.current = true;
    showLatest();
  }, [entries]);

  return <details className="turn-thought-draft" onToggle={event => { if (event.currentTarget.open) showLatest(); }}>
    <summary>Draft strategy{Boolean(entries?.length) && <span className="turn-thought-count" aria-hidden="true">{entries?.length}</span>}</summary>
    <div ref={scroller} className="turn-thought-copy" role="region" aria-label="Draft strategy" tabIndex={0}
      onScroll={({ currentTarget }) => {
        follow.current = currentTarget.scrollHeight - currentTarget.scrollTop - currentTarget.clientHeight < 8;
      }}>
      {entries?.length ? <ul>{entries.map((entry, index) => <li key={index}><code>{entry}</code></li>)}</ul>
        : <p className="turn-thought-empty">Waiting for command tags.</p>}
    </div>
  </details>;
}
