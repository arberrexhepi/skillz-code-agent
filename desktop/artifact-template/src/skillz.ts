export interface SkillzMessage { role: 'user' | 'assistant'; content: string }
export interface SkillzChatOptions { history?: SkillzMessage[]; approveMutations?: boolean }
export async function askSkillz(message: string, options: SkillzChatOptions = {}): Promise<{ message: string }> {
  const response = await fetch('/_skillz/agent/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message, history: options.history || [], approveMutations: options.approveMutations === true }) });
  const value = await response.json(); if (!response.ok) throw new Error(value.error || `Skillz chatbot failed with HTTP ${response.status}.`); return value;
}
export async function artifactCommands(): Promise<{ commands: Array<{ name: string; kind: 'discovery' | 'mutation'; description: string; inputSchema: Record<string, unknown> }> }> {
  const response = await fetch('/_skillz/commands'); if (!response.ok) throw new Error('Could not load artifact commands.'); return response.json();
}
