import { test } from 'node:test';
import assert from 'node:assert/strict';
import { eventDelay, mapEvent, mergeEvents } from '../src/runtime/trace';
import { sampleRun } from '../src/runtime/sample';
import { atoms, connections } from '../src/catalog';
test('live tool failures light tools and recovery while internal phases remain inferred', () => {
  const event = { seq: 1, at: 0, type: 'progress', data: { action_type: 'patch_file', ok: false, path: 'server.ts' } };
  const mapped = mapEvent(event);
  assert.equal(mapped.primary, 'tools'); assert.equal(mapped.failed, true);
  assert.ok(mapped.related.includes('recovery')); assert.ok(mapped.inferred.includes('guards')); assert.ok(mapped.inferred.includes('grammar'));
});
test('planner responses do not mark completion while approval or discovery remains pending', () => {
  assert.equal(mapEvent({ seq: 1, at: 0, type: 'response', data: { status: 'awaiting-input' } }).primary, 'planner');
  assert.equal(mapEvent({ seq: 2, at: 5, type: 'response', data: { status: 'completed' } }).primary, 'completion');
});
test('actual beta validation commands and transient diagnostics map to verification', () => {
  for (const action_type of ['diagnose', 'run_check', 'run_route_check', 'read_diagnostics', 'show_run_issue']) {
    assert.equal(mapEvent({ seq: 1, at: 0, type: 'progress', data: { action_type, ok: true } }).primary, 'validation');
  }
});
test('worker finish and individual goal completion are not completed planner runs', () => {
  assert.equal(mapEvent({ seq: 1, at: 0, type: 'progress', data: { action_type: 'finish', ok: true } }).primary, 'validation');
  assert.equal(mapEvent({ seq: 2, at: 1, type: 'progress', data: { action_type: 'finish', ok: false } }).primary, 'recovery');
  assert.equal(mapEvent({ seq: 3, at: 2, type: 'progress', data: { type: 'goal_finish' } }).primary, 'planner');
});
test('replay uses event time, speed, and optional idle compression deterministically', () => {
  const events = [{ seq: 1, at: 100, type: 'input', data: {} }, { seq: 2, at: 10100, type: 'response', data: {} }];
  assert.equal(eventDelay(events, 0, 2, false), 5000);
  assert.equal(eventDelay(events, 0, 2, true), 800);
  assert.equal(eventDelay(events, 0, 0.5, false), 20000);
});
test('poll reconnection merges overlapping events without duplicate replay steps', () => {
  const first = sampleRun.events.slice(0, 4), reconnect = sampleRun.events.slice(2, 7);
  assert.deepEqual(mergeEvents(first, reconnect), sampleRun.events.slice(0, 7));
});
test('catalog endpoints resolve and illustrative sample includes a failure and recovery', () => {
  assert.equal(atoms.length, 13); assert.equal(new Set(atoms.map(atom => atom.id)).size, 13);
  for (const edge of connections) for (const id of [edge.from, edge.to]) assert.ok(atoms.some(atom => atom.id === id));
  assert.equal(sampleRun.sample, true); assert.ok(sampleRun.events.some(event => mapEvent(event).failed));
  assert.equal(mapEvent(sampleRun.events.at(-1)).primary, 'completion');
});
