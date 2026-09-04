import { useEffect, useState } from 'react';
import metadata from '../artifact.json';
export default function App() {
  const [context, setContext] = useState('');
  const [error, setError] = useState('');
  useEffect(() => { document.title = metadata.title; }, []);
  async function loadContext() {
    try { const response = await fetch('/context/repo-facts'); if (!response.ok) throw new Error('Repo facts are not shared yet.'); setContext(await response.text()); setError(''); }
    catch (error) { setError(String(error)); }
  }
  return <main><span className="eyebrow">SKILLZ ARTIFACT</span><h1>{metadata.title}</h1><p>{metadata.prompt}</p><section><h2>Your canvas is ready.</h2><p>Ask the artifact agent to build a visualization, explorer, or tool here.</p><button onClick={loadContext}>Read shared repo facts</button>{error && <p role="alert">{error}</p>}{context && <pre>{context}</pre>}</section></main>;
}
