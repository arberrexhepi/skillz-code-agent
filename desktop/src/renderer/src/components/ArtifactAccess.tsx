import { useEffect, useState } from 'react';
import type { ArtifactAccess, ReadDirectory } from '../../../shared/artifacts';

export const emptyAccess: ArtifactAccess = { directories: [], allowWorkspaceRead: false, allowWorkspaceProcessProxy: false };
const defaultProcessScript = /^(?:dev|start|serve|preview)(?::[A-Za-z0-9._-]+)?$/;

function ProcessScriptAccess({ scripts, allowlist, onChange }: { scripts?: string[]; allowlist?: string[]; onChange: (scripts: string[]) => void }) {
  if (!scripts) return <p className="muted">Create the artifact, then customize its script allowlist in File access.</p>;
  if (!scripts.length) return <p className="muted">No package.json scripts were detected in this folder.</p>;
  const allowed = new Set(allowlist ?? scripts.filter(name => defaultProcessScript.test(name)));
  const columns = [
    { title: 'Whitelist', items: scripts.filter(name => allowed.has(name)), allow: true },
    { title: 'Disallowed', items: scripts.filter(name => !allowed.has(name)), allow: false },
  ];
  const move = (name: string, permit: boolean) => {
    const next = new Set(allowed);
    if (permit) next.add(name); else next.delete(name);
    onChange(scripts.filter(script => next.has(script)));
  };
  return <div className="artifact-process-list" aria-label="Process Proxy script permissions">
    {columns.map(column => <section key={column.title}>
      <header><strong>{column.title}</strong><span>{column.items.length}</span></header>
      <div>{column.items.length ? column.items.map(name => <button type="button" key={name} title={column.allow ? `Disallow npm run ${name}` : `Allow npm run ${name}`} onClick={() => move(name, !column.allow)}><code>npm run {name}</code><span aria-hidden="true">{column.allow ? '→' : '←'}</span></button>) : <small>None</small>}</div>
    </section>)}
  </div>;
}

