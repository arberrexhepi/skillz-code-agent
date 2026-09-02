// Runs in the browser fixture so native dialog behavior and React effects are real.
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { AgentWorkspaceContext, type AgentWorkspaceValue } from '../../src/renderer/src/agent/agentWorkspace';
import { PlanDecisionCard } from '../../src/renderer/src/components/agent/PlanDecisionCard';
import { initialAgentUiState } from '../../src/shared/agentCore';
import { reviewPlan } from './plan-review';
import '../../src/renderer/src/styles.css';
import '../../src/renderer/src/agentTypography.css';
import '../../src/renderer/src/planReview.css';

Object.assign(window, { IS_REACT_ACT_ENVIRONMENT: true });
const host = document.getElementById('fixture')!;
const results = document.getElementById('results')!;
const assert = (condition: unknown, message: string): void => { if (!condition) throw new Error(message); };
const button = (label: string): HTMLButtonElement => {
  const scope = host.querySelector('dialog[open]') || host;
  const found = [...scope.querySelectorAll('button')].find((item) => (item.getAttribute('aria-label') || item.textContent) === label);
  if (!found) throw new Error(`Button missing: ${label}`);
  return found;
};
const click = async (label: string): Promise<void> => { await act(async () => button(label).click()); };
const isOpen = (): boolean => Boolean(host.querySelector<HTMLDialogElement>('dialog')?.open);
const escape = async (): Promise<void> => { await act(async () => host.querySelector('dialog')!.dispatchEvent(new Event('cancel', { cancelable: true }))); };
const input = (): HTMLTextAreaElement => host.querySelector('textarea')!;
const fill = async (text: string): Promise<void> => {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(input(), text);
    input().dispatchEvent(new Event('input', { bubbles: true }));
  });
};
const mount = async (paused = false) => {
  const root = createRoot(host);
  let finish!: (ok: boolean) => void;
  let reject!: (error: Error) => void;
  let calls = 0;
  let action = '';
  const response = new Promise<boolean>((resolve, fail) => { finish = resolve; reject = fail; });
  // Only the fields used by this component are needed in this isolated fixture.
  const agent = {
    state: { ...initialAgentUiState, bridge: { transcript: [], planner: paused
      ? { execution_paused: true, paused_plan: reviewPlan }
      : { pending_plan: reviewPlan } } },
    plannerAction: (name: string) => { calls += 1; action = name; return response; },
  } as AgentWorkspaceValue;
  await act(async () => root.render(<AgentWorkspaceContext.Provider value={agent}><div className="agent-panel"><PlanDecisionCard /></div></AgentWorkspaceContext.Provider>));
  return { finish, reject, count: () => calls, action: () => action, cleanup: async () => { await act(async () => { finish(true); root.unmount(); }); } };
};

const cases: Array<[string, (fixture: Awaited<ReturnType<typeof mount>>) => Promise<void>, boolean?]> = [
  ['Approval dismisses before execution finishes; reopened review can close while pending', async (fixture) => {
    await click('Review full plan ↗');
    await click('Approve plan');
    assert(!isOpen(), 'Approval left the dialog blocking the workspace');
    assert(fixture.action() === 'approve_plan', 'Wrong planner action');
    await click('Review full plan ↗');
    assert(button('Approve plan').disabled, 'Duplicate approval enabled');
    assert(!button('Close plan review').disabled, 'Close disabled during execution');
    await click('Close plan review');
    assert(!isOpen(), 'Close did not dismiss during execution');
    await click('Review full plan ↗');
    await escape();
    assert(!isOpen(), 'Escape did not dismiss during execution');
    assert(fixture.count() === 1, 'Dismissing sent another action');
  }],
  ['Resume dismisses immediately while execution continues', async (fixture) => {
    await click('Review full plan ↗');
    await click('Resume');
    assert(!isOpen(), 'Resume left the dialog open');
    assert(fixture.action() === 'continue_issue', 'Wrong resume action');
  }, true],
  ['Rejection dismisses before acknowledgement', async () => {
    await click('Review full plan ↗');
    await click('Reject');
    assert(!isOpen(), 'Rejection left the dialog open');
  }],
  ['Revision can be dismissed without cancelling; a late result leaves reopened review intact', async (fixture) => {
    await click('Suggest plan changes');
    await fill('Keep the schema unchanged.');
    await click('Request revised plan');
    assert(fixture.action() === 'revise_plan', 'Revision not submitted');
    assert(isOpen(), 'Revision should remain available until dismissed');
    assert(button('Revising plan…').disabled, 'Duplicate revision enabled');
    await click('Close plan review');
    assert(!isOpen(), 'Close did not dismiss pending revision');
    await click('Review full plan ↗');
    await act(async () => fixture.finish(true));
    assert(isOpen(), 'Late result closed a newer review');
    await click('Suggest plan changes');
    assert(input().value === 'Keep the schema unchanged.', 'Dismissal or late result erased draft');
  }],
  ['Escape works during revision and failure preserves the draft for retry', async (fixture) => {
    await click('Suggest plan changes');
    await fill('Add keyboard checks.');
    await click('Request revised plan');
    await escape();
    assert(!isOpen(), 'Escape did not dismiss pending revision');
    await act(async () => fixture.finish(false));
    await click('Suggest plan changes');
    assert(input().value === 'Add keyboard checks.', 'Failure lost the draft');
    assert(!button('Request revised plan').disabled, 'Revision cannot be retried');
  }],
  ['Thrown request errors release the action lock without reopening the dialog', async (fixture) => {
    await click('Review full plan ↗');
    await click('Approve plan');
    await act(async () => fixture.reject(new Error('Connection lost')));
    assert(!isOpen(), 'Failed action reopened the dialog');
    assert(host.textContent?.includes('Connection lost'), 'Failure is not visible');
    await click('Review full plan ↗');
    assert(!button('Approve plan').disabled, 'Failure left approval locked');
  }],
];

for (const [name, run, paused] of cases) {
  const fixture = await mount(paused);
  const result = document.createElement('li');
  try { await run(fixture); result.textContent = `PASS: ${name}`; }
  catch (error) { result.textContent = `FAIL: ${name}: ${error instanceof Error ? error.message : error}`; }
  finally { await fixture.cleanup(); results.append(result); }
}
