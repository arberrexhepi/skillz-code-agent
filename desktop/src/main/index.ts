import { installArtifactFrameSecurity } from './services/artifactFrameSecurity';
import { ArtifactLibraryService } from './services/artifactLibrary';
import { ArtifactsService } from './services/artifacts';
import path from 'node:path';
import { app, BrowserWindow, Menu, screen, shell, type MenuItemConstructorOptions } from 'electron';
import { registerIpc } from './ipc';
import { AgentService } from './services/agent';
import { RuntimeSettingsService } from './services/runtimeSettings';
import { GitService } from './services/git';
import { TerminalService } from './services/terminal';
import { WorkspaceService } from './services/workspace';
import { WorkspaceHistoryService } from './services/workspaceHistory';

let mainWindow: BrowserWindow | null = null;
const shutdownTasks = new Set<Promise<unknown>>();

function installApplicationMenu(): void {
  const editorCommand = (command: 'undo' | 'redo' | 'find'): void => {
    const window = BrowserWindow.getFocusedWindow() || mainWindow;
    if (window && !window.isDestroyed()) window.webContents.send('editor:command', command);
  };
  const template: MenuItemConstructorOptions[] = [
    ...(process.platform === 'darwin' ? [{ role: 'appMenu' as const }] : []),
    { role: 'fileMenu' },
    {
      label: 'Edit',
      submenu: [
        { label: 'Undo', accelerator: 'CmdOrCtrl+Z', registerAccelerator: false, click: () => editorCommand('undo') },
        { label: 'Redo', accelerator: process.platform === 'darwin' ? 'CmdOrCtrl+Shift+Z' : 'Ctrl+Y', registerAccelerator: false, click: () => editorCommand('redo') },
        { type: 'separator' },
        { label: 'Find', accelerator: 'CmdOrCtrl+F', click: () => editorCommand('find') },
        { type: 'separator' },
        { role: 'cut' },
        { role: 'copy' },
        { role: 'paste' },
        { role: 'selectAll' },
      ],
    },
    { role: 'viewMenu' },
    { role: 'windowMenu' },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function createWindow(): void {
  const workArea = screen.getPrimaryDisplay().workAreaSize;
  const window = new BrowserWindow({
    width: Math.min(1560, workArea.width),
    height: Math.min(980, workArea.height),
    minWidth: 1050,
    minHeight: 680,
    backgroundColor: '#0b0d10',
    titleBarStyle: process.platform === 'darwin' ? 'hiddenInset' : 'default',
    webPreferences: {
      preload: path.join(__dirname, '../preload/index.js'),
      contextIsolation: true,
      nodeIntegration: false,
      nodeIntegrationInSubFrames: false,
      sandbox: true,
    },
  });
  mainWindow = window;

  const send = (channel: string, payload: unknown): void => {
    if (!window.isDestroyed()) window.webContents.send(channel, payload);
  };
  const workspaceHistory = new WorkspaceHistoryService(path.join(app.getPath('userData'), '.recent_repositories.md'));
  const workspace = new WorkspaceService((paths) => send('workspace:changed', paths), workspaceHistory);
  const git = new GitService(workspace);
  const terminal = new TerminalService(workspace, (event) => send('terminal:event', event));
  const runtimeSettings = new RuntimeSettingsService(path.join(app.getPath('userData'), 'runtime-settings.json'));
  const agent = new AgentService(workspace, (event) => send('agent:event', event), runtimeSettings);
  const artifactLibrary = new ArtifactLibraryService(
    path.join(app.getPath('userData'), 'artifact-settings.json'),
    app.isPackaged ? path.join(process.resourcesPath, 'artifact-template') : path.join(app.getAppPath(), 'artifact-template'),
    path.join(app.getPath('userData'), 'artifact-contexts'),
    app.isPackaged ? path.join(process.resourcesPath, 'prebuilt-artifacts') : path.join(app.getAppPath(), 'prebuilt-artifacts'),
  );
  const artifacts = new ArtifactsService(artifactLibrary, runtimeSettings, (event) => send('artifacts:event', event), () => workspace.current()?.root || '');
  const disposeFrameSecurity = installArtifactFrameSecurity(window.webContents, () => artifacts.previewOrigins());
  registerIpc(window, { workspace, git, terminal, agent, artifacts });

  window.webContents.setWindowOpenHandler(({ url }) => {
    let target = '';
    try { target = new URL(url).origin; } catch { /* Deny malformed URLs. */ }
    if (artifacts.processOrigins().includes(target)) void shell.openExternal(url);
    return { action: 'deny' };
  });
  window.webContents.on('will-navigate', (event) => event.preventDefault());
  window.on('closed', () => {
    disposeFrameSecurity();
    terminal.disposeAll();
    const shutdown = Promise.allSettled([agent.stop(), artifacts.dispose()]);
    shutdownTasks.add(shutdown);
    void shutdown.finally(() => shutdownTasks.delete(shutdown));
    workspace.dispose();
    mainWindow = null;
  });

  if (process.env.ELECTRON_RENDERER_URL) {
    void window.loadURL(process.env.ELECTRON_RENDERER_URL);
  } else {
    void window.loadFile(path.join(__dirname, '../renderer/index.html'));
  }
}

app.whenReady().then(() => {
  installApplicationMenu();
  createWindow();
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

// Keep Electron alive until artifact servers, browser pages, and agent sessions stop.
app.on('will-quit', (event) => {
  if (!shutdownTasks.size) return;
  event.preventDefault();
  void Promise.allSettled([...shutdownTasks]).then(() => app.quit());
});
