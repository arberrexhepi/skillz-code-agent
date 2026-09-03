import { useCallback, useEffect, useId, useRef, useState } from 'react';
import type { ArtifactCapabilities as Capabilities, ArtifactSetupProgress, ArtifactSetupSelection } from '../../../shared/artifacts';
import type { RuntimeSelection } from '../agent/agentWorkspace';

export function ArtifactCapabilities({ selection, firstTime, onReadyChange, onBusyChange }: { selection: RuntimeSelection; firstTime: boolean; onReadyChange: (ready: boolean) => void; onBusyChange: (busy: boolean) => void }): React.JSX.Element {
  const keyId = useId();
  const [result, setResult] = useState<Capabilities>();
  const [checking, setChecking] = useState(true);
  const [error, setError] = useState('');
  const [key, setKey] = useState('');
  const [saving, setSaving] = useState(false);
  const [installingBrowser, setInstallingBrowser] = useState(false);
  const [progress, setProgress] = useState<ArtifactSetupProgress>({ running: false, step: '', log: '' });
  const [expanded, setExpanded] = useState(firstTime);
  const sequence = useRef(0);
  const refresh = useCallback(async () => {
    const request = ++sequence.current;
    setChecking(true); setError('');
    try {
      const value = await window.workbench.artifacts.capabilities({ provider: selection.provider, model: selection.model } as ArtifactSetupSelection);
      if (sequence.current === request) { setResult(value); setExpanded(!value.ready); }
    } catch (cause) { if (sequence.current === request) { setResult(undefined); setError(clean(cause)); setExpanded(true); } }
    finally { if (sequence.current === request) setChecking(false); }
  }, [selection.provider, selection.model]);
  useEffect(() => {
    setResult(undefined); setKey('');
    void refresh();
    return () => { sequence.current++; };
  }, [refresh]);
  useEffect(() => {
    void window.workbench.artifacts.setupProgress().then(setProgress).catch(cause => setError(clean(cause)));
    return window.workbench.artifacts.onEvent(event => {
      if (event.type !== 'setup') return;
      setProgress(event.progress);
      if (!event.progress.running) void refresh();
    });
  }, [refresh]);
  const busy = progress.running || saving || installingBrowser;
  const ready = Boolean(result?.ready && !checking && !busy);
  useEffect(() => { onReadyChange(ready); }, [ready, onReadyChange]);
  useEffect(() => { onBusyChange(busy); return () => onBusyChange(false); }, [busy, onBusyChange]);
  async function install() {
    setError(''); setExpanded(true);
    setProgress({ running: true, step: 'Checking capabilities', log: '' });
    try { await window.workbench.artifacts.installCapabilities({ provider: selection.provider, model: selection.model } as ArtifactSetupSelection); }
    catch (cause) { setError(clean(cause)); }
    finally { setProgress(await window.workbench.artifacts.setupProgress().catch(() => ({ running: false, step: '', log: '' }))); await refresh(); }
  }
  async function installBrowser() {
    setInstallingBrowser(true); setError('');
    try { await window.workbench.artifacts.installBrowser(); await refresh(); }
    catch (cause) { setError(clean(cause)); }
    finally { setInstallingBrowser(false); }
  }
  async function saveKey(value: string | null) {
    setSaving(true); setError(''); setKey('');
    try { await window.workbench.artifacts.saveProviderKey(selection.provider as ArtifactSetupSelection['provider'], value); await refresh(); }
    catch (cause) { setError(clean(cause)); }
    finally { setSaving(false); }
  }
  const installable = result?.items.filter(item => item.installable && !item.optional) || [];
  return <section className="artifact-capabilities" aria-label="Artifact capabilities">
    <button type="button" className="artifact-capabilities-heading" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>
      <span><strong>{firstTime ? 'Set up artifacts' : 'Artifact capabilities'}</strong><small>{checking ? 'Checking this computer…' : ready ? 'Ready to create and run artifacts' : 'Install or repair the capabilities this artifact needs'}</small></span>
      <span className={ready ? 'capability-ready' : ''}>{ready ? 'Ready' : 'Setup'} {expanded ? '⌃' : '⌄'}</span>
    </button>
    {expanded && <div className="artifact-capabilities-body">
      <p>Install the tools needed to build and preview artifacts. Existing capabilities are reused, and provider support follows your runtime selection.</p>
      <ul>{result?.items.map(item => <li key={item.id}><span className={item.ready ? 'capability-ready' : item.optional ? '' : 'capability-missing'} aria-label={item.ready ? 'Ready' : item.optional ? 'Optional' : 'Needs setup'}>{item.ready ? '✓' : '○'}</span><div><strong>{item.label}{item.optional && ' (optional)'}</strong><small>{item.detail}</small></div>{item.id === 'browser' && item.installable && <button type="button" disabled={busy || checking} onClick={() => void installBrowser()}>{installingBrowser ? 'Installing Playwright…' : 'Install Playwright'}</button>}{item.download && <button type="button" disabled={busy} onClick={() => void window.workbench.artifacts.openSetupDownload(item.download!).catch(cause => setError(clean(cause)))}>Get {item.label}</button>}</li>)}</ul>
      {result?.keyName && <div className="artifact-provider-key"><label htmlFor={keyId}>{result.keyName}<input id={keyId} type="password" autoComplete="new-password" value={key} placeholder={result.keySaved ? 'Replace saved API key' : 'Paste your API key'} onChange={event => setKey(event.target.value)} disabled={busy || !result.canSaveKey} /></label><div className="artifact-capability-actions"><button type="button" disabled={busy || !key.trim() || !result.canSaveKey} onClick={() => void saveKey(key.trim())}>{saving ? 'Saving…' : 'Save API key'}</button>{result.keySaved && <button type="button" disabled={busy} onClick={() => void saveKey(null)}>Remove saved key</button>}</div><small>{result.canSaveKey ? 'Encrypted on this computer. Used only for artifact model requests; never added to artifact files. Restart a running artifact agent after changing its key.' : 'Secure storage is unavailable. Set the key in the environment that launches the workbench, then recheck.'}</small></div>}
      {installable.length > 0 && <p>Will install: {installable.map(item => item.label).join(', ')}. Provider packages use a workbench-managed Python environment. Downloads may take a few minutes.</p>}
      <div className="artifact-capability-actions"><button type="button" className="primary-button" disabled={busy || checking || !installable.length} onClick={() => void install()}>{progress.running ? 'Installing capabilities…' : 'Install capabilities'}</button><button type="button" disabled={busy || checking} onClick={() => void refresh()}>{checking ? 'Checking…' : 'Recheck'}</button></div>
      {progress.step && <p role="status">{progress.step}</p>}
      {(error || progress.error) && <p className="artifacts-error" role="alert">{error || progress.error}</p>}
      {progress.log && <details className="artifact-install-log"><summary>Installation details</summary><pre>{progress.log}</pre></details>}
    </div>}
  </section>;
}
function clean(error: unknown): string { return String(error).replace(/^Error invoking remote method '[^']+': Error: /, ''); }
