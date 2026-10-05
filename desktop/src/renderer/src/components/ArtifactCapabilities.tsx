import { useCallback, useEffect, useRef, useState } from 'react';
import type { ArtifactCapabilities as Capabilities, ArtifactSetupProgress, ArtifactSetupSelection } from '../../../shared/artifacts';
import type { RuntimeSelection } from '../agent/agentWorkspace';

export function ArtifactCapabilities({ selection, firstTime, artifactId, onReadyChange, onBusyChange }: { selection: RuntimeSelection; artifactId?: string; firstTime: boolean; onReadyChange: (ready: boolean) => void; onBusyChange: (busy: boolean) => void }): React.JSX.Element {
  const [result, setResult] = useState<Capabilities>();
  const [checking, setChecking] = useState(true);
  const [error, setError] = useState('');
  const [installingBrowser, setInstallingBrowser] = useState(false);
  const [progress, setProgress] = useState<ArtifactSetupProgress>({ running: false, step: '', log: '' });
  const [expanded, setExpanded] = useState(firstTime);
  const sequence = useRef(0);
  const refresh = useCallback(async () => {
    const request = ++sequence.current;
    setChecking(true); setError('');
    try {
      const value = await window.workbench.artifacts.capabilities({ provider: selection.provider, model: selection.model } as ArtifactSetupSelection, artifactId);
      if (sequence.current === request) { setResult(value); setExpanded(!value.ready); }
    } catch (cause) { if (sequence.current === request) { setResult(undefined); setError(clean(cause)); setExpanded(true); } }
    finally { if (sequence.current === request) setChecking(false); }
  }, [selection.provider, selection.model, artifactId]);
  useEffect(() => {
    setResult(undefined);
    void refresh();
    return () => { sequence.current++; };
  }, [refresh]);
  useEffect(() => {
    void window.workbench.artifacts.setupProgress().then(setProgress).catch(cause => setError(clean(cause)));
    let lastRuntimeStatus: string | undefined;
    return window.workbench.artifacts.onEvent(event => {
      if (event.type === 'runtime' && event.runtime.id === artifactId && event.runtime.status !== lastRuntimeStatus) {
        lastRuntimeStatus = event.runtime.status;
        if (['running', 'error', 'stopped'].includes(lastRuntimeStatus)) void refresh();
      }
      if (event.type !== 'setup') return;
      setProgress(event.progress);
      if (!event.progress.running) void refresh();
    });
  }, [refresh, artifactId]);
  const busy = progress.running || installingBrowser;
  const ready = Boolean(result?.ready && !checking && !busy);
  useEffect(() => { onReadyChange(ready); }, [ready, onReadyChange]);
  useEffect(() => { onBusyChange(busy); return () => onBusyChange(false); }, [busy, onBusyChange]);
  async function install() {
    setError(''); setExpanded(true);
    setProgress({ running: true, step: 'Checking capabilities', log: '' });
    try { await window.workbench.artifacts.installCapabilities({ provider: selection.provider, model: selection.model } as ArtifactSetupSelection, artifactId); }
    catch (cause) { setError(clean(cause)); }
    finally { setProgress(await window.workbench.artifacts.setupProgress().catch(() => ({ running: false, step: '', log: '' }))); await refresh(); }
  }
  async function installBrowser() {
    setInstallingBrowser(true); setError('');
    try { await window.workbench.artifacts.installBrowser(); await refresh(); }
    catch (cause) { setError(clean(cause)); }
    finally { setInstallingBrowser(false); }
  }
  const installable = result?.items.filter(item => item.installable && !item.optional) || [];
  return <section className="artifact-capabilities" aria-label="Artifact capabilities">
    <button type="button" className="artifact-capabilities-heading" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>
      <span><strong>{firstTime ? 'Set up artifacts' : 'Artifact capabilities'}</strong><small>{checking ? 'Checking this computer…' : ready ? 'Ready to create and run artifacts' : 'Install or repair the capabilities this artifact needs'}</small></span>
      <span className={ready ? 'capability-ready' : ''}>{ready ? 'Ready' : 'Setup'} {expanded ? '⌃' : '⌄'}</span>
    </button>
    {expanded && <div className="artifact-capabilities-body">
      <p>{artifactId && 'Repairing artifact dependencies stops this artifact’s preview and agent; database files are preserved. '}Install the tools needed to build and preview artifacts. Existing capabilities are reused, and provider support follows your runtime selection.</p>
      <ul>{result?.items.filter(item => item.id !== 'credentials').map(item => <li key={item.id}><span className={item.ready ? 'capability-ready' : item.optional ? '' : 'capability-missing'} aria-label={item.ready ? 'Ready' : item.optional ? 'Optional' : 'Needs setup'}>{item.ready ? '✓' : '○'}</span><div><strong>{item.label}{item.optional && ' (optional)'}</strong><small>{item.detail}</small></div>{item.id === 'browser' && item.installable && <button type="button" disabled={busy || checking} onClick={() => void installBrowser()}>{installingBrowser ? 'Installing Playwright…' : 'Install Playwright'}</button>}{item.download && <button type="button" disabled={busy} onClick={() => void window.workbench.artifacts.openSetupDownload(item.download!).catch(cause => setError(clean(cause)))}>Get {item.label}</button>}</li>)}</ul>
      {installable.length > 0 && <p>Will install: {installable.map(item => item.label).join(', ')}. Provider packages use a workbench-managed Python environment. Downloads may take a few minutes.</p>}
      <div className="artifact-capability-actions"><button type="button" className="primary-button" disabled={busy || checking || !installable.length} onClick={() => void install()}>{progress.running ? 'Installing capabilities…' : artifactId ? 'Install / repair capabilities' : 'Install capabilities'}</button><button type="button" disabled={busy || checking} onClick={() => void refresh()}>{checking ? 'Checking…' : 'Recheck'}</button></div>
      {progress.step && <p role="status">{progress.step}</p>}
      {(error || progress.error) && <p className="artifacts-error" role="alert">{error || progress.error}</p>}
      {progress.log && <details className="artifact-install-log"><summary>Installation details</summary><pre>{progress.log}</pre></details>}
    </div>}
  </section>;
}
function clean(error: unknown): string { return String(error).replace(/^Error invoking remote method '[^']+': Error: /, ''); }
