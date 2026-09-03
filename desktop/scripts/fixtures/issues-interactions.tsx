import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { AgentIssues } from '../../src/renderer/src/components/AgentIssues';
import { RepoFactsView } from '../../src/renderer/src/components/RepoFactsPanel';
import { AgentWorkspaceContext, type AgentWorkspaceValue } from '../../src/renderer/src/agent/agentWorkspace';
import { initialAgentUiState } from '../../src/shared/agentCore';
import type { WorkbenchApi } from '../../src/shared/contracts';
import type { WorkspaceIssuesSnapshot } from '../../src/shared/workspaceIssues';
import { applyIssueAction, issuesSnapshot } from './workspace-issues';
import { repoFactsSnapshot } from './repo-facts';
import '../../src/renderer/src/styles.css';
import '../../src/renderer/src/issues.css';
import '../../src/renderer/src/repoFacts.css';
import '../../src/renderer/src/agentSuggestions.css';
Object.assign(window, { IS_REACT_ACT_ENVIRONMENT: true });
const host = document.getElementById('fixture')!;
const results = document.getElementById('results')!;
const root = createRoot(host);
const requests: Array<{ action: string; extras: Record<string, unknown> }> = [];
const stores = new Map([['alpha', issuesSnapshot('alpha')], ['beta', { ...issuesSnapshot('beta'), issues: [], proposals: [], activeIssueId: '', status: 'missing' as const }]]);
let selected = 'alpha', revision = 0, fail = false;
let defer: ((snapshot: WorkspaceIssuesSnapshot) => void) | undefined;
let holdRead = false;
let actionRelease: (() => void) | undefined;
let holdAction = false;
const assert = (condition: unknown, message: string): void => { if (!condition) throw new Error(message); };
const pass = (message: string): void => { const row = document.createElement('li'); row.textContent = 'PASS: ' + message; results.append(row); };
const run = async (action: string, extras: Record<string, unknown>) => {
  requests.push({ action, extras });
  if (fail) { fail = false; return false; }
  const target = selected;
  if (holdAction) { holdAction = false; await new Promise<void>(resolve => actionRelease = resolve); }
  applyIssueAction(stores.get(target)!, action, extras); return true;
};
window.workbench = { workspace: {
  issues: (workspaceRoot: string) => holdRead ? new Promise(resolve => { defer = resolve; holdRead = false; }) : Promise.resolve(structuredClone(stores.get(workspaceRoot)!)),
  issueAction: async (workspaceRoot, action, extras) => { if (!(await run(action, extras))) throw new Error(`Could not update ${String(extras.issue_id || 'issue')}`); return structuredClone(stores.get(workspaceRoot)!); },
} } as WorkbenchApi;
const agent = { state: initialAgentUiState, plannerAction: async (action: string, extras: Record<string, unknown>) => {
    if (['create_issue', 'close_issue', 'reopen_issue'].includes(action)) throw new Error('Offline lifecycle action incorrectly used the agent');
    return run(action, extras);
  },
  createIssue: async () => { throw new Error('Offline creation incorrectly used the agent'); },
  decideIssueProposal: async (id: string, decision: string) => { if (!(await run(`${decision}_issue_proposal`, { proposal_id: id }))) throw new Error('Decision failed'); },
} as unknown as AgentWorkspaceValue;
const render = async (workspaceRoot = 'alpha', focusedIssueId?: string) => {
  selected = workspaceRoot;
  await act(async () => root.render(<AgentWorkspaceContext.Provider value={agent}><AgentIssues key={workspaceRoot} workspaceRoot={workspaceRoot} revision={++revision} focusedIssueId={focusedIssueId} onOpenPath={() => {}} /></AgentWorkspaceContext.Provider>));
};
const button = (label: string): HTMLButtonElement => {
  const node = [...host.querySelectorAll<HTMLButtonElement>('button')].find(item => (item.getAttribute('aria-label') || item.textContent) === label);
  assert(node, 'Missing button: ' + label); return node!;
};
const click = async (label: string) => { await act(async () => button(label).click()); };
try {
  await render();
  assert(requests.length === 0, 'Viewing saved issues started an action');
  assert(button('Close issue-042') && button('Reopen issue-001') && button('Accept') && button('Ignore'), 'Management controls are missing');
  assert(!host.textContent?.includes('global-architecture') && !host.textContent?.includes('RUN FACTS'), 'Facts scopes leaked into the issue list');
  pass('A stopped agent still shows saved issues, older closed work, suggestions and management controls');
  await click('Details for issue-042');
  assert(host.textContent?.includes('Lifecycle notes') && host.textContent?.includes('Completed goals'), 'Issue history is missing');
  await click('Close issue-042');
  assert(button('Reopen issue-042'), 'Close did not update saved status');
  await click('Reopen issue-042');
  await click('Continue issue-042');
  assert(requests.slice(-3).map(item => item.action).join(',') === 'close_issue,reopen_issue,continue_issue', 'Actions used an incorrect route');
  assert(stores.get('alpha')!.activeIssueId === 'issue-042', 'Continue did not select the issue');
  pass('Close, Reopen and Continue use structured issue actions and refresh the saved view');
  await click('Ignore');
  assert(stores.get('alpha')!.proposals[0].status === 'ignored', 'Ignore was not recorded');
  await click('Accept');
  assert(button('Close issue-accepted'), 'Accepted suggestion did not become an issue');
  assert(!host.textContent?.includes('Agent-authored'), 'Decided suggestions remained pending');
  pass('Ignore removes a pending suggestion; Accept adds its issue without replacing the active work');
  fail = true;
  await click('Close issue-042');
  assert(host.querySelector('[role="alert"]')?.textContent?.includes('Could not update issue-042'), 'Action failure is hidden');
  assert(button('Close issue-042'), 'Failed action changed the issue status');
  const before = requests.length;
  holdAction = true;
  await act(async () => { button('Close issue-042').click(); button('Close issue-042').click(); });
  assert(requests.length === before + 1, 'Double click queued duplicate mutations');
  await act(async () => actionRelease!());
  pass('Failed actions preserve state and duplicate clicks cannot queue duplicate changes');
  holdRead = true;
  await render();
  await render('beta');
  assert(!host.textContent?.includes('issue-042'), 'Old workspace issues remained visible');
  await act(async () => defer!(issuesSnapshot('alpha')));
  assert(!host.textContent?.includes('issue-042') && host.textContent?.includes('No saved issues yet'), 'A late read crossed workspace boundaries');
  pass('Switching folders clears old issues and ignores late saved-file responses');
  let openedIssue = '';
  await act(async () => root.render(<RepoFactsView ledger={repoFactsSnapshot('alpha').ledger!} onOpenIssue={id => openedIssue = id} />));
  assert(!host.textContent?.includes('Completed goals') && !host.textContent?.includes('Lifecycle notes'), 'Facts retained issue management details');
  await click('View issue-042 in Issues');
  await render('alpha', openedIssue);
  assert(button('Details for issue-042').getAttribute('aria-expanded') === 'true', 'Related issue was not expanded');
  assert(host.textContent?.includes('Lifecycle notes'), 'Related issue history did not open');
  pass('Fact provenance links to the issue view while facts stay focused on repository knowledge');
  document.body.dataset.testResult = 'passed';
} catch (error) { const row = document.createElement('li'); row.textContent = 'FAIL: ' + error; results.append(row); document.body.dataset.testResult = 'failed'; throw error; }
