// Browser-only integration fixture. No Python process, real files, or PTY is used.
import type { WorkbenchApi } from '../../src/shared/contracts';
import { timelineFixture, thoughtFixture } from './agent-timeline';
import { reviewFixture } from './plan-review';
import { proposalsFixture } from './issue-proposals';

let root = '/layout-fixture/alpha';
let terminalCount = 0;
const noop = () => {};
const response = async () => ({ ok: true, state: { planner: {}, transcript: [] } });
const planPreview = new URLSearchParams(window.location.search).has('plan');
let planState = structuredClone(reviewFixture);
let failRevision = new URLSearchParams(window.location.search).has('failRevision');
const suggestionsPreview = new URLSearchParams(window.location.search).has('suggestions');
let suggestionsState = structuredClone(proposalsFixture);
let failDecision = new URLSearchParams(window.location.search).has('failDecision');
const status = async () => ({ isRepository: true, branch: 'layout-preview', ahead: 0, behind: 0, files: [{ path: 'example.ts', indexStatus: ' ', workTreeStatus: 'M' }] });
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
    status, initialize: async () => status(), history: async () => [], fileDiff: async (path) => ({ path, original: 'const greeting = "Before";', modified: 'const greeting = "After";', language: 'typescript' }),
    stage: status, stageAll: status, unstage: status, discard: async () => ({ discarded: false, status: await status() }), commit: status, push: status,
  },
  terminal: {
    create: async () => `fixture-terminal-${++terminalCount}`, write: noop, resize: noop, dispose: noop,
    onEvent: (listener) => { const timer = setTimeout(() => listener({ type: 'data', sessionId: `fixture-terminal-${terminalCount}`, data: '\r\nLayout fixture terminal — no shell connected.\r\n' }), 100); return () => clearTimeout(timer); },
  },
  agent: {
    start: async () => suggestionsPreview ? { ok: true, state: suggestionsState } : planPreview ? { ok: true, state: planState } : response(), submit: response,
    plannerAction: async (action, extras) => {
      if (suggestionsPreview) {
        if (failDecision) { failDecision = false; return { ok: false, message: 'Fixture storage is unavailable. Retry your decision.', state: suggestionsState }; }
        const proposals = suggestionsState.planner.worker_state!.issue_proposals!.proposals!;
        const proposal = proposals.find((item) => item.proposal_id === extras?.proposal_id);
        if (proposal && ['accept_issue_proposal', 'ignore_issue_proposal'].includes(action)) {
          if (action === 'accept_issue_proposal') suggestionsState.planner.issue_state!.issues!.push({ issue_id: 'issue-042', status: 'open', request_summary: proposal.summary });
          suggestionsState.planner.worker_state!.issue_proposals!.proposals = proposals.filter((item) => item !== proposal);
        }
        return { ok: true, state: structuredClone(suggestionsState) };
      }
      if (!planPreview) return response();
      if (action === 'revise_plan') {
        if (failRevision) { failRevision = false; return { ok: false, message: 'Fixture revision failed; retry with the saved feedback.', state: planState }; }
        if (JSON.stringify(extras?.expected_plan) !== JSON.stringify(planState.planner.pending_plan)) return { ok: false, message: 'Stale fixture plan', state: planState };
        planState = { ...planState, planner: { pending_plan: { ...planState.planner.pending_plan, summary: `Revised plan: ${extras?.feedback}` } } };
      } else planState = { ...planState, planner: {} };
      return { ok: true, state: planState };
    }, workerAction: response, reconfigureRuntime: response, configureBackoff: response,
    runtimeOptions: async () => ({ current_provider: 'gemini', current_model: 'preview', providers: [] }),
    chooseCodexCli: async () => null, setCodexCliPath: async () => ({}),
    codexSubscriptionStatus: async () => ({}), codexSubscriptionLogin: async () => ({}), stop: async () => {},
    onEvent: (listener) => {
      if (suggestionsPreview) {
        const timer = setTimeout(() => { listener({ type: 'status', status: 'running' }); listener({ type: 'state', state: suggestionsState }); }, 100);
        return () => clearTimeout(timer);
      }
      if (planPreview) {
        const timer = setTimeout(() => { listener({ type: 'status', status: 'running' }); listener({ type: 'state', state: planState }); }, 100);
        return () => clearTimeout(timer);
      }
      if (new URLSearchParams(window.location.search).has('workflow')) {
        const timer = setTimeout(() => {
          listener({ type: 'state', state: timelineFixture });
          listener({ type: 'progress', payload: thoughtFixture });
          listener({ type: 'progress', payload: { type: 'progress', domain: 'worker', action_type: 'cat', summary: 'Read 100 lines.' } });
        }, 100);
        return () => clearTimeout(timer);
      }
      const timer = setTimeout(() => listener({ type: 'state', state: { planner: {}, transcript: [
        { role: 'user', content: 'Make the workspace comfortable for focused coding and conversation.' },
        { role: 'assistant', content: '## A more flexible workspace\n\nThe editor can step out of the way while the agent stays in focus. Your **unsaved tabs** and drafts stay intact.\n\n- Drag the divider to find a comfortable width.\n- Use the view controls to restore the editor.\n\nInline code such as `editorVisible` remains readable.\n\n```ts\nconst view = { editorVisible: true };\n```\n\n| Area | Behavior |\n| --- | --- |\n| Agent | Resizable |\n| Editor | Show or hide |' },
      ] } }), 100);
      return () => clearTimeout(timer);
    },
  },
} as WorkbenchApi;

void import('../../src/renderer/src/main');