export function ArtifactAccessFields({ value, onChange, sourceRoot, disabled = false, processScripts = {} }: { value: ArtifactAccess; onChange: (value: ArtifactAccess) => void; sourceRoot: string; disabled?: boolean; processScripts?: Record<string, string[]> }) {
  const [error, setError] = useState('');
  const [choosing, setChoosing] = useState(false);
  const [detectedScripts, setDetectedScripts] = useState(processScripts);
  useEffect(() => setDetectedScripts(current => ({ ...current, ...processScripts })), [processScripts]);
  async function add() {
    setChoosing(true); setError('');
    try {
      const choice = await window.workbench.artifacts.chooseReadDirectory();
      if (choice && !value.directories.some(item => item.path === choice.path)) {
        const { packageScripts, ...directory } = choice;
        setDetectedScripts(current => ({ ...current, [directory.id]: packageScripts }));
        onChange({ ...value, directories: [...value.directories, directory] });
      }
    } catch (error) { setError(String(error)); }
    finally { setChoosing(false); }
  }
  function access(id: string, mode: 'read' | 'write') {
    onChange({ ...value, directories: value.directories.map(directory => directory.id === id ? { ...directory, access: mode } : directory) });
  }
  function processProxy(id: string, allowed: boolean) {
    onChange({ ...value, directories: value.directories.map(directory => directory.id === id ? { ...directory, allowProcessProxy: allowed } : directory) });
  }
  function processAllowlist(id: string, allowlist: string[]) {
    onChange({ ...value, directories: value.directories.map(directory => directory.id === id ? { ...directory, processProxyAllowlist: allowlist } : directory) });
  }
  function directoryRow(directory: ReadDirectory) {
    return <li className="artifact-access-directory" key={directory.id}>
      <div className="artifact-access-directory-heading"><span><strong>{directory.label}</strong><code>{directory.path}</code></span><span className="artifact-access-toggles"><label className="artifact-write-toggle"><input type="checkbox" checked={directory.access === 'write'} onChange={event => access(directory.id, event.target.checked ? 'write' : 'read')} /> Allow changes</label><label className="artifact-write-toggle"><input type="checkbox" checked={Boolean(directory.allowProcessProxy)} onChange={event => processProxy(directory.id, event.target.checked)} /> Allow Process Proxy</label></span><button type="button" aria-label={`Remove ${directory.label}`} onClick={() => onChange({ ...value, directories: value.directories.filter(item => item.id !== directory.id) })}>Remove</button></div>
      {directory.allowProcessProxy && <ProcessScriptAccess scripts={detectedScripts[directory.id]} allowlist={directory.processProxyAllowlist} onChange={allowlist => processAllowlist(directory.id, allowlist)} />}
    </li>;
  }
  return <fieldset className="artifact-access" disabled={disabled || choosing}>
    <legend>Allowed system directories</legend>
    <p>Every folder starts read only. Enable changes only when this artifact should be able to modify files in that folder.</p>
    {!value.directories.length && <p className="muted">No additional folders shared.</p>}
    <ul>{value.directories.map(directoryRow)}</ul>
    <button type="button" disabled={value.directories.length >= 30} onClick={() => void add()}>{choosing ? 'Choosing…' : 'Add folder…'}</button>
    {value.directories.some(directory => directory.access === 'write') && <p className="artifact-write-warning" role="status">Write access lets the artifact backend and agent create, replace, rename, and delete files in the selected folders. Stop or revoke access at any time.</p>}
    {value.directories.some(directory => directory.allowProcessProxy) && <p className="artifact-write-warning" role="status">Process Proxy runs whitelisted package scripts on the host as your user. Those scripts can access anything your user can access. Only enable it for repositories and artifacts you trust.</p>}
    <label><input type="checkbox" checked={value.allowWorkspaceRead} onChange={event => onChange({ ...value, allowWorkspaceRead: event.target.checked, allowWorkspaceProcessProxy: event.target.checked ? value.allowWorkspaceProcessProxy : false })} /> Allow active workbench repository reads</label>
    {value.allowWorkspaceRead && <label><input type="checkbox" checked={value.allowWorkspaceProcessProxy} onChange={event => onChange({ ...value, allowWorkspaceProcessProxy: event.target.checked })} /> Allow Process Proxy for active repository</label>}
    {value.allowWorkspaceRead && value.allowWorkspaceProcessProxy && <ProcessScriptAccess scripts={processScripts.workspace} allowlist={value.workspaceProcessProxyAllowlist} onChange={allowlist => onChange({ ...value, workspaceProcessProxyAllowlist: allowlist })} />}
    <p className="muted">{sourceRoot ? `Current repository: ${sourceRoot}` : 'No workbench repository is open.'} The repository open when a session starts is shared read only for that session.</p>
    {error && <div role="alert">{error}</div>}
  </fieldset>;
}
export function ArtifactAccessPanel({ id, sourceRoot }: { id: string; sourceRoot: string }) {
  const [value, setValue] = useState<ArtifactAccess>(emptyAccess);
  const [busy, setBusy] = useState(true);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [processScripts, setProcessScripts] = useState<Record<string, string[]>>({});
  useEffect(() => { let active = true; void Promise.all([window.workbench.artifacts.access(id), window.workbench.artifacts.processScripts(id)]).then(([access, scripts]) => { if (active) { setValue(access); setProcessScripts(scripts); } }).catch(error => { if (active) setError(String(error)); }).finally(() => { if (active) setBusy(false); }); return () => { active = false; }; }, [id]);
  async function save() {
    setBusy(true); setError(''); setMessage('');
    try { await window.workbench.artifacts.saveAccess(id, value); setMessage('Access saved. Start the preview or send a message to restart with these permissions.'); }
    catch (error) { setError(String(error)); }
    finally { setBusy(false); }
  }
  return <div className="artifact-access-panel"><ArtifactAccessFields value={value} onChange={value => { setValue(value); setMessage(''); }} sourceRoot={sourceRoot} disabled={busy} processScripts={processScripts} /><p>Saving stops the preview and agent so permission changes take effect.</p><button className="primary-button" disabled={busy} onClick={() => void save()}>Save file access</button>{message && <p role="status">{message}</p>}{error && <p role="alert">{error}</p>}</div>;
}
