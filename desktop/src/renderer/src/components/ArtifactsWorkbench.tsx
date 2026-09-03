import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ArtifactLibrary, ArtifactRecord, ArtifactRuntime } from '../../../shared/artifacts';
import type { WorkbenchApi } from '../../../shared/contracts';
import { AgentWorkspaceProvider } from '../agent/AgentWorkspaceContext';
import { useAgentWorkspace, type RuntimeSelection } from '../agent/agentWorkspace';
import { RuntimeSelectionFields } from './agent/RuntimeDrawer';
import { AgentPanel } from './AgentPanel';
import { ArtifactAccessFields, ArtifactAccessPanel, emptyAccess } from './ArtifactAccess';
import { ArtifactCapabilities } from './ArtifactCapabilities';
import { ArtifactConnections } from './ArtifactConnections';
import { ArtifactPreview } from './ArtifactPreview';
import { FileNavigationContext } from './PathText';

export function ArtifactsWorkbench({ visible, sourceRoot, onClose }: { visible: boolean; sourceRoot: string; onClose: () => void }): React.JSX.Element {
  const sourceAgent = useAgentWorkspace();
  const [creationRuntime, setCreationRuntime] = useState<RuntimeSelection>(() => ({ ...sourceAgent.runtime }));
  const [access, setAccess] = useState(emptyAccess);
  const [runtimeReady, setRuntimeReady] = useState(false);
  const [capabilitiesReady, setCapabilitiesReady] = useState(false);
  const [setupBusy, setSetupBusy] = useState(false);
  const [library, setLibrary] = useState<ArtifactLibrary>({ root: '', artifacts: [] });
  const [open, setOpen] = useState<string[]>([]);
  const [active, setActive] = useState('');
  const [creating, setCreating] = useState(false);
  const [title, setTitle] = useState('');
  const [prompt, setPrompt] = useState('');
  const [facts, setFacts] = useState(false);
  const [memory, setMemory] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [requested, setRequested] = useState<string[]>([]);
  const [runtimes, setRuntimes] = useState<Record<string, ArtifactRuntime>>({});
  useEffect(() => { void window.workbench.artifacts.library().then(setLibrary).catch((error) => setError(clean(error))); return window.workbench.artifacts.onEvent((event) => { if (event.type === 'runtime') setRuntimes((current) => ({ ...current, [event.runtime.id]: event.runtime })); }); }, []);
  async function configure() { setBusy(true); setError(''); try { const next = await window.workbench.artifacts.chooseFolder(); if (next) { setLibrary(next); setOpen([]); setActive(''); setRuntimes({}); setCreating(true); } } catch (error) { setError(clean(error)); } finally { setBusy(false); } }
  function openArtifact(id: string) { setOpen((current) => current.includes(id) ? current : [...current, id]); setActive(id); setCreating(false); }
  async function create() {
    if (busy || setupBusy || !capabilitiesReady || !runtimeReady || !title.trim() || !prompt.trim()) return;
    setBusy(true); setError('');
    try {
      const record = await window.workbench.artifacts.create({ title, prompt, sourceRoot, access, shareFacts: Boolean(sourceRoot && facts), shareMemory: Boolean(sourceRoot && memory), runtime: { ...creationRuntime, backendScript: creationRuntime.backendScript as 'main.py' | 'main_v2.py' | 'live_test_loop.py' } });
      setLibrary((current) => ({ ...current, artifacts: [...current.artifacts, record] })); setRequested((current) => [...current, record.id]); openArtifact(record.id); setTitle(''); setPrompt(''); setAccess(emptyAccess); setFacts(false); setMemory(false);
    } catch (error) { setError(clean(error)); } finally { setBusy(false); }
  }
  async function closeTab(id: string) {
    setError('');
    try { await window.workbench.artifacts.stop(id); await window.workbench.artifacts.agentStop(id); setOpen((current) => current.filter((value) => value !== id)); if (active === id) setActive(open.find((value) => value !== id) || ''); }
    catch (error) { setError(clean(error)); }
  }
  return <section className="artifacts-workbench" hidden={!visible} aria-label="Artifacts workbench">
    <header className="artifacts-heading"><div><span className="eyebrow">YOUR IDEAS, THEIR OWN SPACE</span><h1>Artifacts</h1></div><p>Build visualizations, explorers, and small tools with the agent.</p><button onClick={onClose}>Back to workspace</button></header>
    {error && <div className="artifacts-error" role="alert">{error}<button onClick={() => setError('')} aria-label="Dismiss artifact error">×</button></div>}
    <div className="artifacts-layout">
      <aside className="artifacts-library"><header><strong>LIBRARY</strong><button aria-label="New artifact" disabled={!library.root || busy} onClick={() => setCreating(true)}>＋</button></header><p className="artifacts-location" title={library.root}>{library.root || 'Choose where your artifacts live.'}</p><button disabled={busy} onClick={() => void configure()}>{library.root ? 'Change folder' : 'Choose artifacts folder'}</button>{library.root && !library.artifacts.length && <p className="muted">Your first artifact starts with an idea.</p>}<nav aria-label="Artifact library">{library.artifacts.map((record) => <button className={active === record.id && !creating ? 'active' : ''} key={record.id} onClick={() => openArtifact(record.id)}><strong>{record.title}</strong><span>{runtimes[record.id]?.status || 'Ready to open'}</span></button>)}</nav><p className="artifacts-library-note">Each artifact has its own Git history, linked as a submodule in this library.</p></aside>
      <div className="artifacts-content"><div className="artifact-tabs" role="tablist" aria-label="Open artifacts">{open.map((id) => <div key={id} className={active === id && !creating ? 'active' : ''}><button role="tab" aria-selected={active === id && !creating} onClick={() => { setActive(id); setCreating(false); }}>{library.artifacts.find((item) => item.id === id)?.title}</button><button aria-label={`Close ${library.artifacts.find((item) => item.id === id)?.title}`} onClick={() => void closeTab(id)}>×</button></div>)}</div>
        {(!library.root || (!open.length && !creating)) && <div className="artifact-empty"><span>◇</span><h2>What would you like to make?</h2><p>A database schema map. A code relationship explorer. A dashboard for your project.</p>{library.root ? <button className="primary-button" onClick={() => setCreating(true)}>Create an artifact</button> : <><p>Choose an empty folder. skillz will initialize a Git repository for your library.</p><ArtifactCapabilities selection={creationRuntime} firstTime onReadyChange={setCapabilitiesReady} onBusyChange={setSetupBusy} /><button className="primary-button" disabled={busy} onClick={() => void configure()}>Choose artifacts folder</button></>}</div>}
        {creating && library.root && <form className="artifact-create" onSubmit={(event) => { event.preventDefault(); void create(); }}><span className="eyebrow">NEW ARTIFACT</span><h2>Start with an idea.</h2><ArtifactCapabilities selection={creationRuntime} firstTime={!library.artifacts.length} onReadyChange={setCapabilitiesReady} onBusyChange={setSetupBusy} /><label>Name<input required maxLength={120} value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Database schema explorer" /></label><label>What should the agent build?<textarea required maxLength={20000} rows={6} value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="Visualize our tables and their relationships, with filters and clickable details…" /></label><fieldset disabled={!sourceRoot}><legend>Share source workspace context</legend><p>{sourceRoot || 'Open a workspace to share its context files.'}</p><label><input type="checkbox" checked={facts} onChange={(event) => setFacts(event.target.checked)} /> Repo facts</label><label><input type="checkbox" checked={memory} onChange={(event) => setMemory(event.target.checked)} /> Memory observability</label></fieldset><ArtifactAccessFields value={access} onChange={setAccess} sourceRoot={sourceRoot} disabled={busy} /><fieldset className="artifact-runtime-picker" disabled={busy}><legend>Agent runtime</legend><RuntimeSelectionFields value={creationRuntime} onChange={setCreationRuntime} options={sourceAgent.state.runtimeOptions} disabled={busy || setupBusy} onReadyChange={setRuntimeReady} /></fieldset><button className="primary-button" disabled={busy || setupBusy || !capabilitiesReady || !runtimeReady || !title.trim() || !prompt.trim()}>{busy ? 'Creating…' : 'Create & ask agent'}</button></form>}
        {open.map((id) => { const record = library.artifacts.find((item) => item.id === id); return record && <ArtifactSession key={id} record={record} sourceRoot={sourceRoot} active={visible && active === id && !creating} runtime={runtimes[id]} initialRuntime={record.runtime || sourceAgent.runtime} request={requested.includes(id)} onRequested={() => setRequested((current) => current.filter((value) => value !== id))} />; })}
      </div>
    </div>
  </section>;
}
function ArtifactSession({ record, sourceRoot, active, runtime, initialRuntime, request, onRequested }: { record: ArtifactRecord; sourceRoot: string; active: boolean; runtime?: ArtifactRuntime; initialRuntime: RuntimeSelection; request: boolean; onRequested: () => void }) {
  const api = useMemo<WorkbenchApi['agent']>(() => {
    const artifacts = window.workbench.artifacts, id = record.id;
    return { ...window.workbench.agent, runtimeOptions: (provider, model) => artifacts.agentRuntimeOptions(id, provider, model), start: (options) => artifacts.agentStart(id, options), submit: (text) => artifacts.agentSubmit(id, text), plannerAction: (action, extras) => artifacts.agentPlannerAction(id, action, extras), workerAction: (action) => artifacts.agentWorkerAction(id, action), reconfigureRuntime: (provider, model) => artifacts.agentReconfigure(id, provider, model), configureBackoff: (enabled, limit) => artifacts.agentBackoff(id, enabled, limit), stop: () => artifacts.agentStop(id), onEvent: (listener) => artifacts.onEvent((event) => { if (event.type === 'agent' && event.id === id) listener(event.event); }) };
  }, [record.id]);
  // A separate provider preserves the source agent and every other artifact conversation.
  return <div className="artifact-session" hidden={!active}><AgentWorkspaceProvider api={api} initialRuntime={initialRuntime}><FileNavigationContext.Provider value={null}><ArtifactSessionBody record={record} sourceRoot={sourceRoot} active={active} runtime={runtime} request={request} onRequested={onRequested} /></FileNavigationContext.Provider></AgentWorkspaceProvider></div>;
}
function ArtifactSessionBody({ record, sourceRoot, active, runtime, request, onRequested }: { record: ArtifactRecord; sourceRoot: string; active: boolean; runtime?: ArtifactRuntime; request: boolean; onRequested: () => void }) {
  const agent = useAgentWorkspace();
  const [section, setSection] = useState<'preview' | 'connections' | 'access' | 'setup' | 'logs'>('preview');
  const [error, setError] = useState(''); const [busy, setBusy] = useState(false);
  const [setupNeeded, setSetupNeeded] = useState(false);
  const [setupBusy, setSetupBusy] = useState(false);
  const setupReady = useCallback((ready: boolean) => setSetupNeeded(!ready), []);
  useEffect(() => {
    if (!active) return;
    let stale = false;
    void window.workbench.artifacts.capabilities({ provider: agent.runtime.provider, model: agent.runtime.model } as import('../../../shared/artifacts').ArtifactSetupSelection)
      .then(value => { if (!stale) setSetupNeeded(!value.ready); })
      .catch(() => { if (!stale) setSetupNeeded(true); });
    return () => { stale = true; };
  }, [active, agent.runtime.provider, agent.runtime.model, runtime?.status, agent.state.status]);
  const sent = useRef(false);
  useEffect(() => { if (request && !sent.current) { sent.current = true; void Promise.resolve().then(() => agent.submit(record.prompt)).then((ok) => { if (!ok) setError('The artifact was created, but the agent could not accept the request. Check Runtime and choose Send original request.'); onRequested(); }); } }, [request, agent, record.prompt, onRequested]);
  async function run(action: () => Promise<unknown>) { setBusy(true); setError(''); try { await action(); } catch (error) { setError(clean(error)); } finally { setBusy(false); } }
  return <><div className="artifact-canvas"><div className="artifact-toolbar"><strong>{record.title}</strong><span className={`artifact-runtime ${runtime?.status || ''}`}>{runtime?.status || 'stopped'}</span><button disabled={busy || runtime?.status === 'installing' || runtime?.status === 'starting'} onClick={() => void run(() => window.workbench.artifacts.start(record.id))}>{runtime?.status === 'running' ? 'Running' : 'Start preview'}</button>{runtime && runtime.status !== 'stopped' && <button onClick={() => void run(() => window.workbench.artifacts.stop(record.id))}>Stop</button>}<button onClick={() => void run(() => window.workbench.artifacts.reveal(record.id))}>Open folder</button></div><div className="artifact-section-tabs">{(['preview', 'connections', 'access', 'setup', 'logs'] as const).map((item) => <button className={section === item ? 'active' : ''} key={item} onClick={() => setSection(item)}>{item === 'preview' ? 'Preview' : item === 'connections' ? 'API connections' : item === 'access' ? 'File access' : item === 'setup' ? 'Setup' : 'Server logs'}</button>)}<button disabled={Boolean(agent.state.pendingAction)} onClick={() => void run(() => agent.submit(record.prompt))}>Send original request</button></div>{setupNeeded && section !== 'setup' && <div className="artifact-setup-notice" role="status">Some artifact capabilities need setup or repair.<button onClick={() => setSection('setup')}>Repair capabilities</button></div>}{error && <div className="artifacts-error" role="alert">{error}</div>}{record.contextMode !== 'none' && <div className="artifact-context">Shared context: {record.shareFacts ? 'repo facts' : ''}{record.shareFacts && record.shareMemory ? ' + ' : ''}{record.shareMemory ? 'memory observability' : ''} · {record.contextMode === 'snapshot' ? 'read-only snapshot, refreshed automatically' : record.contextMode === 'links' ? 'linked files' : record.contextWarning}</div>}<ArtifactPreview title={record.title} active={active && section === 'preview'} runtime={runtime} />{section === 'connections' && <ArtifactConnections id={record.id} />}{section === 'access' && <ArtifactAccessPanel id={record.id} sourceRoot={sourceRoot} />}{section === 'setup' && <div className="artifact-setup-panel"><ArtifactCapabilities selection={agent.runtime} firstTime={false} onReadyChange={setupReady} onBusyChange={setSetupBusy} />{setupBusy && <p role="status">Installing capabilities. You can keep this artifact open.</p>}</div>}{section === 'logs' && <pre className="artifact-logs">{runtime?.logs || 'Start preview to install dependencies and launch the server.'}{runtime?.error && `\n${runtime.error}`}</pre>}</div><div className="artifact-agent"><AgentPanel label="ARTIFACT AGENT" /></div></>;
}
function clean(error: unknown): string { return String(error).replace(/^Error invoking remote method '[^']+': Error: /, ''); }
