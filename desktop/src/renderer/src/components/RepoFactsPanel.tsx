import { PathText } from './PathText';
import { useEffect, useMemo, useState } from 'react';
import { filterRepoFacts, REPO_FACTS_PATH, type RepoFact, type RepoFactsFilter, type RepoFactsIssue, type RepoFactsLedger, type RepoFactsSnapshot } from '../../../shared/repoFacts';

export function RepoFactsPanel({ workspaceRoot, revision, onOpenPath, onOpenIssue }: {
  workspaceRoot: string;
  revision: number;
  onOpenPath: (path: string) => void;
  onOpenIssue?: (id: string) => void;
}): React.JSX.Element {
  const [snapshot, setSnapshot] = useState<RepoFactsSnapshot>();
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let current = true;
    setLoading(true);
    setError('');
    void window.workbench.workspace.repoFacts(workspaceRoot).then((next) => {
      if (!current) return;
      if (next.workspaceRoot !== workspaceRoot) throw new Error('Workspace changed. Refresh Repo Facts in the intended folder.');
      setSnapshot(next);
    }).catch((cause: unknown) => {
      if (!current) return;
      setSnapshot(undefined);
      setError(String(cause).replace(/^Error invoking remote method '[^']+': Error: /, '').replace(/^Error: /, ''));
    }).finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [workspaceRoot, revision, retry]);
  const saved = snapshot?.workspaceRoot === workspaceRoot ? snapshot : undefined;
  return <section className="repo-facts-panel" aria-label="Repository facts">
    <header className="repo-facts-intro"><p>Durable knowledge saved by the agent.</p>
      <button type="button" disabled={!saved || saved.status === 'missing'} onClick={() => onOpenPath(REPO_FACTS_PATH)}>Open source ↗</button>
    </header>
    <p className="repo-facts-file"><code>{REPO_FACTS_PATH}</code><span role="status">{loading ? (saved ? 'Refreshing…' : 'Loading…') : saved?.modifiedAt ? `Saved ${formatDate(saved.modifiedAt)}` : ''}</span></p>
    {error && <div className="repo-facts-error" role="alert"><p><PathText>{error}</PathText></p><button type="button" onClick={() => setRetry((value) => value + 1)}>Retry</button></div>}
    {saved?.status === 'missing' && <div className="repo-facts-empty"><h3>No repository facts yet</h3><p>The agent creates <code>repo_facts.md</code> when it records durable facts in this workspace.</p></div>}
    {saved?.status === 'invalid' && <div className="repo-facts-error" role="alert"><h3>Could not render repository facts</h3><p><PathText>{saved.error}</PathText></p><button type="button" onClick={() => setRetry((value) => value + 1)}>Retry</button></div>}
    {saved?.ledger && <RepoFactsView ledger={saved.ledger} onOpenIssue={onOpenIssue} />}
  </section>;
}

