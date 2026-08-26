import { useEffect, useRef, useState } from 'react';
import { FitAddon } from '@xterm/addon-fit';
import { Terminal } from '@xterm/xterm';

export function TerminalPanel(): React.JSX.Element {
  const hostRef = useRef<HTMLDivElement>(null);
  const sessionRef = useRef<string | null>(null);
  const [collapsed, setCollapsed] = useState(false);
  const [exitLabel, setExitLabel] = useState('');

  useEffect(() => {
    if (!hostRef.current || collapsed) return;
    const terminal = new Terminal({
      convertEol: true,
      cursorBlink: true,
      fontFamily: "'SFMono-Regular', 'Cascadia Code', 'JetBrains Mono', monospace",
      fontSize: 12,
      lineHeight: 1.25,
      theme: { background: '#0b0d10', foreground: '#c6ccd5', cursor: '#9ce5c0', selectionBackground: '#26425f' },
    });
    const fit = new FitAddon();
    terminal.loadAddon(fit);
    terminal.open(hostRef.current);
    fit.fit();

    const unsubscribe = window.workbench.terminal.onEvent((event) => {
      if (event.type === 'data' && (!sessionRef.current || event.sessionId === sessionRef.current)) terminal.write(event.data);
      if (event.type === 'exit' && event.sessionId === sessionRef.current) setExitLabel(`Exited ${event.exitCode}`);
    });
    terminal.onData((data) => {
      if (sessionRef.current) window.workbench.terminal.write(sessionRef.current, data);
    });
    const observer = new ResizeObserver(() => {
      fit.fit();
      if (sessionRef.current) window.workbench.terminal.resize(sessionRef.current, terminal.cols, terminal.rows);
    });
    observer.observe(hostRef.current);
    void window.workbench.terminal.create({ cols: terminal.cols, rows: terminal.rows })
      .then((sessionId) => { sessionRef.current = sessionId; });

    return () => {
      observer.disconnect();
      unsubscribe();
      if (sessionRef.current) window.workbench.terminal.dispose(sessionRef.current);
      sessionRef.current = null;
      terminal.dispose();
    };
  }, [collapsed]);

  return (
    <section className={`terminal-panel ${collapsed ? 'collapsed' : ''}`}>
      <div className="panel-heading terminal-heading">
        <span>TERMINAL</span>
        {exitLabel && <small>{exitLabel}</small>}
        <button type="button" className="icon-button push-right" onClick={() => setCollapsed((value) => !value)}>{collapsed ? '⌃' : '⌄'}</button>
      </div>
      {!collapsed && <div className="terminal-host" ref={hostRef} />}
    </section>
  );
}
