import path from 'node:path';
import { app, BrowserWindow, screen } from 'electron';
import { registerIpc } from './ipc';
import { AgentService } from './services/agent';
import { GitService } from './services/git';
import { TerminalService } from './services/terminal';
import { WorkspaceService } from './services/workspace';

let mainWindow: BrowserWindow | null = null;

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
      sandbox: true,
    },
  });
  mainWindow = window;

  const send = (channel: string, payload: unknown): void => {
    if (!window.isDestroyed()) window.webContents.send(channel, payload);
  };
  const workspace = new WorkspaceService((paths) => send('workspace:changed', paths));
  const git = new GitService(workspace);
  const terminal = new TerminalService(workspace, (event) => send('terminal:event', event));
  const agent = new AgentService(workspace, (event) => send('agent:event', event));
  registerIpc(window, { workspace, git, terminal, agent });

  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', (event) => event.preventDefault());
  window.on('closed', () => {
    terminal.disposeAll();
    void agent.stop();
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
  createWindow();
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});
