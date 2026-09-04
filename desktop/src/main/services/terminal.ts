import { randomUUID } from 'node:crypto';
import * as pty from 'node-pty';
import type { TerminalCreateOptions, TerminalEvent } from '../../shared/contracts';
import type { WorkspaceService } from './workspace';

export function terminalEnvironment(source: NodeJS.ProcessEnv = process.env): Record<string, string> {
  const environment = Object.fromEntries(
    Object.entries(source).filter((entry): entry is [string, string] => typeof entry[1] === 'string'),
  );
  const setDefault = (key: string, value: string): void => {
    if (!Object.keys(environment).some((candidate) => candidate.toLowerCase() === key.toLowerCase())) {
      environment[key] = value;
    }
  };
  // npm's animated progress hides which network step is waiting, and its
  // automatic audit request can hold an install open in an embedded PTY. Keep
  // installs concise and deterministic; users can still run `npm audit` explicitly.
  setDefault('NPM_CONFIG_PROGRESS', 'false');
  setDefault('NPM_CONFIG_AUDIT', 'false');
  setDefault('NPM_CONFIG_FUND', 'false');
  return { ...environment, TERM: 'xterm-256color', COLORTERM: 'truecolor' };
}

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
    const session = pty.spawn(shell, [], {
      name: 'xterm-256color',
      cols: Math.max(2, options.cols),
      rows: Math.max(1, options.rows),
      cwd: this.workspace.requireRoot(),
      env: terminalEnvironment(),
      ...(process.platform === 'win32' ? {} : { encoding: 'utf8' as const }),
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
    // Renderer messages can arrive after workspace teardown or a natural exit.
    this.sessions.get(sessionId)?.write(data);
  }

  resize(sessionId: string, cols: number, rows: number): void {
    this.sessions.get(sessionId)?.resize(Math.max(2, cols), Math.max(1, rows));
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
}
