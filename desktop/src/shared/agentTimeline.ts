import type { AgentBridgeState, AgentProgressMessage, AgentTranscriptEntry, TranscriptPart, WorkflowPart } from './agentTypes';

export type ConversationItem = { kind: 'message'; id: string; entry: AgentTranscriptEntry };
export interface WorkflowItem extends Omit<WorkflowPart, 'content'> {
  id: string;
  events: Array<WorkflowPart & { role: string }>;
  currentGoal?: string;
}
export type TimelineItem = ConversationItem | WorkflowItem;

export function conversationTimeline(state: AgentBridgeState): TimelineItem[] {
  const items: TimelineItem[] = [];
  state.transcript.forEach((entry, index) => {
    const previous = items.at(-1);
    const parts = entry.presentation?.length ? entry.presentation : legacyParts(entry, previous, state);
    parts.forEach((part, partIndex) => {
      const id = `${entry.id || index}:${partIndex}`;
      if (part.kind === 'message') {
        if (part.content.trim()) items.push({ kind: 'message', id, entry: { ...entry, content: part.content } });
        return;
      }
      const prior = items.at(-1);
      const event = { ...part, role: entry.role };
      if (prior?.kind === 'workflow' && prior.category === part.category && part.status !== 'offered' && part.status !== 'requested') {
        prior.events.push(event);
        prior.selection = part.selection || prior.selection;
        prior.summary = part.summary || prior.summary;
        prior.plan = part.plan?.summary ? part.plan : prior.plan;
        prior.discovery = part.discovery || prior.discovery;
        prior.goals = [...(prior.goals || []), ...(part.goals || [])];
        prior.status = prior.status === 'failed' ? 'failed' : part.status;
        if (part.category === 'issue') prior.title = part.title;
      } else {
        items.push({ ...part, id, events: [event], goals: part.goals ? [...part.goals] : undefined });
      }
    });
  });
  if (state.planner.executing) {
    const activePlan = state.planner.pending_plan || state.planner.last_presented_plan;
    const current = [...items].reverse().find((item) => item.kind === 'workflow' && item.category === 'plan'
      && (activePlan?.summary ? item.plan?.summary === activePlan.summary : item.status === 'selected' && item.selection === 'Approved'));
    if (current?.kind === 'workflow') {
      current.status = 'running';
      current.goals = state.planner.completed_results || [];
      current.currentGoal = state.planner.executing_goal_title;
    }
  }
  return items;
}

// Conservative compatibility for bridges without explicit report boundaries.
// Unknown text stays conversational; recognized legacy reports remain readable
// in full inside a card. Never hide arbitrary short user messages.
function legacyParts(entry: AgentTranscriptEntry, previous: TimelineItem | undefined, state: AgentBridgeState): TranscriptPart[] {
  const content = entry.content.trim();
  const workflow = (category: WorkflowPart['category'], status: string, title: string, extras: Partial<WorkflowPart> = {}): WorkflowPart => ({ kind: 'workflow', category, status, title, content, ...extras });
  if (entry.role === 'user') {
    if (previous?.kind === 'workflow' && previous.status === 'offered') {
      const choice = content.toLowerCase();
      if (previous.category === 'discovery') {
        const modes: Record<string, string> = { '1': 'Quick', quick: 'Quick', 'quick scan': 'Quick', '2': 'Moderate', moderate: 'Moderate', 'moderate scan': 'Moderate', '3': 'Deep', deep: 'Deep', 'deep scan': 'Deep', no: 'Skipped', reject: 'Skipped' };
        if (modes[choice]) return [workflow('discovery', 'selected', 'Discovery', { selection: modes[choice] })];
      }
      if (previous.category === 'plan' && /^(?:approve|approved|yes|y|\/approve|\/run|reject|rejected|no|n|\/reject|\/cancel)$/.test(choice)) {
        return [workflow('plan', 'selected', 'Goal plan', { selection: /^(?:reject|rejected|no|n|\/reject|\/cancel)$/.test(choice) ? 'Rejected' : 'Approved' })];
      }
    }
    if (/^\/?(?:create-issue\s+.+|(?:close-issue|reopen|close)\s+issue-[\w-]+)$/i.test(content)) return [workflow('issue', 'requested', 'Issue action')];
    return [{ kind: 'message', content }];
  }
  if (entry.role !== 'assistant') return [{ kind: 'message', content }];
  if (/^Discovery Suggested\n- Reason:/.test(content) && content.includes('\nChoose a Discovery Depth\n') && content.includes('\nResponse Options\n')) {
    return [workflow('discovery', 'offered', 'Discovery', { summary: content.match(/^- Reason: (.+)$/m)?.[1] })];
  }
  if (/^Discovery (?:Complete|Failed)\n- Mode:/.test(content) && content.includes('\n- Worker result:')) {
    const planStart = content.indexOf('\n\nPlan Summary\n');
    const report = workflow('discovery', content.startsWith('Discovery Failed') ? 'failed' : 'complete', 'Discovery');
    if (planStart >= 0) return [{ ...report, content: content.slice(0, planStart) }, ...legacyParts({ ...entry, content: content.slice(planStart + 2) }, undefined, state)];
    return [report];
  }
  if (/^Plan Summary\n- /.test(content) && content.includes('\nGoals\n') && content.includes('\nApproval\n')) {
    return [workflow('plan', 'offered', 'Goal plan', { summary: content.split('\n')[1].replace(/^- /, '') })];
  }
  if (/^Goal \d+\/\d+ (?:Completed|Failed)\n- Title:/.test(content) && content.includes('\n- Worker result:')) {
    const summary = state.planner.last_execution_summary?.trim();
    const hasSummary = summary && content.endsWith(`\n\n${summary}`);
    const report = workflow('plan', /^Goal \d+\/\d+ Failed\n/m.test(content) ? 'failed' : 'complete', 'Goal plan');
    return hasSummary ? [{ ...report, content: content.slice(0, -summary.length).trim() }, { kind: 'message', content: summary }] : [report];
  }
  if (/^(?:Created|Closed|Reopened|Activated) issue\s+issue-[\w-]+/.test(content)) return [workflow('issue', 'complete', content.split('.')[0])];
  return [{ kind: 'message', content }];
}

export function latestTurnThought(activity: AgentProgressMessage[]): AgentProgressMessage | undefined {
  for (let index = activity.length - 1; index >= 0; index--) {
    const item = activity[index];
    // A new goal/discovery must not show a thought from the preceding run.
    if (item.type === 'goal_start' || ['discovery_start', 'discovery_mode_selected', 'plan_execution_start'].includes(item.action_type || '')) return undefined;
    if (item.thought?.trim() && (item.action_type === 'turn_thought' || ['worker', 'discovery'].includes(item.domain || ''))
      && !['discovery_finish', 'discovery_start', 'discovery_mode_selected'].includes(item.action_type || '')) return item;
  }
  return undefined;
}
