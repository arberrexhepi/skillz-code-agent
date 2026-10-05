import { useEffect, useState } from 'react';
import type { ArtifactVaultProvider, ArtifactVaultStatus } from '../../../shared/artifacts';

export function ArtifactVault(): React.JSX.Element {
  const [status, setStatus] = useState<ArtifactVaultStatus>();
  const [drafts, setDrafts] = useState<Partial<Record<ArtifactVaultProvider, string>>>({});
  const [busy, setBusy] = useState<ArtifactVaultProvider>();
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  async function refresh() {
    setError('');
    try { setStatus(await window.workbench.artifacts.vault()); }
    catch (cause) { setError(clean(cause)); }
  }

  useEffect(() => { void refresh(); }, []);

  async function save(provider: ArtifactVaultProvider, key: string | null) {
    setBusy(provider); setError(''); setNotice('');
    try {
      await window.workbench.artifacts.saveProviderKey(provider, key);
      setDrafts((current) => ({ ...current, [provider]: '' }));
      await refresh();
      setNotice(key ? 'Credential saved. Restart a running artifact agent to use it.' : 'Saved credential removed.');
    } catch (cause) { setError(clean(cause)); }
    finally { setBusy(undefined); }
  }

  return <main className="artifact-vault">
    <header>
      <div><span className="eyebrow">SECURE CREDENTIALS</span><h2>Vault</h2><p>Manage model-provider keys used by artifact agents and embedded chatbots.</p></div>
      <span className={status?.canSaveKey ? 'artifact-vault-security ready' : 'artifact-vault-security'}>{status?.canSaveKey ? 'Encrypted storage available' : 'Encrypted storage unavailable'}</span>
    </header>
    <div className="artifact-vault-note"><strong>Keys stay outside artifact repositories.</strong><span>The workbench encrypts saved values with the operating system credential store. Existing values are never displayed.</span></div>
    {error && <p className="artifacts-error" role="alert">{error}</p>}
    {notice && <p className="artifact-vault-notice" role="status">{notice}</p>}
    {!status && !error && <p className="muted">Loading Vault…</p>}
    <div className="artifact-vault-grid">{status?.entries.map((entry) => {
      const draft = drafts[entry.provider] || '';
      const active = busy === entry.provider;
      return <form className="artifact-vault-entry" key={entry.provider} onSubmit={(event) => { event.preventDefault(); if (draft.trim()) void save(entry.provider, draft.trim()); }}>
        <header><div><strong>{entry.label}</strong><code>{entry.keyName}</code></div><span className={'artifact-vault-source ' + entry.source}>{entry.source === 'saved' ? 'Saved in Vault' : entry.source === 'environment' ? 'From environment' : 'Not configured'}</span></header>
        <label>API key<input type="password" autoComplete="new-password" value={draft} placeholder={entry.source === 'missing' ? 'Paste a key' : 'Enter a replacement key'} disabled={active || !status.canSaveKey} onChange={(event) => setDrafts((current) => ({ ...current, [entry.provider]: event.target.value }))} /></label>
        <div className="artifact-form-actions"><button className="primary-button" disabled={active || !status.canSaveKey || !draft.trim()}>{active ? 'Saving…' : entry.source === 'missing' ? 'Save key' : 'Replace key'}</button>{entry.source === 'saved' && <button type="button" disabled={active} onClick={() => void save(entry.provider, null)}>Remove</button>}</div>
        {entry.source === 'environment' && <small>This key comes from the workbench process environment. Change that environment to remove it, or save an encrypted value here to override it.</small>}
      </form>;
    })}</div>
  </main>;
}

function clean(error: unknown): string {
  return String(error).replace(/^Error invoking remote method '[^']+': Error: /, '');
}