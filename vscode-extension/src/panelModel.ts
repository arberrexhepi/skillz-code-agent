export type JsonMap = Record<string, unknown>;

export interface PlannerSuggestedAction extends JsonMap {
  type: string;
  label?: string;
  style?: string;
  mode?: string;
}

export interface WorkerSuggestedAction extends JsonMap {
  type: string;
  label?: string;
  style?: string;
  requires_confirmation?: boolean;
}

export interface DiagnosticItem extends JsonMap {
  path?: string;
  line?: number;
  column?: number;
  code?: string;
  message?: string;
}

export interface LatestDiagnostics extends JsonMap {
  path?: string;
  message?: string;
  diagnostic_engine?: string;
  diagnostics?: DiagnosticItem[];
  step?: number;
  source?: string;
}

export interface ReviewFile extends JsonMap {
  path?: string;
  risk?: string;
  validation?: string;
  added?: number;
  deleted?: number;
}

export interface LatestReview extends JsonMap {
  action_type?: string;
  summary?: string;
  step?: number;
  path?: string;
  diff?: string;
  stat?: string;
  files?: Array<string | ReviewFile>;
  staged?: boolean;
  counts?: JsonMap;
  high_risk_paths?: string[];
  review_summary?: JsonMap;
}

export interface WorkerState extends JsonMap {
  issue_state?: JsonMap;
  latest_diagnostics?: LatestDiagnostics | null;
  latest_review?: LatestReview | null;
  suggested_next_actions?: WorkerSuggestedAction[];
  protected_paths?: string[];
}

export interface ContinuousModeState extends JsonMap {
  enabled?: boolean;
  status?: string;
  cycle?: number;
  max_cycles?: number;
  active_issue_id?: string;
  selected_discovery_mode?: string;
  latest_review_decision?: string;
  stop_reason?: string;
  created_followup_issue_ids?: string[];
  created_followup_issues?: JsonMap[];
}

export interface PlannerState extends JsonMap {
  issue_state?: JsonMap;
  continuous_mode?: ContinuousModeState;
  suggested_next_actions?: PlannerSuggestedAction[];
  worker_state?: WorkerState | null;
}

export interface BridgeState {
  planner: PlannerState;
  transcript: Array<{ role: string; content: string }>;
  last_message?: string;
}

export interface CombinedSuggestedAction extends JsonMap {
  type: string;
  label?: string;
  style?: string;
  mode?: string;
  issue_id?: string;
  max_cycles?: number;
  requires_confirmation?: boolean;
  source: 'planner' | 'worker';
}

export function isContinuousModeActive(planner?: PlannerState): boolean {
  const status = String(planner?.continuous_mode?.status || '').trim();
  return Boolean(planner?.continuous_mode?.enabled || (status && !['idle', 'stopped'].includes(status)));
}

export function continuousModeOwnsLifecycle(planner?: PlannerState): boolean {
  const status = String(planner?.continuous_mode?.status || '').trim();
  return [
    'selecting_issue',
    'discovering',
    'planning',
    'approving',
    'executing',
    'reviewing',
    'closing_issue',
    'creating_followups',
  ].includes(status);
}

export function combineSuggestedActions(state: BridgeState): CombinedSuggestedAction[] {
  const plannerActions = state.planner?.suggested_next_actions || [];
  const workerActions = state.planner?.worker_state?.suggested_next_actions || [];
  return [
    ...plannerActions.map((action) => ({ ...action, source: 'planner' as const })),
    ...workerActions.map((action) => ({ ...action, source: 'worker' as const })),
  ];
}

export function groupLatestDiagnosticsByPath(state: BridgeState): Record<string, DiagnosticItem[]> {
  const latestDiagnostics = state.planner?.worker_state?.latest_diagnostics;
  const diagnostics = latestDiagnostics?.diagnostics || [];
  const grouped: Record<string, DiagnosticItem[]> = {};
  for (const item of diagnostics) {
    const relativePath = String(item.path || latestDiagnostics?.path || '').trim();
    if (!relativePath) {
      continue;
    }
    if (!grouped[relativePath]) {
      grouped[relativePath] = [];
    }
    grouped[relativePath].push(item);
  }
  return grouped;
}

export function buildReviewReport(review?: LatestReview | null): { title: string; language: string; content: string } | null {
  if (!review || !review.action_type) {
    return null;
  }
  if (review.action_type === 'review_changes') {
    return {
      title: 'Python Agent review_changes',
      language: 'json',
      content: JSON.stringify(review, null, 2),
    };
  }

  const content = [String(review.stat || ''), String(review.diff || '')].filter(Boolean).join('\n\n').trim();
  if (!content) {
    return null;
  }
  return {
    title: `Python Agent ${review.action_type}`,
    language: 'diff',
    content,
  };
}

export function primaryPathForReview(review?: LatestReview | null): string | undefined {
  if (!review) {
    return undefined;
  }
  if (review.path) {
    return review.path;
  }
  const files = Array.isArray(review.files) ? review.files : [];
  for (const item of files) {
    if (typeof item === 'string' && item.trim()) {
      return item;
    }
    if (typeof item === 'object' && item && typeof item.path === 'string' && item.path.trim()) {
      return item.path;
    }
  }
  return undefined;
}

export function progressTimelineTarget(
  planner: PlannerState | undefined,
  pendingActionType: string,
): 'plan' | 'discovery' | undefined {
  if (planner?.executing) {
    return 'plan';
  }
  if (planner?.pending_discovery || pendingActionType === 'select_discovery_mode') {
    return 'discovery';
  }
  return undefined;
}

export function buildPlannerActionMessage(action: CombinedSuggestedAction): { action: string; mode?: string; payload?: JsonMap } {
  const payload: JsonMap = typeof action.payload === 'object' && action.payload !== null && !Array.isArray(action.payload)
    ? { ...(action.payload as JsonMap) }
    : {};
  if (typeof action.mode === 'string' && action.mode.trim()) {
    payload.mode = action.mode.trim();
  }
  if (typeof action.issue_id === 'string' && action.issue_id.trim()) {
    payload.issue_id = action.issue_id.trim();
  }
  if (typeof action.max_cycles === 'number' && Number.isFinite(action.max_cycles)) {
    payload.max_cycles = Math.max(1, Math.floor(action.max_cycles));
  }
  return {
    action: action.type,
    mode: typeof action.mode === 'string' ? action.mode : undefined,
    payload: Object.keys(payload).length ? payload : undefined,
  };
}
