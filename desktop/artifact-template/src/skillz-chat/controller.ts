import type { SkillzMessage } from '../skillz';
import { requestChat, type ChatElicitation } from './transport';

export type ChatMessage = { type: 'message'; role: SkillzMessage['role']; content: string };
export type ChatBroadcast = { type: 'broadcast'; content: string };
type ChatQuestion = { type: 'elicitation' } & ChatElicitation;
export type ChatEntry = (ChatMessage | ChatBroadcast | ChatQuestion) & { id: number; routed?: boolean };
export interface ChatSendOptions { approveMutations?: boolean }
export interface ChatTargets {
  message?: (entry: (ChatMessage | ChatBroadcast) & { id: number }) => void;
  elicitation?: (entry: ChatQuestion & { id: number }, respond: (answer: string) => Promise<boolean>) => void;
}

export function createSkillzChat() {
  let snapshot: { entries: ChatEntry[]; pending: boolean; error: string; questionId?: number } = {
    entries: [], pending: false, error: '',
  };
  let id = 0;
  let targets: ChatTargets | undefined;
  const subscribers = new Set<() => void>();

  function update(change: Partial<typeof snapshot>) {
    snapshot = { ...snapshot, ...change };
    for (const notify of subscribers) notify();
  }

  function add(entry: ChatMessage | ChatBroadcast | ChatQuestion) {
    const item = { ...entry, id: ++id };
    const custom = item.type === 'elicitation' ? targets?.elicitation :
      item.type === 'broadcast' || item.role === 'assistant' ? targets?.message : undefined;
    update({
      entries: [...snapshot.entries, { ...item, routed: Boolean(custom) }],
      ...(item.type === 'elicitation' ? { questionId: item.id } : {}),
    });
    try {
      if (item.type === 'elicitation') targets?.elicitation?.(item, answer => respond(item.id, answer));
      else if (item.type === 'broadcast' || item.role === 'assistant') targets?.message?.(item);
    } catch {
      update({ entries: snapshot.entries.map(entry => entry.id === item.id ? { ...entry, routed: false } : entry) });
    }
  }

  function broadcast(content: string) {
    if (content.trim()) add({ type: 'broadcast', content });
  }

  async function send(raw: string, options: ChatSendOptions = {}): Promise<boolean> {
    const content = raw.trim();
    if (!content || snapshot.pending) return false;
    const history: SkillzMessage[] = snapshot.entries.flatMap<SkillzMessage>(entry => {
      if (entry.type === 'message') return [{ role: entry.role, content: entry.content }];
      if (entry.type === 'elicitation') return [{ role: 'assistant', content: entry.question }];
      return [];
    }).slice(-20);
    update({ pending: true, error: '', questionId: undefined });
    add({ type: 'message', role: 'user', content });
    let reply: Awaited<ReturnType<typeof requestChat>>;
    try {
      reply = await requestChat(content, { ...options, history }, broadcast);
    } catch (cause) {
      update({ pending: false, error: cause instanceof Error ? cause.message : String(cause) });
      return false;
    }
    update({ pending: false });
    if (reply.elicitation && typeof reply.elicitation.question === 'string') {
      const choices = Array.isArray(reply.elicitation.choices) ? reply.elicitation.choices.filter(choice => typeof choice === 'string') : [];
      add({ type: 'elicitation', question: reply.elicitation.question, choices });
    } else if (reply.message) {
      add({ type: 'message', role: 'assistant', content: reply.message });
    }
    return true;
  }

  function respond(questionId: number, answer: string): Promise<boolean> {
    return questionId === snapshot.questionId ? send(answer) : Promise.resolve(false);
  }

  return {
    send, respond, broadcast,
    getSnapshot: () => snapshot,
    subscribe(listener: () => void) {
      subscribers.add(listener);
      return () => { subscribers.delete(listener); };
    },
    registerTargets(handlers: ChatTargets) {
      targets = handlers;
      return () => {
        if (targets !== handlers) return;
        targets = undefined;
        update({ entries: snapshot.entries.map(entry => entry.routed ? { ...entry, routed: false } : entry) });
      };
    },
  };
}

export type SkillzChatController = ReturnType<typeof createSkillzChat>;
export const skillzChat = createSkillzChat();
