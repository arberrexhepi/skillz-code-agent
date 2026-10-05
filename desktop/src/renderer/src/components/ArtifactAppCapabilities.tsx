import { useEffect, useState } from 'react';
import { artifactAppSchema, defaultArtifactApp, type ArtifactAppConfig } from '../../../shared/artifacts';

export function ArtifactAppFields({ value, onChange, disabled = false }: { value: ArtifactAppConfig; onChange: (value: ArtifactAppConfig) => void; disabled?: boolean }): React.JSX.Element {
  const database = value.database;
  return <fieldset className="artifact-app-capabilities" disabled={disabled}>
    <legend>App capabilities</legend>
    <p>Choose the services installed into this artifact. They run inside its isolated Docker preview.</p>
    <label className="artifact-capability-choice"><input type="checkbox" checked={value.chatbot.enabled} onChange={event => onChange({ ...value, chatbot: { enabled: event.target.checked } })} /><span><strong>Skillz chatbot</strong><small>Run the Python Skillz process from the Node app and expose API and database commands as its tools.</small></span></label>
    <label className="artifact-capability-choice"><input type="checkbox" checked={database.enabled} onChange={event => onChange({ ...value, database: { ...database, enabled: event.target.checked } })} /><span><strong>Database with Sequelize</strong><small>Add a managed Sequelize connection and read/mutation commands.</small></span></label>
    {database.enabled && <div className="artifact-field-row artifact-capability-settings"><label>DBMS<select value={database.dialect} onChange={event => onChange({ ...value, database: { ...database, dialect: event.target.value as ArtifactAppConfig['database']['dialect'] } })}><option value="sqlite">SQLite</option><option value="postgres">PostgreSQL</option><option value="mysql">MySQL</option><option value="mariadb">MariaDB</option><option value="mssql">Microsoft SQL Server</option></select></label>{database.dialect === 'sqlite' ? <label>Database file<input value={database.storage} onChange={event => onChange({ ...value, database: { ...database, storage: event.target.value } })} /></label> : <label>Connection URL environment variable<input pattern="[A-Za-z_][A-Za-z0-9_]*" value={database.connectionEnv} onChange={event => onChange({ ...value, database: { ...database, connectionEnv: event.target.value } })} /></label>}</div>}
  </fieldset>;
}

export function ArtifactAppCapabilities({ id, running }: { id: string; running: boolean }): React.JSX.Element {
  const [config, setConfig] = useState<ArtifactAppConfig>(defaultArtifactApp);
  const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const [notice, setNotice] = useState('');
  useEffect(() => { let current = true; void window.workbench.artifacts.app(id).then(value => { if (current) setConfig(value); }).catch(error => { if (current) setError(String(error)); }); return () => { current = false; }; }, [id]);
  async function save() { setBusy(true); setError(''); setNotice(''); try { const value = artifactAppSchema.parse(config); const result = await window.workbench.artifacts.saveApp(id, value); setConfig(value); const applied = running ? 'Restart the preview to install and apply them.' : 'They will be installed on the next preview start.'; const history = result.commit ? ` Migration commit ${result.commit.slice(0, 8)}.${result.checkpointCommit ? ` Earlier managed-file edits were saved in checkpoint ${result.checkpointCommit.slice(0, 8)}.` : ''}` : ''; setNotice(result.migrated ? `Artifact runtime upgraded and capabilities saved.${history} ${applied}` : `Capabilities saved. ${applied}`); } catch (error) { setError(String(error)); } finally { setBusy(false); } }
  return <div className="artifact-app-panel"><header><div><h2>App capabilities</h2><p>Runtime features generated for this artifact.</p></div></header><ArtifactAppFields value={config} onChange={setConfig} disabled={busy} />{error && <p className="artifacts-error" role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}<button className="primary-button" disabled={busy} onClick={() => void save()}>{busy ? 'Saving…' : 'Save capabilities'}</button></div>;
}
