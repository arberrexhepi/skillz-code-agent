import path from 'node:path';
import { promises as fs } from 'node:fs';
import { randomUUID } from 'node:crypto';
import type { ArtifactsService } from './services/artifacts';
import { artifactSetupSelectionSchema, artifactId, artifactApisSchema, artifactAccessSchema, createArtifactSchema, previewInputSchema } from '../shared/artifacts';
import { clipboard, dialog, ipcMain, Menu, shell, type BrowserWindow, type IpcMainEvent, type IpcMainInvokeEvent, type MenuItemConstructorOptions } from 'electron';
import { isUntracked } from '../shared/gitStatus';
import { z } from 'zod';
import type { AgentService } from './services/agent';
import type { GitService } from './services/git';
import type { TerminalService } from './services/terminal';
import type { WorkspaceService } from './services/workspace';

interface Services {
  artifacts: ArtifactsService;
  workspace: WorkspaceService;
  git: GitService;
  terminal: TerminalService;
  agent: AgentService;
}

const relativePath = z.string().min(1).max(4096);
const paths = z.array(relativePath).min(1).max(500);
const terminalId = z.string().uuid();
const fileEntry = z.object({
  name: z.string().min(1).max(1024),
  path: relativePath,
  kind: z.enum(['file', 'directory']),
});

function sameWorkspacePath(left: string, right: string): boolean {
  const normalize = (value: string): string => {
    const resolved = path.resolve(value);
    return process.platform === 'win32' ? resolved.toLowerCase() : resolved;
  };
  return normalize(left) === normalize(right);
}

