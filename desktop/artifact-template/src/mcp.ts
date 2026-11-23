export interface ArtifactMcpAction {
  operation: 'servers' | 'list' | 'search' | 'describe' | 'call' | 'read-resource' | 'get-prompt';
  server?: string;
  kind?: 'tools' | 'resources' | 'templates' | 'prompts';
  query?: string;
  offset?: number;
  name?: string;
  uri?: string;
  arguments?: Record<string, unknown>;
  approveMutations?: boolean;
}

export async function artifactMcp(action: ArtifactMcpAction): Promise<Record<string, unknown>> {
  const response = await fetch('/_skillz/mcp/request', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(action),
  });
  const result = await response.json();
  if (!response.ok || result.ok !== true) throw new Error(result.error || `Artifact MCP returned HTTP ${response.status}.`);
  return result;
}
