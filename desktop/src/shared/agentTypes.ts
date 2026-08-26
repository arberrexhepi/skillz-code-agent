export type JsonMap = Record<string, unknown>;

export interface AgentTranscriptEntry {
  role: string;
  content: string;
}

export interface SuggestedAction extends JsonMap {
  type: string;
  label?: string;
  style?: string;
  mode?: string;
  issue_id?: string;
  max_cycles?: number;
  requires_confirmation?: boolean;
  confirmation_prompt?: string;
  source?: 'planner' | 'worker';
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

export interface IssueSummary extends JsonMap {
  issue_id?: string;
  plan_summary?: string;
  request_summary?: string;
  status?: string;
  fact_count?: number;
  goal_fact_count?: number;
  architecture_fact_count?: number;
}

export interface WorkerState extends JsonMap {
  issue_context?: {
    active_durable_issue?: IssueSummary | null;
    focused_run_diagnostic_id?: string;
    run_diagnostics?: Array<{ issue_id?: string; namespace?: string; status?: string; summary?: string; file?: string; line?: string; code?: string }>;
  };
  llm_activity?: { in_flight?: boolean; turn?: number; last_event?: string; elapsed_s?: number; output_chars?: number; error?: string; status_code?: number | null; request_id?: string };
  issue_state?: { active_issue_id?: string; active_issue?: IssueSummary | null; issues?: IssueSummary[]; open_issues?: IssueSummary[]; reopenable_issues?: IssueSummary[]; total_fact_count?: number };
  runtime_config?: { provider?: string; model?: string; thinking_mode?: string; verbosity?: string };
  active_error?: { message?: string; path?: string; error_type?: string; diagnostic_engine?: string; diagnostics?: DiagnosticItem[]; suggested_next_actions?: SuggestedAction[] } | null;
  pending_verification?: { path?: string; mode?: string } | null;
  edit_batch?: { active?: boolean; pending_paths?: string[]; pending_count?: number; started_thought?: string };
  current_run_facts?: Array<{ key?: string; value?: string }>;
  available_skills?: Array<{ name?: string; description?: string; tags?: string[]; category?: string; priority?: number; modes?: string[] }>;
  usage_accounting?: JsonMap;
  last_run_result?: { final_message?: string; validation_passed?: boolean; validation_ran?: boolean } | null;
  latest_diagnostics?: LatestDiagnostics | null;
  latest_review?: LatestReview | null;
  suggested_next_actions?: SuggestedAction[];
  protected_paths?: string[];
  backoff?: AgentBackoff;
}

export interface PlannerGoal extends JsonMap {
  goal_id?: string;
  title?: string;
  goal?: string;
  reason?: string;
  depends_on?: string[];
  estimated_scope?: string;
  success_signals?: string[];
}

export interface PlannerPlan extends JsonMap {
  original_request?: string;
  summary?: string;
  assumptions?: string[];
  clarification_summary?: string;
  goals?: PlannerGoal[];
  not_in_scope?: string[];
  next_steps_preview?: string[];
  confirmation_prompt?: string;
  dependency_repairs?: string[];
  dependency_errors?: string[];
}

export interface DiscoveryResult extends JsonMap {
  mode?: string;
  reason?: string;
  prompt?: string;
  delegated_task?: string;
  final_message?: string;
  ok?: boolean;
  task_satisfied?: boolean;
  validation_ran?: boolean;
  validation_passed?: boolean;
  validation_summary?: string;
  detailed_findings?: string[];
  worker_history_summary?: JsonMap[];
  touched_paths?: string[];
  duration_s?: number;
  tool_calls_used?: number;
  tool_calls_max?: number;
  usage_summary?: string;
}

export interface GoalExecutionResult extends JsonMap {
  goal_id?: string;
  title?: string;
  final_message?: string;
  task_satisfied?: boolean;
  validation_ran?: boolean;
  validation_passed?: boolean;
  validation_summary?: string;
  worker_history_summary?: JsonMap[];
  touched_paths?: string[];
  preserve_context_used?: boolean;
  duration_s?: number;
  commentary_for_next_goal?: string;
  status?: string;
  usage_summary?: string;
  failure_retryable?: boolean;
  failure_status_code?: number | null;
  failure_request_id?: string;
}

export interface ContinuousModeState extends JsonMap {
  enabled?: boolean;
  status?: string;
  cycle?: number;
  max_cycles?: number;
  active_issue_id?: string;
  run_prompt?: string;
  selected_discovery_mode?: string;
  latest_review_decision?: string;
  stop_reason?: string;
  created_followup_issue_ids?: string[];
  created_followup_issues?: JsonMap[];
  completed_issue_ids?: string[];
  completed_issues?: JsonMap[];
}

export interface PlannerState extends JsonMap {
  runtime_config?: { provider?: string; model?: string; thinking_mode?: string; verbosity?: string };
  status?: string;
  latest_request?: string;
  discovery_phase?: string;
  pending_discovery?: { reason?: string; prompt?: string; recommended_mode?: string } | null;
  last_discovery?: DiscoveryResult | null;
  pending_plan?: PlannerPlan | null;
  last_presented_plan?: PlannerPlan | null;
  last_completed_plan?: PlannerPlan | null;
  last_completed_results?: GoalExecutionResult[];
  completed_results?: GoalExecutionResult[];
  pending_checkpoint_results?: GoalExecutionResult[];
  last_execution_summary?: string;
  execution_paused?: boolean;
  paused_plan?: PlannerPlan | null;
  paused_completed_results?: GoalExecutionResult[];
  resume_checkpoint?: { mode?: string; total_goal_count?: number; completed_goal_count?: number; completed_goal_ids?: string[]; next_goal_index?: number; next_goal?: PlannerGoal | null; remaining_goals?: Array<{ goal_id?: string; title?: string; index?: number }> } | null;
  executing?: boolean;
  executing_goal_index?: number;
  executing_goal_id?: string;
  executing_goal_title?: string;
  executing_goal_count?: number;
  active_issue_id?: string;
  issue_state?: JsonMap;
  continuous_mode?: ContinuousModeState;
  planner_usage_summary?: string;
  repo_facts_status_lines?: string[];
  suggested_next_actions?: SuggestedAction[];
  worker_state?: WorkerState | null;
}

export interface AgentBridgeState {
  planner: PlannerState;
  transcript: AgentTranscriptEntry[];
  last_message?: string;
  bridge_warning?: string;
}

export interface AgentBackoff {
  enabled: boolean;
  token_limit_k: number;
  window_tokens_used: number;
}

export interface RuntimeProviderOption extends JsonMap {
  key?: string;
  label?: string;
  default_model?: string;
  selection_model?: string;
  suggested_models?: string[];
  notes?: string;
  active?: boolean;
  active_model?: string;
  accepts_custom_model?: boolean;
  hidden?: boolean;
}

export interface RuntimeOptionsPayload extends JsonMap {
  providers?: RuntimeProviderOption[];
  provider_keys?: string[];
  current_provider?: string;
  current_model?: string;
}

export interface AgentProgressMessage extends JsonMap {
  type: 'progress' | 'goal_start' | 'goal_finish';
  domain?: string;
  step?: number;
  action_type?: string;
  skill_name?: string;
  skill_mode?: string;
  path?: string;
  command?: string;
  issue_id?: string;
  ok?: boolean;
  elapsed_s?: number;
  thought?: string;
  summary?: string;
  diff?: string;
  added_lines?: number;
  removed_lines?: number;
  state?: AgentBridgeState;
}
