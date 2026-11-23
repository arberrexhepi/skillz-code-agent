import { useEffect, useId, useRef, useState, type FormEvent } from 'react';
import type { McpApi, McpProfile, McpServerSettings } from '../../../shared/mcp';
import './mcpPanel.css';

const emptyDraft = { name: '', address: '', transport: 'http' as McpServerSettings['transport'],
  options: '{\n  "auth": "none"\n}' };
type Category = Parameters<McpApi['inspect']>[1];
type Page = Awaited<ReturnType<McpApi['inspect']>>;

function transportLabel(transport: McpServerSettings['transport']) {
  return transport === 'stdio' ? 'Executable' : 'Streamable HTTP';
}

function formatMcpError(cause: unknown) {
  const message = cause instanceof Error ? cause.message : String(cause);
  return message.replace(/^Error invoking remote method 'mcp:[^']+': (?:Error: )?/, '');
}

function serverDraft(name: string, server: McpProfile['servers'][string]) {
  const { transport, url, command, authStatus: _status, builtIn: _builtIn, ...options } = server;
  return { name, transport, address: url || command || '', options: JSON.stringify(options, null, 2) };
}

export function McpPanel({ artifactId, revision }: { artifactId?: string; revision?: number }) {
  const executableHelpId = useId();
  const formRef = useRef<HTMLFormElement>(null);
  const [profile, setProfile] = useState<McpProfile>();
  const [draft, setDraft] = useState(emptyDraft);
  const [editing, setEditing] = useState<string>();
  const [form, setForm] = useState(false);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [catalog, setCatalog] = useState<{ server: string; kind: Category; query: string; page?: Page }>();
  const servers = Object.entries(profile?.servers || {});

  useEffect(() => {
    let active = true;
    setError('');
    void window.workbench.mcp.profile(artifactId).then((value) => { if (active) setProfile(value); })
      .catch((cause) => { if (active) setError(formatMcpError(cause)); });
    return () => { active = false; };
  }, [artifactId, revision]);

  useEffect(() => {
    if (form) formRef.current?.querySelector<HTMLInputElement>('input:not([readonly])')?.focus();
  }, [form, editing]);

  async function work(label: string, action: () => Promise<void>) {
    setError('');
    setBusy(label);
    try { await action(); }
    catch (cause) { setError(formatMcpError(cause)); }
    finally { setBusy(''); }
  }

  function save(event: FormEvent) {
    event.preventDefault();
    void work('Saving…', async () => {
      const options: unknown = JSON.parse(draft.options);
      if (!options || typeof options !== 'object' || Array.isArray(options)) throw new Error('Options must be a JSON object.');
      const settings = { ...options, transport: draft.transport,
        ...(draft.transport === 'http' ? { url: draft.address.trim() } : { command: draft.address.trim() }) } as McpServerSettings;
      if (!editing && profile?.servers[draft.name.trim()]) throw new Error('This server name already exists.');
      setProfile(await window.workbench.mcp.save(draft.name.trim(), settings, artifactId));
      setForm(false);
    });
  }

  function manage(action: Parameters<McpApi['manage']>[0], name: string) {
    void work(action === 'login' ? 'Complete sign-in in your browser…' : 'Updating…', async () => {
      setProfile(await window.workbench.mcp.manage(action, name, artifactId));
      if (action === 'remove' && catalog?.server === name) setCatalog(undefined);
    });
  }

  function inspect(server: string, kind: Category, query = '', offset = 0) {
    setCatalog({ server, kind, query });
    void work('Finding capabilities…', async () => {
      setCatalog({ server, kind, query, page: await window.workbench.mcp.inspect(server, kind, artifactId, query, offset) });
    });
  }

  return (
    <section className="mcp-panel" aria-label={artifactId ? 'Artifact MCP servers' : 'Repository MCP servers'}>
      <header className="mcp-panel-header">
        <div className="mcp-heading">
          <h2>MCP servers</h2>
          <div className="mcp-actions">
            <button type="button" className="mcp-button-accent" disabled={!!busy || form} onClick={() => {
              setEditing(undefined);
              setDraft(emptyDraft);
              setForm(true);
            }}><span aria-hidden="true">+</span> Add server</button>
            <button type="button" className="mcp-icon-button" title="Refresh servers" aria-label="Refresh servers"
              disabled={!!busy} onClick={() => void work('Refreshing servers…', async () => {
                setProfile(await window.workbench.mcp.profile(artifactId));
              })}>↻</button>
          </div>
        </div>
        <p>Enabled servers are available to {artifactId ? 'this artifact’s agent' : 'this repository’s agent'}.</p>
      </header>
      {profile?.ready === false && <div className="mcp-notice">
        <p>Install the MCP runtime to connect to these servers.</p>
        <button className="mcp-button-accent" disabled={!!busy} onClick={() => void work('Installing MCP runtime…', async () => {
          await window.workbench.mcp.setup();
          setProfile(await window.workbench.mcp.profile(artifactId));
        })}>Install runtime</button>
      </div>}
      {(busy || (!profile && !error)) && <p className="mcp-status" role="status">{busy || 'Loading servers…'}</p>}
      {error && <p className="mcp-notice mcp-error" role="alert">{error}</p>}
      {form && <form ref={formRef} className="mcp-form" key={editing || ''} onSubmit={save}>
        <h3>{editing ? `Edit ${editing}` : 'Add server'}</h3>
        <div className="mcp-form-fields">
          <label>Server name<input required pattern="[A-Za-z0-9][A-Za-z0-9_.\-]{0,79}" value={draft.name}
            readOnly={!!editing} onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></label>
          <label>Transport<select value={draft.transport} onChange={(event) => setDraft({ ...draft,
            transport: event.target.value as McpServerSettings['transport'], address: '',
            options: event.target.value === 'stdio' ? '{\n  "args": [],\n  "env": []\n}' : emptyDraft.options })}>
            <option value="http">{transportLabel('http')}</option><option value="stdio">{transportLabel('stdio')}</option>
          </select></label>
        </div>
        <label>{draft.transport === 'http' ? 'MCP endpoint' : 'Executable path or command'}<input required value={draft.address}
          placeholder={draft.transport === 'http' ? 'https://example.com/mcp' : '/path/to/mcp-server'}
          aria-describedby={draft.transport === 'stdio' ? executableHelpId : undefined}
          onChange={(event) => setDraft({ ...draft, address: event.target.value })} /></label>
        {draft.transport === 'stdio' && <p className="mcp-field-help" id={executableHelpId}>Skillz launches this program on the host over stdio. Start any companion app or add-on it requires.</p>}
        <details className="mcp-options">
          <summary>Options (JSON)</summary>
          <div className="mcp-options-body">
            <textarea aria-label="Options (JSON)" rows={5} spellCheck={false} value={draft.options}
              onChange={(event) => setDraft({ ...draft, options: event.target.value })} />
            <p className="mcp-field-help">{draft.transport === 'http'
              ? <>Use <code>auth: "oauth"</code> for browser sign-in, or <code>auth: "bearer"</code> with a <code>bearerEnv</code> variable name.</>
              : <>Use <code>args</code> for command arguments and <code>env</code> for host variable names.</>} Use <code>allowedTools</code> to limit tool access.</p>
          </div>
        </details>
        <div className="mcp-actions mcp-form-actions">
          <button className="mcp-button-primary" disabled={!!busy} type="submit">Save server</button>
          <button type="button" disabled={!!busy} onClick={() => setForm(false)}>Cancel</button>
        </div>
      </form>}
      {profile && !servers.length && !form && <p className="mcp-empty">No MCP servers configured.</p>}
      {servers.length > 0 && <div className="mcp-servers">
        {servers.map(([name, server]) => {
          const enabled = server.enabled !== false;
          const address = server.url || server.command || '';
          const location = server.transport === 'stdio' ? address.split(/[\\/]/).pop() || address : address;
          return <article className="mcp-server" key={name}>
            <div className="mcp-server-row">
              <div className="mcp-server-heading">
                <span className="mcp-server-icon" data-transport={server.transport} aria-hidden="true" />
                <div className="mcp-server-meta">
                  <div className="mcp-server-title">
                    <h3>{name}</h3>
                    <span className="mcp-enabled" data-enabled={enabled}>{enabled ? 'Enabled' : 'Disabled'}</span>
                  </div>
                  <p className="mcp-endpoint" title={address}>
                    <span>{transportLabel(server.transport)}</span><code>{location}</code>
                  </p>
                </div>
              </div>
              <div className="mcp-actions mcp-server-actions">
                <button type="button" className="mcp-capabilities" aria-expanded={catalog?.server === name}
                  disabled={!!busy || !profile?.ready || !enabled}
                  onClick={() => catalog?.server === name ? setCatalog(undefined) : inspect(name, 'tools')}>Capabilities</button>
                {server.auth === 'oauth' && (server.authStatus === 'signed-in'
                  ? <button type="button" disabled={!!busy} onClick={() => manage('logout', name)}>Sign out</button>
                  : <button type="button" disabled={!!busy || !profile?.ready} onClick={() => manage('login', name)}>Sign in</button>)}
                <button type="button" className="mcp-button-subtle" disabled={!!busy}
                  onClick={() => manage(enabled ? 'disable' : 'enable', name)}>{enabled ? 'Disable' : 'Enable'}</button>
                <button type="button" className="mcp-button-subtle" disabled={!!busy} onClick={() => {
                  setEditing(name);
                  setDraft(serverDraft(name, server));
                  setForm(true);
                }}>Edit</button>
                {!server.builtIn && <button type="button" className="mcp-button-subtle mcp-button-remove" disabled={!!busy}
                  onClick={() => { if (window.confirm(`Remove MCP server ${name} from this workspace?`)) manage('remove', name); }}>Remove</button>}
              </div>
            </div>
            {catalog?.server === name && <div className="mcp-catalog">
              <div className="mcp-heading">
                <h4>Capabilities</h4>
                <button type="button" className="mcp-icon-button" title="Close capabilities" aria-label="Close capabilities"
                  onClick={() => setCatalog(undefined)}>×</button>
              </div>
              <form className="mcp-catalog-search" onSubmit={(event) => { event.preventDefault(); inspect(name, catalog.kind, catalog.query); }}>
                <label>Category<select disabled={!!busy} value={catalog.kind} onChange={(event) => inspect(name, event.target.value as Category)}>
                  <option value="tools">Tools</option><option value="resources">Resources</option>
                  <option value="templates">Resource templates</option><option value="prompts">Prompts</option>
                </select></label>
                <label>Search<input placeholder="Filter capabilities" value={catalog.query} onChange={(event) => setCatalog({ ...catalog, query: event.target.value })} /></label>
                <button disabled={!!busy}>Search</button>
              </form>
              {catalog.page && <>
                <div className="mcp-catalog-count">{catalog.page.total} {catalog.page.total === 1 ? 'result' : 'results'}</div>
                {catalog.page.items.length ? <ul className="mcp-results">{catalog.page.items.map((item) => {
                  const identifier = item.name || item.uri || item.uriTemplate;
                  return <li key={identifier}><code>{identifier}</code>{item.description && <p>{item.description}</p>}</li>;
                })}</ul> : <p className="mcp-field-help">No matching capabilities.</p>}
                {catalog.page.next_offset != null && <button type="button" className="mcp-next-page" disabled={!!busy}
                  onClick={() => inspect(name, catalog.kind, catalog.query, catalog.page?.next_offset)}>Next page <span aria-hidden="true">→</span></button>}
              </>}
            </div>}
          </article>;
        })}
      </div>}
    </section>
  );
}
