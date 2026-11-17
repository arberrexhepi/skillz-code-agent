export type NodeId = 'intake' | 'planner' | 'discovery' | 'context' | 'model' | 'grammar' | 'guards' | 'tools' | 'evidence' | 'validation' | 'recovery' | 'memory' | 'completion';
export interface RunEvent { seq: number; at: number; type: string; data: Record<string, unknown> }
export interface Plan { summary?: string; goals?: { goal_id?: string; title?: string; goal?: string; success_signals?: string[] }[] }
export interface RunState { planner: { status?: string; pending_plan?: Plan; paused_plan?: Plan; last_presented_plan?: Plan; pending_discovery?: { reason?: string }; pending_discovery_extension?: { reason?: string; findings?: string; additional_turns?: number }; suggested_next_actions?: { type: string; label?: string; confirmation_prompt?: string; requires_confirmation?: boolean }[] }; transcript: { role: string; content: string }[] }
export interface RunRepository { id: string; label: string; access: 'read' | 'write' }
export interface Run {
  id: string; prompt: string; startedAt: string; endedAt?: string; status: 'running' | 'awaiting-input' | 'completed' | 'failed' | 'interrupted';
  repository?: RunRepository; selection: { provider: string; model: string; backendScript?: string }; events: RunEvent[]; eventCount?: number; state?: RunState; message?: string; sample?: boolean;
}
export type RunSummary = Omit<Run, 'events' | 'state'> & { eventCount: number };
export interface RuntimeStatus { repositories?: RunRepository[]; available: boolean; ready?: boolean; setupAvailable?: boolean; setupMessage?: string; selection?: Run['selection']; busy?: boolean; activeRunId?: string; persistenceError?: string; error?: string; runs: RunSummary[] }
export interface EventMapping { primary: NodeId; related: NodeId[]; inferred: NodeId[]; label: string; failed: boolean }
