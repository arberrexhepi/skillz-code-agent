import type { AgentBridgeState, AgentProgressMessage, AgentTranscriptEntry, TranscriptPart, WorkflowPart } from '../../src/shared/agentTypes';

const report = (category: WorkflowPart['category'], status: string, extras: Partial<WorkflowPart> = {}): WorkflowPart => ({
  kind: 'workflow', category, status, title: category === 'plan' ? 'Goal plan' : 'Discovery', content: `${category} ${status}`, ...extras,
});
const entry = (role: string, ...presentation: TranscriptPart[]): AgentTranscriptEntry => ({ role, content: presentation.map((part) => part.content).join('\n\n'), presentation });
const plan = { summary: 'Show ten projects with a Show more control', goals: [{ goal_id: 'g1', title: 'Fix project loading' }, { goal_id: 'g2', title: 'Add Show more' }] };

export const timelineFixture: AgentBridgeState = {
  planner: { executing: true, executing_goal_title: 'Add Show more', executing_goal_count: 2, executing_goal_index: 2, last_presented_plan: plan, completed_results: [{ goal_id: 'g1', title: 'Fix project loading', status: 'completed', final_message: 'Removed the two-project limit.' }] },
  transcript: [
    { role: 'user', content: 'Show ten projects initially, then a Show more + button.' },
    entry('assistant', report('discovery', 'offered', { summary: 'Trace project loading and sidebar rendering.', content: 'Discovery Suggested\n- Reason: Trace project loading\n\nChoose a Discovery Depth\n1. Quick\n2. Moderate\n3. Deep\n\nResponse Options\nChoose a depth.' })),
    entry('user', report('discovery', 'selected', { selection: 'Moderate', content: 'moderate' })),
    entry('assistant', report('discovery', 'complete', { discovery: { mode: 'moderate', final_message: 'The sidebar limits the fetched projects before rendering. We need to keep the full list and paginate its visible slice.', touched_paths: ['src/Sidebar.tsx'] } }), { kind: 'message', content: 'The missing project is caused by the query limit, not the database record. I’ll fix loading and add the expansion control.' }, report('plan', 'offered', { plan })),
    entry('user', report('plan', 'selected', { selection: 'Approved', content: 'approve' })),
  ],
};

export const thoughtFixture: AgentProgressMessage = {
  type: 'progress', domain: 'worker', action_type: 'turn_thought', turn: 4,
  thought: 'Project loading now returns the full list. I’m checking the sidebar’s expansion state so the first ten stay visible and Show more reveals the next batch without resetting the selected project. Then I’ll verify the desktop and mobile layouts.',
};
