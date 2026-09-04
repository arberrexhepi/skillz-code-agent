import { applyIssueAction, issuesBridge, issuesSnapshot } from './workspace-issues';
import type { AgentEvent } from '../../src/shared/contracts';
// Browser-only integration fixture. No Python process, real files, or PTY is used.
import type { WorkbenchApi } from '../../src/shared/contracts';
import { timelineFixture, thoughtFixture } from './agent-timeline';
import { reviewFixture } from './plan-review';
import { proposalsFixture } from './issue-proposals';
import { repoFactsMarkdown, repoFactsSnapshot } from './repo-facts';

let root = '/layout-fixture/alpha';
let terminalCount = 0;
const noop = () => {};
const response = async () => ({ ok: true, state: { planner: {}, transcript: [] } });
const issuesPreview = new URLSearchParams(window.location.search).has('issues');
let persistedIssues = issuesSnapshot(root);
let emitAgent: ((event: AgentEvent) => void) | undefined;
const pathsPreview = new URLSearchParams(window.location.search).has('paths');
const factsPreview = new URLSearchParams(window.location.search).has('facts') || pathsPreview || issuesPreview;
const planPreview = new URLSearchParams(window.location.search).has('plan');
let planState = structuredClone(reviewFixture);
let failRevision = new URLSearchParams(window.location.search).has('failRevision');
const suggestionsPreview = new URLSearchParams(window.location.search).has('suggestions');
let suggestionsState = structuredClone(proposalsFixture);
let failDecision = new URLSearchParams(window.location.search).has('failDecision');
const status = async () => ({ isRepository: true, branch: 'layout-preview', ahead: 0, behind: 0, files: [{ path: 'example.ts', indexStatus: ' ', workTreeStatus: 'M' }] });
const info = () => ({ root, name: root.split('/').at(-1)! });
const document = (path: string, content = Array.from({ length: pathsPreview ? 80 : 2 }, (_, i) => `// Line ${i + 1} in ${path}`).join('\n')) => ({ path, content, language: 'typescript', modifiedAt: 0 });

window.workbench = {
  workspace: {
    current: async () => info(),
    recent: async () => [],
    choose: async () => { emitAgent?.({ type: 'status', status: 'stopped' }); root = root.endsWith('alpha') ? '/layout-fixture/beta' : '/layout-fixture/alpha'; return info(); },
    open: async (next) => { root = next; return info(); },
    close: async () => {},
    list: async () => [{ name: 'example.ts', path: 'example.ts', kind: 'file' }],
    issues: async (workspaceRoot) => factsPreview && workspaceRoot.endsWith('alpha') ? { ...structuredClone(persistedIssues), workspaceRoot } : { workspaceRoot, status: 'missing', activeIssueId: '', issues: [], proposals: [], warnings: [] },
    issueAction: async (workspaceRoot, action, extras) => { applyIssueAction(persistedIssues, action, extras); return { ...structuredClone(persistedIssues), workspaceRoot }; },
    repoFacts: async (workspaceRoot) => factsPreview && workspaceRoot.endsWith('alpha') ? repoFactsSnapshot(workspaceRoot) : { workspaceRoot, path: 'repo_facts.md', status: 'missing' },
    read: async (path) => { if (path.includes('missing')) throw new Error(`File not found: ${path}`); return factsPreview && path === 'repo_facts.md' ? { ...document(path, repoFactsMarkdown), language: 'markdown' } : document(path); }, write: async (path, content) => document(path, content), onChange: () => noop,
  },
  git: {
    status, initialize: async () => status(), history: async () => [], fileDiff: async (path) => ({ path, original: 'const greeting = "Before";', modified: 'const greeting = "After";', language: 'typescript' }),
    stage: status, stageAll: status, unstage: status, discard: async () => ({ discarded: false, status: await status() }), commit: status, push: status,
  },
  terminal: {
    create: async () => `fixture-terminal-${++terminalCount}`, copy: async () => {}, write: noop, resize: noop, dispose: noop,
    onEvent: (listener) => { const timer = setTimeout(() => listener({ type: 'data', sessionId: `fixture-terminal-${terminalCount}`, data: pathsPreview ? '\r\nError /repo/src/app.ts:12:3 — inspect `docs/My kërkesë.md`\r\n' : '\r\nLayout fixture terminal — no shell connected.\r\n' }), 100); return () => clearTimeout(timer); },
  },
  editor: { onCommand: () => noop },
  agent: {
    start: async () => issuesPreview ? (emitAgent?.({ type: 'status', status: 'running' }), { ok: true, state: issuesBridge(persistedIssues) }) : suggestionsPreview ? { ok: true, state: suggestionsState } : planPreview ? { ok: true, state: planState } : response(), submit: response,
    plannerAction: async (action, extras) => {
      if (issuesPreview) { applyIssueAction(persistedIssues, action, extras); return { ok: true, state: issuesBridge(persistedIssues) }; }
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
      emitAgent = listener;
      if (issuesPreview) return () => { emitAgent = undefined; };
      if (pathsPreview) {
        const timer = setTimeout(() => {
          listener({ type: 'state', state: { planner: { issue_state: { active_issue: { issue_id: 'issue-paths', status: 'open', request_summary: 'Inspect /repo/src/app.ts:12:3 and docs/guide.md' }, issues: [] }, worker_state: { latest_diagnostics: { diagnostics: [{ file: 'src/app.ts', path: 'src/app.ts', message: 'Check /repo/src/app.ts:12:3', line: 12, column: 3 }] }, current_run_facts: [{ key: 'layout', value: 'Read /repo/src/app.ts:12:3 for the surrounding implementation.' }], issue_context: { run_diagnostics: [{ issue_id: 'diag-path', summary: 'Fix src/app.ts', file: 'src/app.ts', line: 12 }] } } }, transcript: [{ role: 'assistant', content: 'Read `/repo/src/app.ts:12:3` to understand the handler. The surrounding explanation stays readable. Compare [the guide](docs/guide.md#L8) and `docs/My kërkesë.md`. Missing file: src/missing.ts.\n\n```ts\nconst path = "src/app.ts"; // literal code stays intact\n```\n\n[External documentation](https://example.com/src/app.ts)' }] } });
          listener({ type: 'progress', payload: { type: 'progress', domain: 'worker', action_type: 'cat', path: 'src/app.ts', summary: 'Read /repo/src/app.ts:12:3 and docs/guide.md.' } });
        }, 100);
        return () => clearTimeout(timer);
      }
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

if (new URLSearchParams(window.location.search).has('artifacts')) await import('./artifacts-fixture');
void import('../../src/renderer/src/main');
