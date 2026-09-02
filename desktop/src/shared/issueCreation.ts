import type { AgentResponse, WorkbenchApi } from './contracts';

/** Add to the issue backlog; never redirect the planner's active context. */
export async function createInactiveIssue(
  api: Pick<WorkbenchApi['agent'], 'plannerAction'>,
  details: string,
): Promise<AgentResponse> {
  const summary = details.trim();
  if (!summary) throw new Error('Enter issue details before adding an issue.');
  const response = await api.plannerAction('create_issue', { summary, activate: false });
  if (!response.ok) throw new Error(response.message || 'Issue creation failed. Your details have been kept.');
  return response;
}
