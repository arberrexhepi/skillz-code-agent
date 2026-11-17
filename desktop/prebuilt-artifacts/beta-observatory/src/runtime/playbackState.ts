import type { Run } from './types';

export interface PlaybackState { runId: string; cursor: number; playing: boolean; following: boolean; seenLive: boolean }
export function initialPlayback(run: Run, live: boolean): PlaybackState {
  return { runId: run.id, cursor: live ? Math.max(0, run.events.length - 1) : 0, playing: false, following: true, seenLive: live };
}
export function synchronizePlayback(state: PlaybackState, run: Run, live: boolean): PlaybackState {
  if (state.runId !== run.id) return initialPlayback(run, live);
  const seenLive = state.seenLive || live;
  const last = Math.max(0, run.events.length - 1);
  const cursor = state.following && seenLive ? last : Math.min(state.cursor, last);
  const playing = state.playing && !live && cursor < last;
  if (cursor === state.cursor && seenLive === state.seenLive && playing === state.playing) return state;
  return { ...state, cursor, seenLive, playing };
}
export function seekPlayback(state: PlaybackState, run: Run, live: boolean, index: number): PlaybackState {
  return { ...synchronizePlayback(state, run, live), cursor: Math.min(Math.max(0, index), Math.max(0, run.events.length - 1)), playing: false, following: false };
}
export function followPlayback(state: PlaybackState, run: Run, live: boolean): PlaybackState {
  return { ...synchronizePlayback(state, run, live), cursor: Math.max(0, run.events.length - 1), playing: false, following: true };
}
export function pendingDecision(run: Run): string {
  const planner = run.state?.planner;
  if (planner?.pending_discovery) return 'Choose discovery mode';
  if (planner?.pending_discovery_extension) return 'Review discovery extension';
  if (planner?.pending_plan || planner?.paused_plan) return 'Review the proposed plan';
  if (planner?.suggested_next_actions?.some(action => action.type === 'retry_failed_goal')) return 'Review retry options';
  return 'The agent is awaiting your input';
}
