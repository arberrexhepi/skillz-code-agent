import type { EventMapping, NodeId, RunEvent } from './types';
const validationActions = new Set(['diagnose', 'run_check', 'run_route_check', 'read_diagnostics', 'ingest_log', 'show_run_issue', 'list_run_issues', 'resolve_run_issue', 'reopen_run_issue']);
export function mapEvent(event?: RunEvent): EventMapping {
  if (!event) return { primary: 'intake', related: [], inferred: [], label: 'Ready to explore', failed: false };
  const data = event.data, action = String(data.action_type || data.action || '');
  let primary: NodeId = 'planner', related: NodeId[] = [], inferred: NodeId[] = [];
  if (event.type === 'input') primary = 'intake';
  else if (event.type === 'response') primary = data.status === 'completed' ? 'completion' : data.status === 'failed' ? 'recovery' : 'planner';
  else if (event.type === 'error' || event.type === 'interrupted') primary = 'recovery';
  else if (event.type === 'state') {
    primary = data.pendingVerification ? 'validation' : /discover/.test(String(data.status)) ? 'discovery' : 'planner';
  } else if (event.type === 'progress') {
    if (action.startsWith('model_call')) { primary = 'model'; if (action === 'model_call_start') inferred = ['context']; if (action === 'model_call_finish') inferred = ['grammar']; if (/error|interrupted/.test(action)) related = ['recovery']; }
    else if (data.type === 'goal_start') { primary = 'planner'; inferred = ['context']; }
    else if (data.type === 'goal_finish') { primary = 'planner'; inferred = ['memory']; }
    else if (/discovery/.test(action)) primary = 'discovery';
    else if (validationActions.has(action)) { primary = 'validation'; inferred = ['evidence']; }
    else if (/fact|issue|checkpoint/.test(action)) primary = 'memory';
    else if (/validat|diagnostic|test|verify|lint|typecheck/.test(action)) { primary = 'validation'; inferred = ['evidence']; }
    else if (/skill|context|^drop$|focus|expand|collapse/.test(action)) primary = 'context';
    else if (action === 'finish') { primary = data.ok === false ? 'recovery' : 'validation'; }
    else if (action === 'turn_thought') primary = 'model';
    else { primary = 'tools'; inferred = ['grammar', 'guards', 'evidence']; }
    if (data.ok === false) related = [...related, 'recovery'];
  }
  return { primary, related, inferred, label: String(data.summary || data.message || data.action_type || data.status || (event.type === 'input' ? 'Request received' : 'Planner request')), failed: data.ok === false || event.type === 'error' || /error|interrupted/.test(action) };
}
export function timeLabel(ms: number): string { const seconds = Math.max(0, ms) / 1000; return `${Math.floor(seconds / 60).toString().padStart(2, '0')}:${Math.floor(seconds % 60).toString().padStart(2, '0')}.${Math.floor((seconds % 1) * 10)}`; }
export function eventDelay(events: RunEvent[], cursor: number, speed: number, skipIdle: boolean): number {
  const gap = Math.max(0, (events[cursor + 1]?.at || 0) - (events[cursor]?.at || 0));
  return Math.max(16, (skipIdle ? Math.min(gap, 1600) : gap) / speed);
}
export function mergeEvents(current: RunEvent[], incoming: RunEvent[]): RunEvent[] {
  const events = new Map(current.map(event => [event.seq, event]));
  for (const event of incoming) events.set(event.seq, event);
  return [...events.values()].sort((a, b) => a.seq - b.seq);
}
