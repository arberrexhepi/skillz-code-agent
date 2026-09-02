import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { RepoFactsPanel } from '../../src/renderer/src/components/RepoFactsPanel';
import type { WorkbenchApi } from '../../src/shared/contracts';
import type { RepoFactsSnapshot } from '../../src/shared/repoFacts';
import { repoFactsSnapshot } from './repo-facts';
import '../../src/renderer/src/styles.css';
import '../../src/renderer/src/repoFacts.css';
Object.assign(window, { IS_REACT_ACT_ENVIRONMENT: true });
const host = document.getElementById('fixture')!;
const results = document.getElementById('results')!;
const root = createRoot(host);
const assert = (condition: unknown, message: string): void => { if (!condition) throw new Error(message); };
const pass = (message: string): void => { const row = document.createElement('li'); row.textContent = 'PASS: ' + message; results.appendChild(row); };
const pending: Array<{ root: string; resolve: (snapshot: RepoFactsSnapshot) => void; reject: (error: Error) => void }> = [];
const opened: string[] = [];
window.workbench = { workspace: { repoFacts: (workspaceRoot: string) => new Promise((resolve, reject) => pending.push({ root: workspaceRoot, resolve, reject })) } } as WorkbenchApi;
let revision = 0;
const render = async (workspaceRoot = 'alpha') => {
  await act(async () => root.render(<RepoFactsPanel key={workspaceRoot} workspaceRoot={workspaceRoot} revision={++revision} onOpenPath={(path) => opened.push(path)} />));
  const request = pending.shift();
  assert(request?.root === workspaceRoot, 'Wrong workspace requested');
  return request!;
};
const button = (label: string) => {
  const found = [...host.querySelectorAll<HTMLButtonElement>('button')].find((item) => item.textContent === label);
  assert(found, 'Missing button ' + label);
  return found!;
};
const setValue = async (label: string, value: string) => {
  const field = [...host.querySelectorAll('label')].find((item) => item.firstChild?.textContent === label)?.querySelector('input,select') as HTMLInputElement | HTMLSelectElement;
  assert(field, 'Missing field ' + label);
  await act(async () => {
    Object.getOwnPropertyDescriptor(field instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype, 'value')!.set!.call(field, value);
    field.dispatchEvent(new Event(field instanceof HTMLSelectElement ? 'change' : 'input', { bubbles: true }));
  });
};
try {
  const initial = await render();
  assert(host.textContent?.includes('Loading'), 'Initial loading state missing');
  await act(async () => initial.resolve(repoFactsSnapshot('alpha')));
  assert(host.textContent?.includes('facts.offline'), 'Facts were not rendered');
  await act(async () => button('Open source ↗').click());
  assert(opened.join() === 'repo_facts.md', 'Source action opened the wrong file');
  pass('Saved facts render without an agent; source action opens repo_facts.md');
  await setValue('Record type', 'goal');
  assert(host.textContent?.includes('facts.offline') && !host.textContent?.includes('facts.storage'), 'Fact type filter is wrong');
  await setValue('Search records', 'not a saved fact');
  assert(host.textContent?.includes('No matching records'), 'Search empty state missing');
  await act(async () => button('Clear filters').click());
  await setValue('Scope', 'issue-039');
  assert(host.textContent?.includes('discovery.approval') && !host.textContent?.includes('facts.offline'), 'Scope filter leaked other records');
  await setValue('Scope', 'all');
  pass('Search, type, scope, and clearing filters update rendered records');

  const oldRefresh = await render();
  const freshRefresh = await render();
  const fresh = repoFactsSnapshot('alpha');
  fresh.ledger!.issues[1].facts[0].value = 'Newest saved facts';
  await act(async () => freshRefresh.resolve(fresh));
  await act(async () => oldRefresh.resolve(repoFactsSnapshot('alpha')));
  assert(host.textContent?.includes('Newest saved facts'), 'Old refresh replaced newer data');
  pass('A late refresh cannot overwrite a newer saved snapshot');

  const oldRepo = await render();
  const beta = await render('beta');
  assert(!host.textContent?.includes('Newest saved facts'), 'Old repository facts remained visible after switching');
  const next = repoFactsSnapshot('beta');
  next.ledger!.issues[1].facts[0].value = 'Beta repository facts';
  await act(async () => beta.resolve(next));
  await act(async () => oldRepo.reject(new Error('Old repository unavailable')));
  assert(host.textContent?.includes('Beta repository facts') && !host.textContent?.includes('Old repository unavailable'), 'Old repository response leaked');
  pass('Repository switches discard old facts and late errors');

  const malformed = await render('beta');
  await act(async () => malformed.resolve({ workspaceRoot: 'beta', path: 'repo_facts.md', status: 'invalid', error: 'Malformed saved JSON' }));
  assert(host.textContent?.includes('Malformed saved JSON') && !host.textContent?.includes('Beta repository facts'), 'Malformed file was shown as valid');
  assert(!button('Open source ↗').disabled, 'Malformed source cannot be inspected');
  const deleted = await render('beta');
  await act(async () => deleted.resolve({ workspaceRoot: 'beta', path: 'repo_facts.md', status: 'missing' }));
  assert(host.textContent?.includes('No repository facts yet') && button('Open source ↗').disabled, 'Missing file is not distinguished from a corrupt file');
  pass('Malformed and deleted files replace previous data with distinct states');

  const failed = await render('beta');
  await act(async () => failed.reject(new Error('Unable to read file')));
  assert(host.querySelector('[role="alert"]')?.textContent?.includes('Unable to read file'), 'Read error missing');
  await act(async () => button('Retry').click());
  const retried = pending.shift()!;
  assert(retried.root === 'beta', 'Retry requested the wrong repository');
  await act(async () => retried.resolve(repoFactsSnapshot('beta')));
  assert(!host.querySelector('[role="alert"]') && host.textContent?.includes('facts.offline'), 'Retry failed to restore the view');
  pass('Read failures can be retried without starting or changing the agent');
  document.body.dataset.testResult = 'passed';
} catch (error) {
  const row = document.createElement('li'); row.textContent = 'FAIL: ' + String(error); results.appendChild(row);
  document.body.dataset.testResult = 'failed';
  throw error;
}
