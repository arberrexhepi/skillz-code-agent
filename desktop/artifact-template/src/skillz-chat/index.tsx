import { useEffect, useId, useRef, useState, useSyncExternalStore, type FormEvent } from 'react';
import appConfig from '../../.artifact/app.json';
import { skillzChat, type SkillzChatController } from './controller';
import './chat.css';

export { createSkillzChat, skillzChat } from './controller';
export type { ChatEntry, ChatTargets, SkillzChatController } from './controller';
export type SkillzChatLayout = 'floating' | 'sidebar' | 'workspace';

type ChatbotConfig = { enabled: boolean; includeShell?: boolean; shellLayout?: SkillzChatLayout };
const { chatbot = { enabled: false } } = appConfig as { chatbot?: ChatbotConfig };

export function SkillzChat({
  layout = chatbot.shellLayout || 'floating', controller = skillzChat, title = 'Skillz', className = '',
}: { layout?: SkillzChatLayout; controller?: SkillzChatController; title?: string; className?: string }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const snapshot = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot);
  const routedQuestion = snapshot.entries.some(entry => entry.id === snapshot.questionId && entry.routed);
  useEffect(() => {
    if (layout === 'floating' && routedQuestion) setOpen(false);
  }, [layout, routedQuestion]);
  if (!chatbot.enabled || chatbot.includeShell === false) return null;
  if (layout !== 'floating') return <ChatSurface {...{ id, layout, controller, title, className, snapshot }} />;
  return <div className={`skillz-chat-floater ${className}`}>
    {open && <ChatSurface id={id} layout={layout} controller={controller} title={title} snapshot={snapshot} />}
    <button type="button" className="skillz-chat-pill" aria-expanded={open} aria-controls={open ? id : undefined}
      onClick={() => setOpen(value => !value)}>
      <span className="skillz-chat-dot" aria-hidden="true" />{open ? `Close ${title} chat` : `Chat with ${title}`}
    </button>
  </div>;
}

function ChatSurface({
  id, layout, controller, title, snapshot, className = '',
}: { id: string; layout: SkillzChatLayout; controller: SkillzChatController; title: string; snapshot: ReturnType<SkillzChatController['getSnapshot']>; className?: string }) {
  const { entries, pending, error, questionId } = snapshot;
  const [draft, setDraft] = useState('');
  const [approve, setApprove] = useState(false);
  const transcript = useRef<HTMLDivElement>(null);
  const displayed = layout === 'floating' ? entries.filter(entry => !entry.routed) : entries;
  const waitingForAnswer = displayed.some(entry => entry.id === questionId);

  useEffect(() => {
    const element = transcript.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, [entries, pending]);

  function send(event: FormEvent) {
    event.preventDefault();
    if (!draft.trim() || pending) return;
    void (waitingForAnswer && questionId
      ? controller.respond(questionId, draft)
      : controller.send(draft, { approveMutations: approve }));
    setDraft('');
    setApprove(false);
  }

  return <section id={id} className={`skillz-chat skillz-chat--${layout} ${className}`} aria-label={`${title} chat`}>
    <header className="skillz-chat-header"><h2>{title}</h2></header>
    <div ref={transcript} className="skillz-chat-transcript" role="log" aria-label="Conversation" aria-live="polite" tabIndex={0}>
      {displayed.map(entry => {
        if (entry.type === 'broadcast') return <p className="skillz-chat-broadcast" key={entry.id}>{entry.content}</p>;
        if (entry.type === 'elicitation') return <div className="skillz-chat-question" key={entry.id}>
          <p>{entry.question}</p>
          {entry.choices.length > 0 && <div className="skillz-chat-choices">{entry.choices.map((choice, index) =>
            <button type="button" key={index} disabled={pending || questionId !== entry.id}
              onClick={() => { setApprove(false); void controller.respond(entry.id, choice); }}>{choice}</button>)}</div>}
        </div>;
        return <div className="skillz-chat-message" data-role={entry.role} key={entry.id}>
          <span>{entry.role === 'assistant' ? title : 'You'}</span><p>{entry.content}</p>
        </div>;
      })}
      {pending && <p className="skillz-chat-status">Waiting for {title}…</p>}
    </div>
    <form className="skillz-chat-composer" onSubmit={send}>
      {error && <p role="alert" className="skillz-chat-error">{error}</p>}
      <label htmlFor={`${id}-message`}>{waitingForAnswer ? 'Your answer' : 'Message'}</label>
      <textarea id={`${id}-message`} rows={2} maxLength={10000} value={draft}
        onChange={event => setDraft(event.target.value)} />
      <div className="skillz-chat-controls">
        {!waitingForAnswer && <label className="skillz-chat-approval"><input type="checkbox" checked={approve}
          onChange={event => setApprove(event.target.checked)} /> Approve changes for this message</label>}
        <button type="submit" className="skillz-chat-send" disabled={pending || !draft.trim()}>Send</button>
      </div>
    </form>
  </section>;
}
