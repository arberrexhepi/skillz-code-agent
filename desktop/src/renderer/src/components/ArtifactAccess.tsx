import { useEffect, useState } from 'react';
import type { ArtifactAccess } from '../../../shared/artifacts';

export const emptyAccess: ArtifactAccess = { directories: [], allowWorkspaceRead: false };
export function ArtifactAccessFields({ value, onChange, sourceRoot, disabled = false }: { value: ArtifactAccess; onChange: (value: ArtifactAccess) => void; sourceRoot: string; disabled?: boolean }) {
  const [error, setError] = useState('');
  const [choosing, setChoosing] = useState(false);
  async function add() {
    setChoosing(true); setError('');
    try {
      const directory = await window.workbench.artifacts.chooseReadDirectory();
      if (directory && !value.directories.some((item) => item.path === directory.path)) onChange({ ...value, directories: [...value.directories, directory] });
    } catch (error) { setError(String(error)); }
    finally { setChoosing(false); }
  }
  return <fieldset className="artifact-access" disabled={disabled || choosing}>
    <legend>Allowed system directories</legend>
    <p>The artifact and its agent can read these folders. They cannot change the files in them.</p>
    {!value.directories.length && <p className="muted">No additional folders shared.</p>}
    <ul>{value.directories.map((directory) => <li key={directory.id}><span><strong>{directory.label}</strong><code>{directory.path}</code></span><span className="muted">Read only</span><button type="button" aria-label={`Remove ${directory.label}`} onClick={() => onChange({ ...value, directories: value.directories.filter((item) => item.id !== directory.id) })}>Remove</button></li>)}</ul>
    <button type="button" disabled={value.directories.length >= 30} onClick={() => void add()}>{choosing ? 'Choosing…' : 'Add folder…'}</button>
    <label><input type="checkbox" checked={value.allowWorkspaceRead} onChange={(event) => onChange({ ...value, allowWorkspaceRead: event.target.checked })} /> Allow active workbench repository reads</label>
    <p className="muted">{sourceRoot ? `Current repository: ${sourceRoot}` : 'No workbench repository is open.'} The repository open when a session starts is shared read only for that session.</p>
    {error && <div role="alert">{error}</div>}
  </fieldset>;
}
export function ArtifactAccessPanel({ id, sourceRoot }: { id: string; sourceRoot: string }) {
  const [value, setValue] = useState<ArtifactAccess>(emptyAccess);
  const [busy, setBusy] = useState(true);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  useEffect(() => { let active = true; void window.workbench.artifacts.access(id).then((access) => { if (active) setValue(access); }).catch((error) => { if (active) setError(String(error)); }).finally(() => { if (active) setBusy(false); }); return () => { active = false; }; }, [id]);
  async function save() {
    setBusy(true); setError(''); setMessage('');
    try { await window.workbench.artifacts.saveAccess(id, value); setMessage('Access saved. Start the preview or send a message to restart with these permissions.'); }
    catch (error) { setError(String(error)); }
    finally { setBusy(false); }
  }
  return <div className="artifact-access-panel"><ArtifactAccessFields value={value} onChange={(value) => { setValue(value); setMessage(''); }} sourceRoot={sourceRoot} disabled={busy} /><p>Saving stops the preview and agent so permission changes take effect.</p><button className="primary-button" disabled={busy} onClick={() => void save()}>Save file access</button>{message && <p role="status">{message}</p>}{error && <p role="alert">{error}</p>}</div>;
}
