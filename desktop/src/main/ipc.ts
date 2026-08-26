import { ipcMain, type BrowserWindow, type IpcMainEvent, type IpcMainInvokeEvent } from 'electron';
import { z } from 'zod';
import type { AgentService } from './services/agent';
import type { GitService } from './services/git';
import type { TerminalService } from './services/terminal';
import type { WorkspaceService } from './services/workspace';

interface Services {
  workspace: WorkspaceService;
  git: GitService;
  terminal: TerminalService;
  agent: AgentService;
}

const relativePath = z.string().min(1).max(4096);
const paths = z.array(relativePath).min(1).max(500);
const terminalId = z.string().uuid();

export function registerIpc(window: BrowserWindow, services: Services): void {
  for (const channel of [
    'workspace:current', 'workspace:choose', 'workspace:open', 'workspace:list', 'workspace:read', 'workspace:write',
    'git:status', 'git:file-diff', 'git:stage', 'git:unstage', 'git:commit',
    'terminal:create', 'agent:start', 'agent:submit', 'agent:planner-action', 'agent:stop',
  ]) ipcMain.removeHandler(channel);
  for (const channel of ['terminal:write', 'terminal:resize', 'terminal:dispose']) ipcMain.removeAllListeners(channel);

  const trusted = (event: IpcMainInvokeEvent | IpcMainEvent): void => {
    if (event.sender.id !== window.webContents.id) throw new Error('Untrusted IPC sender.');
  };
  const handle = <TArgs extends unknown[], TResult>(
    channel: string,
    listener: (event: IpcMainInvokeEvent, ...args: TArgs) => Promise<TResult> | TResult,
  ): void => {
    ipcMain.handle(channel, (event, ...args) => {
      trusted(event);
      return listener(event, ...(args as TArgs));
    });
  };

  handle('workspace:current', () => services.workspace.current());
  handle('workspace:choose', async () => {
    const selected = await services.workspace.choose(window);
    if (selected) {
      services.terminal.disposeAll();
      await services.agent.stop();
    }
    return selected;
  });
  handle('workspace:open', async (_event, root: unknown) => {
    services.terminal.disposeAll();
    await services.agent.stop();
    return services.workspace.open(z.string().min(1).parse(root));
  });
  handle('workspace:list', (_event, path: unknown = '') => services.workspace.list(z.string().max(4096).parse(path)));
  handle('workspace:read', (_event, path: unknown) => services.workspace.read(relativePath.parse(path)));
  handle('workspace:write', (_event, path: unknown, content: unknown) => (
    services.workspace.write(relativePath.parse(path), z.string().max(10 * 1024 * 1024).parse(content))
  ));

  handle('git:status', () => services.git.status());
  handle('git:file-diff', (_event, path: unknown, staged: unknown = false) => (
    services.git.fileDiff(relativePath.parse(path), z.boolean().parse(staged))
  ));
  handle('git:stage', (_event, values: unknown) => services.git.stage(paths.parse(values)));
  handle('git:unstage', (_event, values: unknown) => services.git.unstage(paths.parse(values)));
  handle('git:commit', (_event, message: unknown) => services.git.commit(z.string().min(1).max(2000).parse(message)));

  handle('terminal:create', (_event, options: unknown) => services.terminal.create(z.object({
    cols: z.number().int().min(2).max(1000),
    rows: z.number().int().min(1).max(1000),
  }).parse(options)));
  ipcMain.on('terminal:write', (event, id: unknown, data: unknown) => {
    trusted(event);
    services.terminal.write(terminalId.parse(id), z.string().max(1024 * 1024).parse(data));
  });
  ipcMain.on('terminal:resize', (event, id: unknown, cols: unknown, rows: unknown) => {
    trusted(event);
    services.terminal.resize(
      terminalId.parse(id),
      z.number().int().min(2).max(1000).parse(cols),
      z.number().int().min(1).max(1000).parse(rows),
    );
  });
  ipcMain.on('terminal:dispose', (event, id: unknown) => {
    trusted(event);
    services.terminal.dispose(terminalId.parse(id));
  });

  handle('agent:start', (_event, options: unknown) => services.agent.start(z.object({
    provider: z.string().min(1).max(80),
    model: z.string().min(1).max(200),
    backendScript: z.enum(['main.py', 'main_v2.py', 'live_test_loop.py']).optional(),
  }).parse(options)));
  handle('agent:submit', (_event, text: unknown) => services.agent.submit(z.string().min(1).max(200_000).parse(text)));
  handle('agent:planner-action', (_event, action: unknown, extras: unknown = {}) => services.agent.plannerAction(
    z.string().min(1).max(100).parse(action),
    z.record(z.string(), z.unknown()).parse(extras),
  ));
  handle('agent:stop', () => services.agent.stop());
}