export function RepoFactsView({ ledger, onOpenIssue }: { ledger: RepoFactsLedger; onOpenIssue?: (id: string) => void }): React.JSX.Element {
  const [filter, setFilter] = useState<RepoFactsFilter>({ query: '', kind: 'all', scope: 'all' });
  const [limit, setLimit] = useState(40);
  const scopes = ledger.issues.filter(issue => issue.facts.length > 0);
  const scope = scopes.some(issue => issue.id === filter.scope) ? filter.scope : 'all';
  const records = useMemo(() => filterRepoFacts(ledger, { ...filter, scope }).flatMap(issue => issue.facts.map(fact => ({ issue, fact }))), [ledger, filter, scope]);
  const facts = ledger.issues.flatMap(issue => issue.facts);
  const change = (next: Partial<RepoFactsFilter>): void => { setFilter(current => ({ ...current, ...next })); setLimit(40); };
  return <>
    <dl className="repo-facts-counts facts-only-counts" aria-label="Saved fact counts"><div><dt>Architecture</dt><dd>{facts.filter(fact => fact.kind === 'architecture').length}</dd></div><div><dt>Goal facts</dt><dd>{facts.filter(fact => fact.kind === 'goal').length}</dd></div></dl>
    {ledger.warnings.map(warning => <p className="repo-facts-warning" role="status" key={warning}><PathText>{warning}</PathText></p>)}
    {ledger.legacy && <p className="repo-facts-note">Legacy facts are shown as repository architecture. The file has not been changed.</p>}
    <div className="repo-facts-filters"><label>Search records<input type="search" placeholder="Fact key, value, or source…" value={filter.query} onChange={event => change({ query: event.target.value })} /></label>
      <label>Record type<select value={filter.kind} onChange={event => change({ kind: event.target.value as RepoFactsFilter['kind'] })}><option value="all">All facts</option><option value="architecture">Architecture facts</option><option value="goal">Goal facts</option></select></label>
      <label>Scope<select value={scope} onChange={event => change({ scope: event.target.value })}><option value="all">Repository and all issues</option>{scopes.map(issue => <option value={issue.id} key={issue.id}>{issue.repositoryScope ? 'Repository' : issue.id}</option>)}</select></label>
    </div>
    <p className="repo-facts-match-count" role="status">{records.length} of {facts.length} saved facts</p>
    {!records.length && <div className="repo-facts-empty"><h3>{facts.length ? 'No matching records' : 'No saved facts'}</h3><p>{facts.length ? 'Try a different search, record type, or scope.' : 'Architecture and goal facts will appear here when recorded. Manage open and closed work in Issues.'}</p>{facts.length > 0 && <button type="button" onClick={() => change({ query: '', kind: 'all', scope: 'all' })}>Clear filters</button>}</div>}
    {records.slice(0, limit).map(({ issue, fact }, index) => <FactRecord key={`${issue.id}:${fact.kind}:${fact.key}:${index}`} fact={fact} issue={issue} onOpenIssue={onOpenIssue} />)}
    {records.length > limit && <button type="button" className="repo-facts-more" onClick={() => setLimit(value => value + 40)}>Show more facts ({records.length - limit} remaining)</button>}
    <details className="repo-facts-file-details"><summary>File metadata</summary><dl className="repo-facts-metadata"><Meta label="Format" value={ledger.legacy ? 'Legacy flat facts' : ledger.schemaVersion ? `Schema ${ledger.schemaVersion}` : 'Empty file'} /></dl>{Object.keys(ledger.migration).length > 0 && <><h4>Migration metadata</h4><pre><PathText>{JSON.stringify(ledger.migration, null, 2)}</PathText></pre></>}<p>Only retained facts are shown. Issue actions and completed-goal history are available in Issues.</p></details>
  </>;
}
function FactRecord({ fact, issue, onOpenIssue }: { fact: RepoFact; issue: RepoFactsIssue; onOpenIssue?: (id: string) => void }): React.JSX.Element {
  return <article className="repo-fact-record standalone"><span className={`repo-fact-kind ${fact.kind}`}>{fact.kind === 'goal' ? 'Goal fact' : 'Architecture'}</span><h4><code><PathText>{fact.key}</PathText></code></h4><p className="repo-fact-value"><PathText>{fact.value}</PathText></p>
    <div className="repo-fact-owner"><span>Recorded for </span>{issue.repositoryScope ? <span>Repository</span> : onOpenIssue ? <button type="button" onClick={() => onOpenIssue(issue.id)} aria-label={`View ${issue.id} in Issues`}>{issue.id} ↗</button> : <code>{issue.id}</code>}</div>
    <details><summary>Provenance{fact.run ? ` · Run ${fact.run}` : ''}{fact.step ? ` · Step ${fact.step}` : ''}</summary><dl className="repo-facts-metadata"><Meta label="Source action" value={fact.source || 'Not recorded'} /><Meta label="Run" value={fact.run ? String(fact.run) : 'Not recorded'} /><Meta label="Step" value={fact.step ? String(fact.step) : 'Not recorded'} /></dl></details>
  </article>;
}
function Meta({ label, value }: { label: string; value: string }): React.JSX.Element | null { return value ? <div><dt>{label}</dt><dd><PathText>{value}</PathText></dd></div> : null; }
function formatDate(value: number): string { return new Date(value).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }); }
