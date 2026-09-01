import type { AgentBridgeState, AgentProgressMessage, AgentTranscriptEntry, DiagnosticItem, DiscoveryResult, GoalExecutionResult, IssueSummary, LatestReview, PlannerPlan, RuntimeOptionsPayload, SuggestedAction } from './agentTypes';

export type AgentStatus = 'stopped' | 'starting' | 'running' | 'error';

export interface AgentUiState {
  bridge: AgentBridgeState;
  status: AgentStatus;
  activity: AgentProgressMessage[];
  runtimeOptions?: RuntimeOptionsPayload;
  notice: string;
  pendingAction: string;
}

export const initialAgentUiState: AgentUiState = {
  bridge: { planner: {}, transcript: [] },
  status: 'stopped',
  activity: [],
  notice: '',
  pendingAction: '',
};

export type AgentUiAction =
  | { type: 'bridge-state'; state: AgentBridgeState }
  | { type: 'progress'; progress: AgentProgressMessage }
  | { type: 'status'; status: AgentStatus; message?: string }
  | { type: 'notice'; message: string }
  | { type: 'pending'; action: string }
  | { type: 'runtime-options'; options: RuntimeOptionsPayload }
  | { type: 'reset' };

export function reduceAgentUi(state: AgentUiState, action: AgentUiAction): AgentUiState {
  switch (action.type) {
    case 'bridge-state': return { ...state, bridge: action.state };
    case 'progress': {
      const bridge = action.progress.state || state.bridge;
      return { ...state, bridge, activity: [...state.activity, action.progress].slice(-250) };
    }
    case 'status': return { ...state, status: action.status, notice: action.message || (action.status === 'running' ? '' : state.notice) };
    case 'notice': return { ...state, notice: action.message };
    case 'pending': return { ...state, pendingAction: action.action };
    case 'runtime-options': return { ...state, runtimeOptions: action.options };
    case 'reset': return initialAgentUiState;
  }
}

export function combineSuggestedActions(state: AgentBridgeState): SuggestedAction[] {
  return [
    ...(state.planner.suggested_next_actions || []).map((action) => ({ ...action, source: 'planner' as const })),
    ...(state.planner.worker_state?.suggested_next_actions || []).map((action) => ({ ...action, source: 'worker' as const })),
  ];
}

export function groupDiagnostics(state: AgentBridgeState): Record<string, DiagnosticItem[]> {
  const latest = state.planner.worker_state?.latest_diagnostics;
  const diagnostics = [...(latest?.diagnostics || []), ...(state.planner.worker_state?.active_error?.diagnostics || [])];
  const grouped: Record<string, DiagnosticItem[]> = {};
  for (const item of diagnostics) {
    const path = String(item.path || latest?.path || '').trim();
    if (path) (grouped[path] ||= []).push(item);
  }
  return grouped;
}

export function currentReview(state: AgentBridgeState): LatestReview | null {
  return state.planner.worker_state?.latest_review || null;
}

export function primaryReviewPath(review?: LatestReview | null): string | undefined {
  if (review?.path) return review.path;
  for (const file of review?.files || []) {
    if (typeof file === 'string' && file.trim()) return file;
    if (typeof file === 'object' && file.path?.trim()) return file.path;
  }
  return undefined;
}

export function issueState(state: AgentBridgeState): { active?: IssueSummary; open: IssueSummary[]; reopenable: IssueSummary[]; totalFacts: number } {
  const worker = state.planner.worker_state;
  const source = worker?.issue_state || (state.planner.issue_state as WorkerStateLike | undefined) || {};
  const active = source.active_issue || worker?.issue_context?.active_durable_issue || undefined;
  const open = (source.open_issues || source.issues || [])
    .filter((item) => !item.status || item.status === 'open')
    .filter((item) => item.issue_id !== active?.issue_id);
  return { active: active || undefined, open, reopenable: source.reopenable_issues || [], totalFacts: Number(source.total_fact_count || 0) };
}

interface WorkerStateLike {
  active_issue?: IssueSummary | null;
  issues?: IssueSummary[];
  open_issues?: IssueSummary[];
  reopenable_issues?: IssueSummary[];
  total_fact_count?: number;
}

export function continuousIsActive(state: AgentBridgeState): boolean {
  const continuous = state.planner.continuous_mode;
  const status = String(continuous?.status || '');
  return Boolean(continuous?.enabled || (status && !['idle', 'stopped'].includes(status)));
}

