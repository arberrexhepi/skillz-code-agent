import type { DiscoveryExtensionRequest } from '../../src/shared/agentTypes';
export const extension: DiscoveryExtensionRequest = {
  request_id: 'discovery-example', mode: 'quick', additional_turns: 2, additional_tool_calls: 2,
  turns_used: 8, turns_max: 8, tool_calls_used: 6, tool_calls_max: 6,
  reason: 'Two route implementations disagree about the API contract.',
  proposal: 'Read the route caller and its test to identify the active contract.',
  ambiguities: ['Does the active route use the legacy or new contract?', 'Which caller must preserve compatibility?'],
  findings: 'The entry point is routes.ts. Both contracts have callers.\nAccented paths such as kërkesë.ts remain relevant.',
};
