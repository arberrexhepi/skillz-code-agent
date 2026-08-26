import { randomUUID } from 'node:crypto';
import * as pty from 'node-pty';
import type { TerminalCreateOptions, TerminalEvent } from '../../shared/contracts';
import type { WorkspaceService } from './workspace';

export class TerminalService {
  private readonly sessions = new Map<string, pty.IPty>();

  constructor(
    private readonly workspace: WorkspaceService,
    private readonly emit: (event: TerminalEvent) => void,
  ) {}

  create(options: TerminalCreateOptions): string {
    const sessionId = randomUUID();
    const shell = process.platform === 'win32'
      ? (process.env.COMSPEC || 'powershell.exe')
      : (process.env.SHELL || '/bin/zsh');
    const environment = Object.fromEntries(
      Object.entries(process.env).filter((entry): entry is [string, string] => typeof entry[1] === 'string'),
    );
    const session = pty.spawn(shell, [], {
      name: 'xterm-256color',
      cols: Math.max(2, options.cols),
      rows: Math.max(1, options.rows),
      cwd: this.workspace.requireRoot(),
      env: { ...environment, TERM: 'xterm-256color', COLORTERM: 'truecolor' },
      encoding: 'utf8',
    });
    this.sessions.set(sessionId, session);
    session.onData((data) => this.emit({ type: 'data', sessionId, data }));
    session.onExit(({ exitCode }) => {
      this.sessions.delete(sessionId);
      this.emit({ type: 'exit', sessionId, exitCode });
    });
    return sessionId;
  }

  write(sessionId: string, data: string): void {
    this.requireSession(sessionId).write(data);
  }

  resize(sessionId: string, cols: number, rows: number): void {
    this.requireSession(sessionId).resize(Math.max(2, cols), Math.max(1, rows));
  }

  dispose(sessionId: string): void {
    const session = this.sessions.get(sessionId);
    if (!session) return;
    this.sessions.delete(sessionId);
    session.kill();
  }

  disposeAll(): void {
    for (const sessionId of [...this.sessions.keys()]) this.dispose(sessionId);
  }

  private requireSession(sessionId: string): pty.IPty {
    const session = this.sessions.get(sessionId);
    if (!session) throw new Error('Terminal session is no longer active.');
    return session;
  }
}
