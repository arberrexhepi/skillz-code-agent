const assert = require('node:assert/strict');
const { test } = require('node:test');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const loadTypeScript = require('./load-ts.cjs');
const {
  AgentComposerControls,
  DEFAULT_AUTO_MAX_TURNS,
  normalizeAutoMaxTurns,
  submitComposerInstruction,
} = loadTypeScript(() => require('../src/renderer/src/components/agent/AgentComposerControls.tsx'));

test('auto turn limits stay within the supported range', () => {
  assert.equal(normalizeAutoMaxTurns(''), DEFAULT_AUTO_MAX_TURNS);
  assert.equal(normalizeAutoMaxTurns('4.9'), 4);
  assert.equal(normalizeAutoMaxTurns(-2), 1);
  assert.equal(normalizeAutoMaxTurns(200), 25);
});

test('normal send and auto send use their respective agent paths', async () => {
  const calls = [];
  const agent = {
    submit: async (text) => { calls.push(['submit', text]); return true; },
    plannerAction: async (action, extras) => { calls.push(['plannerAction', action, extras]); return true; },
  };
  assert.equal(await submitComposerInstruction(agent, '  normal request  ', false, 8), true);
  assert.equal(await submitComposerInstruction(agent, '  continuous request  ', true, '6'), true);
  assert.deepEqual(calls, [
    ['submit', 'normal request'],
    ['plannerAction', 'start_continuous', { max_cycles: 6, prompt: 'continuous request' }],
  ]);
});

test('max turns input is only visible while Auto is checked', () => {
  const props = { maxTurns: '3', disabled: false, onAutoEnabledChange() {}, onMaxTurnsChange() {} };
  const off = renderToStaticMarkup(React.createElement(AgentComposerControls, { ...props, autoEnabled: false }));
  assert.match(off, />Auto</);
  assert.match(off, /type="checkbox"/);
  assert.doesNotMatch(off, /Maximum auto turns/);

  const on = renderToStaticMarkup(React.createElement(AgentComposerControls, { ...props, autoEnabled: true }));
  assert.match(on, /Max turns/);
  assert.match(on, /aria-label="Maximum auto turns"/);
  assert.match(on, /min="1"/);
  assert.match(on, /max="25"/);
});
