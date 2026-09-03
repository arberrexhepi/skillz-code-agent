import { createRoot } from 'react-dom/client';
import type { GitStatus, WorkbenchApi } from '../../src/shared/contracts';
import { GitPanel } from '../../src/renderer/src/components/GitPanel';
import '../../src/renderer/src/styles.css';

// Real panel, mocked Git operations: browser checks never create a repository.
const workspaceRoot = 'C:\\Projects\\My First Project';
const params = new URLSearchParams(location.search);
let initialized = false;
let failInitialization = params.has('failInit');
let calls = 0;
const status = (): GitStatus => initialized
  ? { isRepository: true, branch: 'main', ahead: 0, behind: 0, files: [{ path: 'notes.md', indexStatus: '?', workTreeStatus: '?' }] }
  : { isRepository: false, branch: '', ahead: 0, behind: 0, files: [] };
window.workbench = { git: {
  status: async () => {
    if (params.has('gitError')) throw new Error('Git executable was not found. Install Git and retry.');
    const snapshot = status();
    await new Promise((resolve) => setTimeout(resolve, params.has('slowRefresh') ? 600 : 30));
    return snapshot;
  },
  initialize: async (expectedRoot: string) => {
    if (expectedRoot !== workspaceRoot) throw new Error('Wrong workspace');
    calls += 1;
    document.getElementById('calls')!.textContent = `Initialization calls: ${calls}`;
    await new Promise((resolve) => setTimeout(resolve, 500));
    if (failInitialization) { failInitialization = false; throw new Error('Could not create .git: permission denied. Retry when the folder is writable.'); }
    initialized = true;
    return status();
  },
  history: async () => {
    if (!initialized) throw new Error('History must not be requested before initialization.');
    return [];
  },
} } as WorkbenchApi;
createRoot(document.getElementById('fixture')!).render(<aside className="sidebar" style={{ width: 290, height: '100vh', margin: '0 auto' }}>
  <div className="panel-heading"><span>SOURCE CONTROL</span></div>
  <GitPanel workspaceRoot={workspaceRoot} revision={0} onOpenDiff={() => {}} onStatus={() => {}} onBeforeDiscard={() => true} onDiscard={async () => {}} />
  <small id="calls" style={{ padding: 12, color: '#68737d' }}>Initialization calls: 0</small>
</aside>);
