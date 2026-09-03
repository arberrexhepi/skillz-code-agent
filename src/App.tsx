import { useEffect, useMemo, useState } from 'react';
import { files, type SharedFolder } from './files';
import { createIssue, newLedger, parseLedgerDocument, serializeLedger, transitionIssue, type Issue, type LedgerDocument } from './issues';

interface DocumentState extends LedgerDocument { root: SharedFolder; source: string; hash: string }
const emptyHash = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855';
async function sha256(value: string): Promise<string> { return [...new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value)))].map(byte => byte.toString(16).padStart(2, '0')).join(''); }
export default function App() {
  const [roots, setRoots] = useState<SharedFolder[]>([]); const [selected, setSelected] = useState('');
  const [document, setDocument] = useState<DocumentState>(); const [filter, setFilter] = useState<'open' | 'closed' | 'all'>('open');
  const [query, setQuery] = useState(''); const [summary, setSummary] = useState(''); const [plan, setPlan] = useState('');
  const [busy, setBusy] = useState(false); const [message, setMessage] = useState(''); const [error, setError] = useState('');
  useEffect(() => { window.document.title = 'Repository issue manager'; void refreshRoots(); }, []);
  async function refreshRoots() { try { const value = (await files.roots()).filter(root => root.id !== 'repo'); setRoots(value); if (!selected && value[0]) setSelected(value[0].id); } catch (cause) { setError(String(cause)); } }
  async function load(id = selected) {
    const root = roots.find(item => item.id === id); if (!root) return;
    setBusy(true); setError(''); setMessage('');
    try {
      let source: string, hash: string;
      try { source = await files.readText(id, 'repo_facts.md'); hash = await sha256(source); }
      catch (cause) {
        if (root.readOnly || !String(cause).includes('ENOENT')) throw cause;
        source = newLedger(); hash = emptyHash;
      }
      setDocument({ root, source, hash, ...parseLedgerDocument(source) });
    } catch (cause) { setDocument(undefined); setError(String(cause)); }
    finally { setBusy(false); }
  }
  useEffect(() => { if (selected && roots.some(root => root.id === selected)) void load(selected); }, [selected, roots.length]);
  async function update(change: (ledger: DocumentState['ledger']) => void, success: string): Promise<boolean> {
    if (!document || document.root.readOnly) return false;
    setBusy(true); setError(''); setMessage('');
    try {
      const ledger = structuredClone(document.ledger); change(ledger);
      const source = serializeLedger(document, ledger);
      const result = await files.writeText(document.root.id, 'repo_facts.md', source, document.hash);
      setDocument({ ...document, source, hash: result.sha256, ledger }); setMessage(success); return true;
    } catch (cause) { setError(String(cause)); return false; }
    finally { setBusy(false); }
  }
  function issue(id: string, action: 'activate' | 'close' | 'reopen') {
    void update(ledger => {
      transitionIssue(ledger, id, action);
    }, action === 'activate' ? `${id} is now active.` : `${id} ${action === 'close' ? 'closed' : 'reopened'}.`);
  }
  function create(event: React.FormEvent) {
    event.preventDefault(); const request = summary.trim(); if (!request) return;
    void update(ledger => {
      createIssue(ledger, request, plan);
    }, 'Issue created and activated.').then(saved => { if (saved) { setSummary(''); setPlan(''); } });
  }
  const issues = useMemo(() => (document?.ledger.issues || []).filter(item => !['global-architecture', 'legacy-architecture'].includes(item.issue_id)).filter(item => filter === 'all' || (item.status === 'open' ? 'open' : 'closed') === filter).filter(item => [item.issue_id, item.request_summary, item.plan_summary].join(' ').toLocaleLowerCase().includes(query.toLocaleLowerCase())).sort((a, b) => Number(b.issue_id === document?.ledger.active_issue_id) - Number(a.issue_id === document?.ledger.active_issue_id) || Number(b.status === 'open') - Number(a.status === 'open') || b.issue_id.localeCompare(a.issue_id, undefined, { numeric: true })), [document, filter, query]);
  return <main><header><div><span className="eyebrow">READY-MADE ARTIFACT</span><h1>Repository issue manager</h1><p>Manage durable Skillz issues without starting an agent.</p></div><button onClick={() => void load()} disabled={!selected || busy}>Refresh</button></header>
    {!roots.length && <section className="empty"><h2>No repositories shared</h2><p>Open this artifact's <strong>File access</strong> tab, add a repository folder, enable <strong>Allow changes</strong>, and save.</p></section>}
    {roots.length > 0 && <><nav className="repos" aria-label="Shared repositories">{roots.map(root => <button key={root.id} className={selected === root.id ? 'active' : ''} onClick={() => setSelected(root.id)}><strong>{root.label}</strong><span>{root.readOnly ? 'Read only' : 'Can manage issues'}</span></button>)}</nav>
      {error && <div className="alert" role="alert">{error}</div>}{message && <div className="notice" role="status">{message}</div>}
      {document && <div className="workspace"><aside><div className="filters"><input aria-label="Filter issues" placeholder="Filter issues…" value={query} onChange={event => setQuery(event.target.value)} /><div>{(['open','closed','all'] as const).map(value => <button key={value} className={filter === value ? 'active' : ''} onClick={() => setFilter(value)}>{value}</button>)}</div></div>{document.root.readOnly ? <p className="readonly">This repository is read only. Enable Allow changes in File access to manage it.</p> : <form onSubmit={create}><h2>Create issue</h2><label>Summary<input value={summary} onChange={event => setSummary(event.target.value)} /></label><label>Plan<textarea rows={3} value={plan} onChange={event => setPlan(event.target.value)} /></label><button disabled={busy || !summary.trim()}>Create & activate</button></form>}</aside>
        <section className="issues"><div className="issue-heading"><h2>{issues.length} {filter === 'all' ? '' : filter} issue{issues.length === 1 ? '' : 's'}</h2><code>repo_facts.md</code></div>{issues.map(item => { const active = item.issue_id === document.ledger.active_issue_id; const open = item.status === 'open'; return <article key={item.issue_id} className={active ? 'active' : ''}><div className="issue-top"><code>{item.issue_id}</code><span className={open ? 'open' : 'closed'}>{active ? 'active' : open ? 'open' : 'closed'}</span></div><h3>{item.request_summary || item.plan_summary || 'Untitled issue'}</h3>{item.plan_summary && item.plan_summary !== item.request_summary && <p>{item.plan_summary}</p>}<footer><span>{item.priority ? `Priority ${item.priority}` : item.opened_at || item.closed_at || ''}</span>{!document.root.readOnly && <div>{open && !active && <button disabled={busy} onClick={() => issue(item.issue_id, 'activate')}>Make active</button>}{open ? <button disabled={busy} onClick={() => issue(item.issue_id, 'close')}>Close</button> : <button disabled={busy} onClick={() => issue(item.issue_id, 'reopen')}>Reopen</button>}</div>}</footer></article>; })}{!issues.length && <div className="empty"><h2>No matching issues</h2><p>Adjust the filters or create a new issue.</p></div>}</section></div>}
    </>}</main>;
}