export interface AgentHandoff {
  discovery: DiscoveryResult | null;
  nextSteps: string[];
  goalResults: GoalExecutionResult[];
  executionSummary: string;
  plan: PlannerPlan | null;
  executionState: 'executing' | 'paused' | 'complete' | 'idle';
  completedGoalCount: number;
  totalGoalCount: number;
  currentGoalTitle: string;
}

export function agentHandoff(state: AgentBridgeState): AgentHandoff {
  const planner = state.planner;
  const plan = planner.pending_plan || planner.paused_plan || planner.last_completed_plan || planner.last_presented_plan || null;
  const goalResults = planner.executing
    ? planner.completed_results || []
    : planner.execution_paused
      ? [...(planner.paused_completed_results || []), ...(planner.completed_results || []).filter((result) => result.status !== 'completed')]
      : planner.last_completed_results?.length
        ? planner.last_completed_results
        : planner.completed_results || [];
  const executionState = planner.executing ? 'executing'
    : planner.execution_paused ? 'paused'
      : planner.last_completed_plan || planner.last_execution_summary ? 'complete'
        : 'idle';
  const preview = (plan?.next_steps_preview || []).filter((step) => step.trim());
  const suggested = combineSuggestedActions(state)
    .filter((action) => !['reset_session', 'delete_session', 'create_issue'].includes(action.type))
    .map((action) => String(action.label || action.type).trim())
    .filter(Boolean);
  const nextSteps = executionState === 'executing'
    ? []
    : executionState === 'complete' || executionState === 'paused'
      ? (planner.last_next_steps || []).filter((step) => step.trim())
      : [...new Set(preview.length ? preview : suggested)].slice(0, 4);
  return {
    discovery: planner.last_discovery || null,
    nextSteps,
    goalResults,
    executionSummary: executionState === 'executing' ? '' : String(planner.last_execution_summary || '').trim(),
    plan,
    executionState,
    completedGoalCount: goalResults.filter((result) => result.status === 'completed').length,
    totalGoalCount: Number(planner.executing_goal_count || plan?.goals?.length || goalResults.length || 0),
    currentGoalTitle: String(planner.executing_goal_title || '').trim(),
  };
}

export function presentationTranscript(state: AgentBridgeState): AgentTranscriptEntry[] {
  const handoff = agentHandoff(state);
  let replacedExecutionReport = false;

  const entries = state.transcript.flatMap((entry): AgentTranscriptEntry[] => {
    if (entry.role !== 'assistant') return [entry];
    const content = String(entry.content || '').trim();
    if (isDiscoveryReport(content, handoff.discovery)) return [];
    if (isGoalReport(content, handoff)) {
      if (replacedExecutionReport || !handoff.executionSummary) return [];
      replacedExecutionReport = true;
      return [{ role: 'assistant', content: handoff.executionSummary }];
    }
    return [entry];
  });

  const hasSummary = entries.some((entry) => entry.role === 'assistant' && entry.content.trim() === handoff.executionSummary);
  if (handoff.executionSummary && handoff.goalResults.length && !hasSummary) {
    entries.push({ role: 'assistant', content: handoff.executionSummary });
  }
  return entries;
}

function isDiscoveryReport(content: string, discovery: DiscoveryResult | null): boolean {
  if (!/(?:^|\n\n)Discovery (?:Complete|Failed)\b/i.test(content)) return false;
  const finalMessage = String(discovery?.final_message || '').trim();
  return !finalMessage || content.includes(finalMessage);
}

function isGoalReport(content: string, handoff: AgentHandoff): boolean {
  if (!/(?:^|\n\n)Goal \d+\/\d+ (?:Completed|Failed)\n- Title:/i.test(content)) return false;
  if (!/\n- Worker result:/i.test(content)) return false;
  if (!handoff.goalResults.length) return true;
  return handoff.goalResults.some((result) => {
    const message = String(result.final_message || '').trim();
    return !message || content.includes(message);
  });
}

export function describeProgress(item: AgentProgressMessage): { title: string; detail: string; tone: 'good' | 'bad' | 'neutral' } {
  const action = String(item.action_type || item.type).replaceAll('_', ' ');
  const title = item.type === 'goal_start' ? `Started ${item.summary || item.thought || 'goal'}`
    : item.type === 'goal_finish' ? `Finished ${item.summary || item.thought || 'goal'}`
      : item.summary || item.thought || action;
  const detail = [item.path, item.command, item.elapsed_s !== undefined ? `${item.elapsed_s}s` : ''].filter(Boolean).join(' · ');
  return { title: String(title), detail, tone: item.ok === false ? 'bad' : item.ok === true ? 'good' : 'neutral' };
}
