const assert = require('node:assert/strict');
const { test } = require('node:test');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const loadTypeScript = require('./load-ts.cjs');
const { DEFAULT_WORKSPACE_VIEW, normalizeWorkspaceView, agentWidthBounds, clampAgentWidth, keyboardAgentWidth, readWorkspaceView, writeWorkspaceView, WorkspaceViewControls, WorkspaceLayout } = loadTypeScript(() => ({
  ...require('../src/shared/workspaceView.ts'),
  ...require('../src/renderer/src/components/WorkspaceViewControls.tsx'),
  ...require('../src/renderer/src/components/WorkspaceLayout.tsx'),
}));

test('view defaults and malformed saved values remain usable', () => {
  for (const value of [null, [], 'bad', { editorVisible: 'false', agentWidth: NaN }]) {
    assert.deepEqual(normalizeWorkspaceView(value), DEFAULT_WORKSPACE_VIEW);
  }
  assert.deepEqual(normalizeWorkspaceView({ editorVisible: false, agentWidth: 582.4 }), { editorVisible: false, agentWidth: 582 });
  assert.equal(normalizeWorkspaceView({ agentWidth: -20 }).agentWidth, 300);
  assert.equal(normalizeWorkspaceView({ agentWidth: Infinity }).agentWidth, 390);
});

test('view persistence is isolated per workspace', () => {
  const data = new Map();
  const storage = { getItem: (key) => data.get(key), setItem: (key, value) => data.set(key, value) };
  writeWorkspaceView(storage, '/repo/alpha', { editorVisible: false, agentWidth: 610 });
  writeWorkspaceView(storage, '/repo/beta', { editorVisible: true, agentWidth: 480 });
  assert.deepEqual(readWorkspaceView(storage, '/repo/alpha'), { editorVisible: false, agentWidth: 610 });
  assert.deepEqual(readWorkspaceView(storage, '/repo/beta'), { editorVisible: true, agentWidth: 480 });
  assert.deepEqual(readWorkspaceView(storage, '/repo/new'), DEFAULT_WORKSPACE_VIEW);
});

test('corrupt or unavailable storage does not break layout controls', () => {
  assert.deepEqual(readWorkspaceView({ getItem: () => '{invalid' }, '/repo'), DEFAULT_WORKSPACE_VIEW);
  assert.deepEqual(readWorkspaceView({ getItem: () => { throw Error('Blocked'); } }, '/repo'), DEFAULT_WORKSPACE_VIEW);
  assert.doesNotThrow(() => writeWorkspaceView({ setItem: () => { throw Error('Full'); } }, '/repo', DEFAULT_WORKSPACE_VIEW));
});

test('resizing reserves editor space and clamps extreme drags', () => {
  const bounds = agentWidthBounds(1440, 268);
  assert.deepEqual(bounds, { min: 300, max: 846 });
  assert.equal(clampAgentWidth(600, bounds), 600);
  assert.equal(clampAgentWidth(5000, bounds), 846);
  assert.equal(clampAgentWidth(-5000, bounds), 300);
});

test('minimum window and unusually narrow containers never overallocate panels', () => {
  for (const [total, sidebar] of [[1050, 225], [650, 225], [200, 225]]) {
    const bounds = agentWidthBounds(total, sidebar);
    assert.ok(bounds.min >= 0);
    assert.ok(bounds.max >= bounds.min);
    assert.ok(bounds.max <= Math.max(0, total - sidebar - 6));
  }
});

test('temporary window clamps do not alter preferred width', () => {
  const view = { editorVisible: true, agentWidth: 800 };
  assert.equal(clampAgentWidth(view.agentWidth, agentWidthBounds(1050, 225)), 499);
  assert.equal(clampAgentWidth(view.agentWidth, agentWidthBounds(1560, 268)), 800);
  assert.equal(view.agentWidth, 800);
});

test('keyboard resizing moves the right-hand panel in the expected direction', () => {
  const bounds = { min: 300, max: 800 };
  assert.equal(keyboardAgentWidth('ArrowLeft', 400, bounds), 410);
  assert.equal(keyboardAgentWidth('ArrowRight', 400, bounds), 390);
  assert.equal(keyboardAgentWidth('ArrowLeft', 400, bounds, true), 450);
  assert.equal(keyboardAgentWidth('ArrowRight', 300, bounds), 300);
  assert.equal(keyboardAgentWidth('Home', 400, bounds), 300);
  assert.equal(keyboardAgentWidth('End', 400, bounds), 800);
  assert.equal(keyboardAgentWidth('Tab', 400, bounds), undefined);
});

test('view toggle exposes state and both controls call their actions', () => {
  let toggles = 0;
  let resets = 0;
  const props = { editorVisible: false, onToggleEditor: () => toggles++, onReset: () => resets++ };
  const html = renderToStaticMarkup(React.createElement(WorkspaceViewControls, props));
  assert.match(html, /aria-pressed="false"/);
  assert.match(html, /Show editor/);
  const buttons = React.Children.toArray(WorkspaceViewControls(props).props.children);
  buttons[0].props.onClick();
  buttons[1].props.onClick();
  assert.equal(toggles, 1);
  assert.equal(resets, 1);
});

test('hidden editor remains mounted along with agent and dock', () => {
  const html = renderToStaticMarkup(React.createElement(WorkspaceLayout, {
    view: { editorVisible: false, agentWidth: 500 }, onAgentResize() {},
    sidebar: 'Sidebar', editor: React.createElement('textarea', { defaultValue: 'Unsaved code' }),
    agent: React.createElement('textarea', { defaultValue: 'Agent draft' }), dock: 'Live terminal',
  }));
  assert.match(html, /class="workbench editor-hidden"/);
  assert.match(html, /id="workspace-editor"[^>]*hidden=""/);
  assert.match(html, /Unsaved code/);
  assert.match(html, /Agent draft/);
  assert.match(html, /Live terminal/);
  assert.match(html, /role="separator"[^>]*tabindex="-1"[^>]*hidden=""/);
});