export function registerIpc(window: BrowserWindow, services: Services): void {
  for (const channel of [
    'workspace:current', 'workspace:recent', 'workspace:choose', 'workspace:open', 'workspace:close', 'workspace:list', 'workspace:show-entry-menu', 'workspace:create-file', 'workspace:read', 'workspace:write', 'workspace:repo-facts', 'workspace:issues', 'workspace:issue-action',
    'git:status', 'git:initialize', 'git:history', 'git:file-diff', 'git:stage', 'git:stage-all', 'git:unstage', 'git:discard', 'git:commit', 'git:push',
    'terminal:create', 'terminal:copy', 'agent:start', 'agent:submit', 'agent:planner-action', 'agent:worker-action',
    'agent:reconfigure-runtime', 'agent:configure-backoff', 'agent:runtime-options',
    'agent:codex-subscription-status', 'agent:codex-subscription-login', 'agent:stop',
    'agent:choose-codex-cli', 'agent:set-codex-cli-path',
  ]) ipcMain.removeHandler(channel);
  for (const channel of ['terminal:write', 'terminal:resize', 'terminal:dispose']) ipcMain.removeAllListeners(channel);

  const trusted = (event: IpcMainInvokeEvent | IpcMainEvent): void => {
    if (event.sender.id !== window.webContents.id || event.senderFrame !== window.webContents.mainFrame) throw new Error('Untrusted IPC sender.');
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

  const artifactChannels = ['capabilities', 'install-capabilities', 'setup-progress', 'save-provider-key', 'setup-download', 'docker-cleanup-plan', 'clean-docker', 'library', 'prebuilts', 'install-prebuilt', 'choose-folder', 'choose-read-directory', 'access', 'save-access', 'create', 'apis', 'save-apis', 'start', 'stop', 'install-browser', 'preview', 'input', 'reload', 'close-preview', 'reveal', 'agent-start', 'agent-submit', 'agent-runtime', 'agent-planner', 'agent-worker', 'agent-reconfigure', 'agent-backoff', 'agent-stop'];
  for (const name of artifactChannels) ipcMain.removeHandler(`artifacts:${name}`);
  handle('artifacts:capabilities', (_event, selection: unknown) => services.artifacts.capabilities.status(artifactSetupSelectionSchema.parse(selection)));
  handle('artifacts:install-capabilities', (_event, selection: unknown) => services.artifacts.capabilities.install(artifactSetupSelectionSchema.parse(selection)));
  handle('artifacts:setup-progress', () => services.artifacts.capabilities.snapshot());
  handle('artifacts:save-provider-key', (_event, provider: unknown, key: unknown) => services.artifacts.capabilities.saveKey(z.enum(['openai', 'gemini', 'anthropic', 'meta']).parse(provider), z.string().trim().min(1).max(8192).nullable().parse(key)));
  handle('artifacts:setup-download', (_event, tool: unknown) => services.artifacts.capabilities.openDownload(z.enum(['python', 'git', 'docker']).parse(tool)));
  handle('artifacts:docker-cleanup-plan', () => services.artifacts.dockerCleanupPlan());
  handle('artifacts:clean-docker', () => services.artifacts.cleanDocker());
  handle('artifacts:library', () => services.artifacts.library.library());
  handle('artifacts:prebuilts', () => services.artifacts.library.prebuilts());
  handle('artifacts:install-prebuilt', (_event, id: unknown, access: unknown, runtime: unknown) => services.artifacts.library.installPrebuilt(artifactId.parse(id), artifactAccessSchema.parse(access), runtime == null ? undefined : createArtifactSchema.shape.runtime.parse(runtime)));
  handle('artifacts:choose-folder', async () => {
    const result = await dialog.showOpenDialog(window, { title: 'Choose an empty folder or existing artifact library', properties: ['openDirectory', 'createDirectory'] });
    return result.canceled || !result.filePaths[0] ? null : services.artifacts.configure(result.filePaths[0]);
  });
  handle('artifacts:choose-read-directory', async () => {
    const result = await dialog.showOpenDialog(window, { title: 'Choose a folder to share with this artifact', properties: ['openDirectory'] });
    if (result.canceled || !result.filePaths[0]) return null;
    const directory = await fs.realpath(result.filePaths[0]);
    return { id: `folder-${randomUUID().slice(0, 8)}`, label: path.basename(directory) || directory, path: directory, access: 'read' as const };
  });
  handle('artifacts:access', (_event, id: unknown) => services.artifacts.library.access(artifactId.parse(id)));
  handle('artifacts:save-access', async (_event, id: unknown, raw: unknown) => {
    const checkedId = artifactId.parse(id);
    await services.artifacts.saveAccess(checkedId, artifactAccessSchema.parse(raw));
  });
  handle('artifacts:create', (_event, raw: unknown) => {
    const options = createArtifactSchema.parse(raw);
    if (options.sourceRoot && options.sourceRoot !== services.workspace.current()?.root) throw new Error('Source workspace changed. Reopen Artifacts and retry.');
    return services.artifacts.create(options);
  });
  handle('artifacts:apis', (_event, id: unknown) => services.artifacts.library.apis(artifactId.parse(id)));
  handle('artifacts:save-apis', (_event, id: unknown, config: unknown) => services.artifacts.library.saveApis(artifactId.parse(id), artifactApisSchema.parse(config)));
  handle('artifacts:start', (_event, id: unknown) => services.artifacts.start(artifactId.parse(id)));
  handle('artifacts:stop', (_event, id: unknown) => services.artifacts.stop(artifactId.parse(id)));
  handle('artifacts:install-browser', () => services.artifacts.preview.installBrowser());
  handle('artifacts:preview', (_event, id: unknown) => services.artifacts.frame(artifactId.parse(id)));
  handle('artifacts:input', (_event, id: unknown, input: unknown) => services.artifacts.preview.input(artifactId.parse(id), previewInputSchema.parse(input)));
  handle('artifacts:reload', (_event, id: unknown) => services.artifacts.preview.reload(artifactId.parse(id)));
  handle('artifacts:close-preview', (_event, id: unknown) => services.artifacts.preview.close(artifactId.parse(id)));
  handle('artifacts:reveal', async (_event, id: unknown) => { const artifact = await services.artifacts.library.find(artifactId.parse(id)); const error = await shell.openPath(artifact.root); if (error) throw new Error(error); });
  handle('artifacts:agent-start', (_event, id: unknown, options: unknown) => services.artifacts.startAgent(artifactId.parse(id), z.object({ provider: z.string().min(1).max(80), model: z.string().min(1).max(200), backendScript: z.enum(['main.py', 'main_v2.py', 'live_test_loop.py']).optional() }).parse(options)));
  handle('artifacts:agent-runtime', async (_event, id: unknown, provider: unknown = '', model: unknown = '') => (await services.artifacts.agent(artifactId.parse(id))).runtimeOptions(z.string().max(80).parse(provider), z.string().max(200).parse(model)));
  handle('artifacts:agent-submit', (_event, id: unknown, text: unknown) => services.artifacts.submit(artifactId.parse(id), z.string().min(1).max(200000).parse(text)));
  handle('artifacts:agent-planner', async (_event, id: unknown, action: unknown, extras: unknown = {}) => (await services.artifacts.agent(artifactId.parse(id))).plannerAction(z.string().min(1).max(100).parse(action), z.record(z.string(), z.unknown()).parse(extras)));
  handle('artifacts:agent-worker', async (_event, id: unknown, action: unknown) => (await services.artifacts.agent(artifactId.parse(id))).workerAction(z.record(z.string(), z.unknown()).parse(action)));
  handle('artifacts:agent-reconfigure', async (_event, id: unknown, provider: unknown, model: unknown) => (await services.artifacts.agent(artifactId.parse(id))).reconfigureRuntime(z.string().min(1).max(80).parse(provider), z.string().min(1).max(200).parse(model)));
  handle('artifacts:agent-backoff', async (_event, id: unknown, enabled: unknown, limit: unknown) => (await services.artifacts.agent(artifactId.parse(id))).configureBackoff(z.boolean().parse(enabled), z.number().min(1).max(10000).parse(limit)));
  handle('artifacts:agent-stop', async (_event, id: unknown) => (await services.artifacts.agent(artifactId.parse(id))).stop());

  handle('workspace:current', () => services.workspace.current());
  handle('workspace:recent', () => services.workspace.recent());
  handle('workspace:choose', async () => {
    const previous = services.workspace.current();
    const selected = await services.workspace.choose(window);
    if (selected && (!previous || !sameWorkspacePath(previous.root, selected.root))) {
      services.terminal.disposeAll();
      await services.agent.stop();
    }
    return selected;
  });
  handle('workspace:open', async (_event, root: unknown) => {
    const candidate = z.string().min(1).parse(root);
    const current = services.workspace.current();
    if (current && sameWorkspacePath(current.root, candidate)) return current;
    services.terminal.disposeAll();
    await services.agent.stop();
    return services.workspace.open(candidate);
  });
  handle('workspace:close', async () => {
    services.terminal.disposeAll();
    await services.agent.stop();
    services.workspace.close();
  });
  handle('workspace:list', (_event, path: unknown = '') => services.workspace.list(z.string().max(4096).parse(path)));
  handle('workspace:show-entry-menu', async (_event, rawEntry: unknown, rawExpanded: unknown) => {
    const entry = fileEntry.parse(rawEntry);
    const expanded = z.boolean().parse(rawExpanded);
    const root = services.workspace.requireRoot();
    const unresolved = services.workspace.resolve(entry.path);
    const stat = await fs.lstat(unresolved);
    if (stat.isSymbolicLink()) throw new Error('Linked entries cannot be opened from the workspace menu.');
    if ((entry.kind === 'file' && !stat.isFile()) || (entry.kind === 'directory' && !stat.isDirectory())) throw new Error('The selected workspace entry has changed. Refresh Files and retry.');
    const target = await fs.realpath(unresolved);
    const relation = path.relative(root, target);
    if (relation.startsWith('..') || path.isAbsolute(relation)) throw new Error('Path is outside the active workspace.');
    if (!sameWorkspacePath(root, services.workspace.requireRoot())) throw new Error('Workspace changed. Open the menu again.');

    return new Promise<'open' | 'toggle' | 'new-file' | null>((resolve) => {
      let resolved = false;
      const finish = (action: 'open' | 'toggle' | 'new-file' | null): void => {
        if (resolved) return;
        resolved = true;
        resolve(action);
      };
      const revealLabel = process.platform === 'darwin' ? 'Reveal in Finder' : process.platform === 'win32' ? 'Reveal in File Explorer' : 'Show in File Manager';
      const template: MenuItemConstructorOptions[] = [
        ...(entry.kind === 'file'
          ? [{ label: 'Open', click: () => finish('open') }]
          : [
              { label: 'New File…', click: () => finish('new-file') },
              { type: 'separator' as const },
              { label: expanded ? 'Collapse' : 'Expand', click: () => finish('toggle') },
            ]),
        { type: 'separator' },
        { label: revealLabel, click: () => { shell.showItemInFolder(target); finish(null); } },
        { type: 'separator' },
        { label: 'Copy Relative Path', click: () => { clipboard.writeText(entry.path); finish(null); } },
        { label: 'Copy Full Path', click: () => { clipboard.writeText(target); finish(null); } },
      ];
      Menu.buildFromTemplate(template).popup({ window, callback: () => finish(null) });
    });
  });
  handle('workspace:create-file', (_event, parentPath: unknown, name: unknown) => services.workspace.createFile(
    relativePath.parse(parentPath),
    z.string().min(1).max(255).parse(name),
  ));
  handle('workspace:read', (_event, path: unknown) => services.workspace.read(relativePath.parse(path)));
  handle('workspace:issues', (_event, root: unknown) => services.workspace.issues(z.string().min(1).max(4096).parse(root)));
  handle('workspace:issue-action', (_event, root: unknown, action: unknown, extras: unknown = {}) => services.workspace.issueAction(
    z.string().min(1).max(4096).parse(root),
    z.enum(['create_issue', 'close_issue', 'reopen_issue']).parse(action),
    z.record(z.string(), z.unknown()).parse(extras),
  ));
  handle('workspace:repo-facts', (_event, root: unknown) => services.workspace.repoFacts(z.string().min(1).max(4096).parse(root)));
  handle('workspace:write', (_event, path: unknown, content: unknown) => (
    services.workspace.write(relativePath.parse(path), z.string().max(10 * 1024 * 1024).parse(content))
  ));

  handle('git:status', () => services.git.status());
  handle('git:initialize', (_event, root: unknown) => services.git.initialize(z.string().min(1).max(4096).parse(root)));
  handle('git:history', (_event, limit: unknown = 50) => services.git.history(z.number().int().min(1).max(200).parse(limit)));
  handle('git:file-diff', (_event, path: unknown, staged: unknown = false) => (
    services.git.fileDiff(relativePath.parse(path), z.boolean().parse(staged))
  ));
  handle('git:stage', (_event, values: unknown) => services.git.stage(paths.parse(values)));
  handle('git:stage-all', () => services.git.stageAll());
  handle('git:unstage', (_event, values: unknown) => services.git.unstage(paths.parse(values)));
  handle('git:discard', (_event, value: unknown) => services.git.discard(
    relativePath.parse(value),
    async (file) => {
      const untracked = isUntracked(file);
      const result = await dialog.showMessageBox(window, {
        type: 'warning',
        title: untracked ? 'Move untracked file to Trash?' : 'Discard unstaged changes?',
        message: file.path,
        detail: untracked
          ? 'This file is not tracked by Git. It will be moved to Trash, where you can recover it.'
          : 'Restore this file to its staged version. Staged changes will be kept. Unstaged changes will be lost and cannot be undone.',
        buttons: ['Cancel', untracked ? 'Move to Trash' : 'Discard Changes'],
        defaultId: 0,
        cancelId: 0,
        noLink: true,
      });
      return result.response === 1;
    },
    (path) => shell.trashItem(path),
  ));
  handle('git:commit', (_event, message: unknown) => services.git.commit(z.string().min(1).max(2000).parse(message)));
  handle('git:push', () => services.git.push());

  handle('terminal:create', (_event, options: unknown) => services.terminal.create(z.object({
    cols: z.number().int().min(2).max(1000),
    rows: z.number().int().min(1).max(1000),
  }).parse(options)));
  handle('terminal:copy', (_event, text: unknown) => clipboard.writeText(z.string().min(1).max(200_000).parse(text)));
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
  handle('agent:worker-action', (_event, action: unknown) => services.agent.workerAction(
    z.record(z.string(), z.unknown()).parse(action),
  ));
  handle('agent:reconfigure-runtime', (_event, provider: unknown, model: unknown) => services.agent.reconfigureRuntime(
    z.string().min(1).max(80).parse(provider),
    z.string().min(1).max(200).parse(model),
  ));
  handle('agent:configure-backoff', (_event, enabled: unknown, tokenLimitK: unknown) => services.agent.configureBackoff(
    z.boolean().parse(enabled),
    z.number().int().min(0).max(10_000_000).parse(tokenLimitK),
  ));
  handle('agent:runtime-options', (_event, provider: unknown = '', model: unknown = '') => services.agent.runtimeOptions(
    z.string().max(80).parse(provider),
    z.string().max(200).parse(model),
  ));
  handle('agent:codex-subscription-status', () => services.agent.codexSubscriptionStatus());
  handle('agent:codex-subscription-login', () => services.agent.codexSubscriptionLogin());
  handle('agent:choose-codex-cli', async () => {
    const selection = await dialog.showOpenDialog(window, {
      title: 'Locate Codex CLI',
      buttonLabel: 'Choose Codex CLI',
      properties: ['openFile', 'showHiddenFiles'],
      ...(process.platform === 'win32' ? { filters: [{ name: 'Executable', extensions: ['exe'] }] } : {}),
    });
    return selection.canceled ? null : selection.filePaths[0] || null;
  });
  handle('agent:set-codex-cli-path', (_event, candidate: unknown) => services.agent.setCodexCliPath(
    z.string().trim().min(1).max(4096).refine((value) => !value.includes('\0')).nullable().parse(candidate),
  ));
  handle('agent:stop', () => services.agent.stop());
}
