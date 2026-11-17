import { test } from 'node:test';
import assert from 'node:assert/strict';
import { followPlayback, initialPlayback, seekPlayback, synchronizePlayback } from '../src/runtime/playbackState';
import type { Run } from '../src/runtime/types';

function recording(id: string, count: number, status: Run['status'] = 'running'): Run {
  return { id, prompt: 'Fixture task', startedAt: '2026-10-06T12:00:00Z', status,
    selection: { provider: 'fixture', model: 'fixture' },
    events: Array.from({ length: count }, (_, i) => ({ seq: i + 1, at: i * 100, type: 'progress', data: {} })) };
}

test('live batches and the final response follow the latest event across a run switch', () => {
  let state = initialPlayback(recording('sample', 12, 'completed'), false);
  state = synchronizePlayback(state, recording('first', 0), true);
  state = synchronizePlayback(state, recording('first', 4, 'awaiting-input'), true);
  assert.equal(state.cursor, 3);
  state = synchronizePlayback(state, recording('first', 8), true);
  assert.equal(state.cursor, 7);
  state = synchronizePlayback(state, recording('first', 9, 'completed'), false);
  assert.equal(state.cursor, 8);
  state = synchronizePlayback(state, recording('next', 9, 'awaiting-input'), true);
  assert.equal(state.cursor, 8);
  assert.equal(state.runId, 'next');
});

test('manual inspection stays on its step until return to live, including late terminal events', () => {
  let run = recording('live', 4);
  let state = seekPlayback(initialPlayback(run, true), run, true, 1);
  run = recording('live', 9);
  state = synchronizePlayback(state, run, true);
  assert.equal(state.cursor, 1);
  assert.equal(state.following, false);
  state = followPlayback(state, run, true);
  assert.equal(state.cursor, 8);
  state = synchronizePlayback(state, recording('live', 10, 'interrupted'), false);
  assert.equal(state.cursor, 9);
});

test('loading an archive starts at the first step and never inherits the previous live cursor', () => {
  let state = initialPlayback(recording('live', 6), true);
  state = synchronizePlayback(state, recording('archive', 0, 'completed'), false);
  state = synchronizePlayback(state, recording('archive', 6, 'completed'), false);
  assert.equal(state.cursor, 0);
  state = { ...seekPlayback(state, recording('archive', 6, 'completed'), false, 3), playing: true };
  assert.equal(synchronizePlayback(state, recording('archive', 6, 'completed'), false).playing, true);
  assert.equal(synchronizePlayback({ ...state, cursor: 5 }, recording('archive', 6, 'completed'), false).playing, false);
});

test('a new live run resets manual inspection even with an unchanged event count', () => {
  const first = recording('first', 5);
  const manual = seekPlayback(initialPlayback(first, true), first, true, 1);
  const next = synchronizePlayback(manual, recording('next', 5), true);
  assert.equal(next.cursor, 4);
  assert.equal(next.following, true);
});
