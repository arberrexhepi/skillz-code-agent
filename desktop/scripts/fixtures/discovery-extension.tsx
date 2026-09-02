import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { DiscoveryExtensionDecisionCard } from '../../src/renderer/src/components/agent/AgentDecisionCard';
import { extension } from './discovery-extension';
import '../../src/renderer/src/styles.css';
import '../../src/renderer/src/agentTypography.css';

Object.assign(window, { IS_REACT_ACT_ENVIRONMENT: true });
const host = document.getElementById('fixture')!;
const results = document.getElementById('results')!;
const assert = (condition: unknown, message: string): void => { if (!condition) throw new Error(message); };
const buttons = (): HTMLButtonElement[] => [...host.querySelectorAll('button')];
const show = (message: string): void => { const item = document.createElement('li'); item.textContent = message; results.appendChild(item); };
const root = createRoot(host);
const calls: boolean[] = [];
let finish!: (ok: boolean) => void;
let reject!: (reason: Error) => void;
const action = (accept: boolean): Promise<boolean> => {
  calls.push(accept);
  return new Promise((resolve, fail) => { finish = resolve; reject = fail; });
};
await act(async () => root.render(<div className="agent-panel"><DiscoveryExtensionDecisionCard extension={extension} busy={false} onDecision={action} /></div>));
try {
  // Two clicks in one tick must not queue two requests, even before React renders the busy state.
  await act(async () => { buttons()[0].click(); buttons()[0].click(); buttons()[1].click(); });
  assert(calls.length === 1 && calls[0] === true, 'Duplicate or crossed decision submitted');
  assert(buttons().every((button) => button.disabled), 'Pending decision controls remain enabled');
  show('PASS: approval sent once; duplicate and competing clicks blocked');
  await act(async () => finish(false));
  assert(host.textContent?.includes('Could not apply the decision'), 'Failed request is not visible');
  assert(buttons().every((button) => !button.disabled), 'Failed decision cannot be retried');
  assert(host.textContent?.includes(extension.proposal), 'Failure lost the proposal');
  show('PASS: failure preserves the proposal and restores both choices');
  await act(async () => buttons()[1].click());
  assert(calls.length === 2 && calls[1] === false, 'Decline does not send the negative decision');
  await act(async () => reject(new Error('Connection interrupted')));
  assert(host.textContent?.includes('Connection interrupted'), 'Thrown error is not visible');
  assert(buttons().every((button) => !button.disabled), 'Thrown error leaves controls locked');
  show('PASS: decline sends the correct decision; connection errors remain retryable');
  await act(async () => buttons()[0].click());
  await act(async () => finish(true));
  assert(!host.querySelector('[role="alert"]'), 'Retry retains stale error');
  show('PASS: successful retry clears the error');
  await act(async () => root.render(<div className="agent-panel"><DiscoveryExtensionDecisionCard key="next" extension={{ ...extension, request_id: 'next', additional_turns: 1 }} busy={false} onDecision={action} /></div>));
  assert(buttons()[0].textContent?.includes('Allow 1 more turn'), 'Next request did not update');
  show('PASS: a new request displays its own turn allowance');
  // Leave the representative two-turn request visible for visual review.
  await act(async () => root.render(<div className="agent-panel"><DiscoveryExtensionDecisionCard key="preview" extension={extension} busy={false} onDecision={action} /></div>));
  document.body.dataset.testResult = 'passed';
} catch (error) {
  show('FAIL: ' + String(error));
  document.body.dataset.testResult = 'failed';
  throw error;
}
