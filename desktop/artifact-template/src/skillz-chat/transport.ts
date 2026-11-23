import type { SkillzChatOptions } from '../skillz';

export interface ChatElicitation { question: string; choices: string[] }
interface Reply { message: string; elicitation?: ChatElicitation; broadcasts?: string[] }

export async function requestChat(
  message: string,
  options: SkillzChatOptions,
  onBroadcast: (content: string) => void,
): Promise<Reply> {
  const response = await fetch('/_skillz/agent/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/x-ndjson, application/json' },
    body: JSON.stringify({ message, history: options.history, approveMutations: options.approveMutations === true }),
  });
  if (!response.ok || !response.headers.get('content-type')?.includes('application/x-ndjson')) {
    const reply = await response.json().catch(() => ({}));
    if (!response.ok || typeof reply.message !== 'string') throw new Error(reply.error || `Skillz chatbot returned HTTP ${response.status}.`);
    if (Array.isArray(reply.broadcasts)) for (const content of reply.broadcasts) if (typeof content === 'string') onBroadcast(content);
    return reply;
  }
  const reader = response.body?.getReader();
  if (!reader) throw new Error('The chatbot response was empty.');
  const decoder = new TextDecoder();
  let buffer = '', result: Reply | undefined;
  function receive(line: string) {
    if (!line.trim()) return;
    const frame = JSON.parse(line);
    if (typeof frame.error === 'string') throw new Error(frame.error);
    if (frame.type === 'broadcast' && typeof frame.content === 'string') onBroadcast(frame.content);
    if (frame.type === 'result' && typeof frame.result?.message === 'string') result = frame.result;
  }
  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      let newline: number;
      while ((newline = buffer.indexOf('\n')) !== -1) {
        receive(buffer.slice(0, newline));
        buffer = buffer.slice(newline + 1);
      }
      if (done) break;
      if (buffer.length > 2_000_000) throw new Error('The chatbot response exceeded 2 MB.');
    }
    receive(buffer);
  } finally { await reader.cancel().catch(() => {}); }
  if (!result) throw new Error('The chatbot stopped before sending a reply.');
  return result;
}
