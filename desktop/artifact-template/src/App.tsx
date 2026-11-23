import { useEffect, useState, type ComponentType } from 'react';
import metadata from '../artifact.json';
import appConfig from '../.artifact/app.json';

type ChatLayout = 'floating' | 'sidebar' | 'workspace';
const { chatbot = { enabled: false } } = appConfig as {
  chatbot?: { enabled: boolean; includeShell?: boolean; shellLayout?: ChatLayout };
};
const { SkillzChat } = import.meta.glob<{ SkillzChat: ComponentType<{ layout: ChatLayout }> }>(
  './skillz-chat/index.tsx', { eager: true },
)['./skillz-chat/index.tsx'] || {};

export default function App() {
  const [context, setContext] = useState('');
  const [error, setError] = useState('');
  useEffect(() => { document.title = metadata.title; }, []);
  async function loadContext() {
    try { const response = await fetch('/context/repo-facts'); if (!response.ok) throw new Error('Repo facts are not shared yet.'); setContext(await response.text()); setError(''); }
    catch (error) { setError(String(error)); }
  }
  const canvas = <main><span className="eyebrow">SKILLZ ARTIFACT</span><h1>{metadata.title}</h1><p>{metadata.prompt}</p><section><h2>Your canvas is ready.</h2><p>Ask the artifact agent to build a visualization, explorer, or tool here.</p><button onClick={loadContext}>Read shared repo facts</button>{error && <p role="alert">{error}</p>}{context && <pre>{context}</pre>}</section></main>;
  if (!SkillzChat || !chatbot.enabled || chatbot.includeShell === false) return canvas;
  const layout = chatbot.shellLayout || 'floating';
  if (layout === 'workspace') return <div className="artifact-chat-starter-workspace"><SkillzChat layout={layout} /></div>;
  return <div className={layout === 'sidebar' ? 'artifact-chat-starter-sidebar' : undefined}>{canvas}<SkillzChat layout={layout} /></div>;
}
