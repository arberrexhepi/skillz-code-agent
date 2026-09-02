// Browser-only integration fixture. No Python process, real files, or PTY is used.
import type { WorkbenchApi } from '../../src/shared/contracts';

let root = '/layout-fixture/alpha';
let terminalCount = 0;
const noop = () => {};
const response = async () => ({ ok: true, state: { planner: {}, transcript: [] } });
const status = async () => ({ branch: 'layout-preview', ahead: 0, behind: 0, files: [{ path: 'example.ts', indexStatus: ' ', workTreeStatus: 'M' }] });
const info = () => ({ root, name: root.split('/').at(-1)! });
const document = (path: string, content = '// Unsaved edits stay here when the editor is hidden.\nconst greeting = "Hello";\n') => ({ path, content, language: 'typescript', modifiedAt: 0 });

window.workbench = {
  workspace: {
    current: async () => info(),
    choose: async () => { root = root.endsWith('alpha') ? '/layout-fixture/beta' : '/layout-fixture/alpha'; return info(); },
    open: async (next) => { root = next; return info(); },
    list: async () => [{ name: 'example.ts', path: 'example.ts', kind: 'file' }],
    read: async (path) => document(path), write: async (path, content) => document(path, content), onChange: () => noop,
  },
  git: {
    status, history: async () => [], fileDiff: async (path) => ({ path, original: 'const greeting = "Before";', modified: 'const greeting = "After";', language: 'typescript' }),
    stage: status, stageAll: status, unstage: status, discard: async () => ({ discarded: false, status: await status() }), commit: status, push: status,
  },
  terminal: {
    create: async () => `fixture-terminal-${++terminalCount}`, write: noop, resize: noop, dispose: noop,
    onEvent: (listener) => { const timer = setTimeout(() => listener({ type: 'data', sessionId: `fixture-terminal-${terminalCount}`, data: '\r\nLayout fixture terminal — no shell connected.\r\n' }), 100); return () => clearTimeout(timer); },
  },
  agent: {
    start: response, submit: response, plannerAction: response, workerAction: response, reconfigureRuntime: response, configureBackoff: response,
    runtimeOptions: async () => ({ current_provider: 'gemini', current_model: 'preview', providers: [] }),
    codexSubscriptionStatus: async () => ({}), codexSubscriptionLogin: async () => ({}), stop: async () => {},
    onEvent: (listener) => {
      const timer = setTimeout(() => listener({ type: 'state', state: { planner: {}, transcript: [
        { role: 'user', content: 'Make the workspace comfortable for focused coding and conversation.' },
        { role: 'assistant', content: '## A more flexible workspace\n\nThe editor can step out of the way while the agent stays in focus. Your **unsaved tabs** and drafts stay intact.\n\n- Drag the divider to find a comfortable width.\n- Use the view controls to restore the editor.\n\nInline code such as `editorVisible` remains readable.\n\n```ts\nconst view = { editorVisible: true };\n```\n\n| Area | Behavior |\n| --- | --- |\n| Agent | Resizable |\n| Editor | Show or hide |' },
      ] } }), 100);
      return () => clearTimeout(timer);
    },
  },
} as WorkbenchApi;

void import('../../src/renderer/src/main');
