import type { AgentBridgeState, PlannerPlan } from '../../src/shared/agentTypes';

export const reviewPlan: PlannerPlan = {
  original_request: 'Make the shared Pro experience prerequisite-aware.',
  summary: 'Make the shared Pro experience prerequisite-aware: Design and Web controls should communicate when no editable target exists, render a calm non-editing state instead of an active editor shell, and become fully available once the user selects a supported target. Preserve existing content throughout these state transitions, including unsaved work and selected tools.',
  clarification_summary: 'Cover both Design and Web views; retain existing editing behavior for valid targets.',
  assumptions: ['Target selection remains the source of truth.', 'Unsaved drafts must survive prerequisite changes.'],
  goals: [
    ['Model Pro capability state', 'Represent missing, loading, ready, and unsupported targets explicitly.'],
    ['Refine domain empty states', 'Explain the next available action without presenting disabled editor chrome as usable.'],
    ['Verify state transitions', 'Test selecting, switching, and removing a target without losing edits.'],
    ['Check keyboard accessibility', 'Keep focus predictable and announce target availability to assistive technology.'],
    ['Validate regression coverage', 'Run the complete suite and verify the final prerequisite transition at desktop and narrow widths.'],
  ].map(([title, goal], index) => ({ goal_id: `g${index + 1}`, title, goal, reason: 'Make prerequisite behavior consistent and understandable.', depends_on: index ? [`g${index}`] : [], estimated_scope: 'Focused frontend change', success_signals: [`Goal ${index + 1} has regression coverage`, 'No unsaved content is discarded'], delegation_notes: ['Reuse the existing capability model.'], preserve_context: true, parallelizable: false, relevant_fact_keys: ['pro.capabilities'] })),
  not_in_scope: ['No new billing or permissions model.'],
  next_steps_preview: ['Review the resulting states with real project content.'],
  dependency_repairs: ['Accessibility validation follows state transition work.'],
  dependency_errors: [],
  confirmation_prompt: 'Approve this plan, reject it, or suggest changes before implementation.',
};
export const reviewFixture: AgentBridgeState = { planner: { pending_plan: reviewPlan }, transcript: [{ role: 'assistant', content: 'The plan is ready for your review.' }] };
