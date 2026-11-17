import { useCallback, useState } from 'react';
import { artifactAgentRuntimeSchema, type ArtifactRecord } from '../../../shared/artifacts';
import { useAgentWorkspace, type RuntimeSelection } from '../agent/agentWorkspace';
import { ArtifactCapabilities } from './ArtifactCapabilities';
import { RuntimeSelectionFields } from './agent/RuntimeDrawer';

export function ArtifactRuntimeSetup({ record, running, onReadyChange, onBusyChange }: {
  record: ArtifactRecord; running: boolean; onReadyChange: (ready: boolean) => void; onBusyChange: (busy: boolean) => void;
}): React.JSX.Element {
  const agent = useAgentWorkspace();
  const [selection, setSelection] = useState<RuntimeSelection>(() => record.runtime || agent.runtime);
  const [ready, setReady] = useState(false);
  const [installing, setInstalling] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const setupBusy = useCallback((busy: boolean) => { setInstalling(busy); onBusyChange(busy); }, [onBusyChange]);
  async function save() {
    setSaving(true); setError(''); setNotice('');
    try {
      await window.workbench.artifacts.saveRuntime(record.id, artifactAgentRuntimeSchema.parse(selection));
      if (running) await window.workbench.artifacts.start(record.id);
      setNotice('Runtime saved. The Agent panel uses this selection on its next start. New recordings and app model requests also use this selection.');
    } catch (cause) { setError(String(cause)); }
    finally { setSaving(false); }
  }
  return <div className="artifact-setup-panel">
    <ArtifactCapabilities artifactId={record.id} revision={ready ? 1 : 0} selection={selection} firstTime={false} onReadyChange={onReadyChange} onBusyChange={setupBusy} />
    <fieldset className="artifact-runtime-picker" disabled={saving || installing}>
      <legend>Agent runtime</legend>
      <p>Codex is discovered on this computer. You can choose its executable or sign in below.</p>
      <RuntimeSelectionFields value={selection} onChange={setSelection} options={agent.state.runtimeOptions} disabled={saving || installing} onReadyChange={setReady} showBackend={false} />
      {running && <p>Saving restarts this preview and stops any active recording. Saved recordings remain available.</p>}
      <button className="primary-button" disabled={!ready || saving || installing} onClick={() => void save()}>{saving ? 'Saving…' : running ? 'Save runtime & restart preview' : 'Save runtime'}</button>
    </fieldset>
    {installing && <p role="status">Installing capabilities. You can keep this artifact open.</p>}
    {notice && <p role="status">{notice}</p>}{error && <p role="alert">{error}</p>}
  </div>;
}
