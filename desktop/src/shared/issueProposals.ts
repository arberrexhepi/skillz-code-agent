import type { AgentResponse } from './contracts';
import type { JsonMap } from './agentTypes';

export async function decideIssueProposal(api: { plannerAction: (action: string, extras: JsonMap) => Promise<AgentResponse> }, proposalId: string, decision: 'accept' | 'ignore'): Promise<AgentResponse> {
  if (!proposalId.trim()) throw new Error('Missing suggestion id.');
  const response = await api.plannerAction(`${decision}_issue_proposal`, { proposal_id: proposalId });
  if (!response.ok) throw new Error(response.message || 'Could not update this suggestion.');
  return response;
}
