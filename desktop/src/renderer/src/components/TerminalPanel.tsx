import { PathChip, PathText, useFileNavigation } from './PathText';
import { terminalFileLinks } from '../terminalFileLinks';
import { useEffect, useRef, useState } from 'react';
import { FitAddon } from '@xterm/addon-fit';
import { Terminal } from '@xterm/xterm';
import type { TerminalEvent } from '../../../shared/contracts';

export function TerminalPanel({ embedded = false }: { embedded?: boolean }): React.JSX.Element {
  const hostRef = useRef<HTMLDivElement>(null);
  const [collapsed, setCollapsed] = useState(false);
  const [exitLabel, setExitLabel] = useState('');
  const [paths, setPaths] = useState<string[]>([]);
  const navigation = useFileNavigation();
  const navigationRef = useRef(navigation);
  navigationRef.current = navigation;

  useEffect(() => {
    if (!hostRef.current || collapsed) return;
    let disposed = false;
    let starting = true;
    let sessionId: string | null = null;
    const pendingEvents: TerminalEvent[] = [];
    setExitLabel('');
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
    let scanTimer: ReturnType<typeof setTimeout> | undefined;
    const links = navigationRef.current ? terminal.registerLinkProvider({ provideLinks: (line, callback) => {
      const nav = navigationRef.current;
      callback(nav ? terminalFileLinks(terminal, line, nav.root, (ref) => navigationRef.current?.open(ref)) : []);
    } }) : undefined;
    const parsed = navigationRef.current ? terminal.onWriteParsed(() => {
      if (scanTimer || disposed) return;
      scanTimer = setTimeout(() => {
        scanTimer = undefined;
        if (disposed) return;
        const nav = navigationRef.current;
        if (!nav) return;
        const found = new Set<string>();
        const buffer = terminal.buffer.active;
        for (let row = Math.max(0, buffer.length - 60); row < buffer.length; row++) {
          if (buffer.getLine(row)?.isWrapped) continue;
          for (const link of terminalFileLinks(terminal, row + 1, nav.root, nav.open)) found.add(link.text);
        }
        const next = [...found].slice(-8);
        setPaths((previous) => previous.join('\n') === next.join('\n') ? previous : next);
      }, 150);
    }) : undefined;

    const receive = (event: TerminalEvent): void => {
      if (disposed) return;
      // A PTY can produce its prompt or exit before create's IPC reply arrives.
      if (starting) { pendingEvents.push(event); return; }
      if (event.sessionId !== sessionId) return;
      if (event.type === 'data') terminal.write(event.data);
      if (event.type === 'exit') {
        sessionId = null;
        setExitLabel(`Exited ${event.exitCode}`);
      }
    };
    const unsubscribe = window.workbench.terminal.onEvent(receive);
    const input = terminal.onData((data) => {
      if (!disposed && sessionId) window.workbench.terminal.write(sessionId, data);
    });
    const resize = (): void => {
      if (disposed) return;
      fit.fit();
      if (sessionId) window.workbench.terminal.resize(sessionId, terminal.cols, terminal.rows);
    };
    const observer = new ResizeObserver(resize);
    observer.observe(hostRef.current);
    void window.workbench.terminal.create({ cols: terminal.cols, rows: terminal.rows })
      .then((createdId) => {
        // Own the session in this effect, never in a ref shared with a later mount.
        if (disposed) { window.workbench.terminal.dispose(createdId); return; }
        sessionId = createdId;
        starting = false;
        for (const event of pendingEvents) receive(event);
        pendingEvents.length = 0;
        resize();
      })
      .catch((cause) => {
        if (disposed) return;
        starting = false;
        pendingEvents.length = 0;
        setExitLabel(`Could not start terminal: ${String(cause).replace(/^Error invoking remote method '[^']+': Error: /, '')}`);
      });

    return () => {
      disposed = true;
      if (scanTimer) clearTimeout(scanTimer);
      links?.dispose(); parsed?.dispose();
      observer.disconnect();
      unsubscribe();
      input.dispose();
      if (sessionId) window.workbench.terminal.dispose(sessionId);
      sessionId = null;
      pendingEvents.length = 0;
      terminal.dispose();
    };
  }, [collapsed]);

  return (
    <section className={`terminal-panel ${embedded ? 'embedded' : ''} ${collapsed ? 'collapsed' : ''}`}>
      {!embedded && <div className="panel-heading terminal-heading">
        <span>TERMINAL</span>
        {exitLabel && <small><PathText>{exitLabel}</PathText></small>}
        <button type="button" className="icon-button push-right" onClick={() => setCollapsed((value) => !value)}>{collapsed ? '⌃' : '⌄'}</button>
      </div>}
      {embedded && exitLabel && <div className="panel-message" role="status"><PathText>{exitLabel}</PathText></div>}
      {!collapsed && paths.length > 0 && <nav className="terminal-file-paths" aria-label="Files in terminal output"><span>Files</span>{paths.map((path) => <PathChip key={path} path={path} />)}</nav>}
      {!collapsed && <div className="terminal-host" ref={hostRef} />}
    </section>
  );
}
