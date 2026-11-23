import { useEffect, useState } from 'react';
import { artifactMcpSourcePath, type ArtifactAppMcp, type ArtifactAppMcpRequest, type ArtifactAppMcpServer } from '../../../shared/artifactAppMcp';

function newServer(transport: ArtifactAppMcpServer['transport'] = 'stdio'): ArtifactAppMcpServer {
  const defaults = { id: '', enabled: true, chatbot: false, timeout: 30 };
  return transport === 'stdio' ? { ...defaults, transport, command: '', args: [], env: [] } : { ...defaults, transport, url: '' };
}

export function ArtifactAppMcpFields({ value, onChange }: { value: ArtifactAppMcp; onChange: (value: ArtifactAppMcp) => void }) {
  function change(index: number, server: ArtifactAppMcpServer) {
    onChange({ ...value, servers: value.servers.map((current, position) => position === index ? server : current) });
  }
  return <>
    <label className="artifact-capability-choice"><input type="checkbox" checked={value.enabled}
      onChange={event => onChange({ ...value, enabled: event.target.checked })} />
      <span><strong>Artifact MCP</strong><small>Install an MCP service and client inside this app’s preview container.</small></span>
    </label>
    {value.enabled && <div className="artifact-mcp-settings">
      <p>Executables run in the container. HTTP connects to an HTTPS server or to a service on the container’s own loopback.</p>
      {value.servers.map((server, index) => <fieldset className="artifact-mcp-server" key={index}>
        <legend>{server.id || (server.source === 'artifact' ? 'New artifact MCP server' : 'New MCP connection')}</legend>
        <div className="artifact-field-row">
          <label>Server ID<input value={server.id} placeholder="asset-search" onChange={event => change(index, { ...server, id: event.target.value })} /></label>
          {server.source === 'artifact' ? <label>Transport<input value="Artifact-local executable" readOnly /></label> : <label>Transport<select value={server.transport} onChange={event => {
            const { id, enabled, chatbot, allowedTools, timeout } = server;
            change(index, { ...newServer(event.target.value as ArtifactAppMcpServer['transport']), id, enabled, chatbot, allowedTools, timeout });
          }}><option value="stdio">Executable</option><option value="http">Streamable HTTP</option></select></label>}
        </div>
        {server.transport === 'stdio' ? <>
          {server.source === 'artifact' ? <p>Server source <code>{artifactMcpSourcePath(server.id || 'server-id')}</code>.
            Saving creates a starter if the file is missing. The artifact agent can edit it.</p> : <label>Executable<input value={server.command} placeholder="node" onChange={event => change(index, { ...server, command: event.target.value })} /></label>}
          <div className="artifact-field-row">
            {server.source !== 'artifact' && <label>Arguments, one per line<textarea rows={2} value={server.args.join('\n')}
              onChange={event => change(index, { ...server, args: event.target.value ? event.target.value.split('\n') : [] })} /></label>}
            <label>Environment variable names, comma separated<input value={server.env.join(',')}
              onChange={event => change(index, { ...server, env: event.target.value ? event.target.value.split(',').map(name => name.trim()) : [] })} /></label>
          </div>
        </> : <>
          <label>MCP endpoint<input value={server.url} placeholder="https://example.com/mcp"
            onChange={event => change(index, { ...server, url: event.target.value })} /></label>
          <label>Bearer token environment variable<input value={server.bearerEnv || ''} placeholder="Optional"
            onChange={event => change(index, { ...server, bearerEnv: event.target.value || undefined })} /></label>
        </>}
        <label>Allowed tools, comma separated<input value={server.allowedTools?.join(',') || ''} placeholder="All tools"
          onChange={event => change(index, { ...server, allowedTools: event.target.value ? event.target.value.split(',').map(tool => tool.trim()) : undefined })} /></label>
        <div className="artifact-mcp-server-actions">
          <label><input type="checkbox" checked={server.enabled} onChange={event => change(index, { ...server, enabled: event.target.checked })} />Enabled</label>
          <label><input type="checkbox" checked={server.chatbot} onChange={event => change(index, { ...server, chatbot: event.target.checked })} />Allow in Skillz chatbot</label>
          <button type="button" onClick={() => onChange({ ...value, servers: value.servers.filter((_, position) => position !== index) })}>Remove server</button>
        </div>
      </fieldset>)}
      <div className="artifact-mcp-server-actions">
        <button type="button" disabled={value.servers.length >= 24} onClick={() => onChange({
          ...value, servers: [...value.servers, { id: '', enabled: true, chatbot: false, timeout: 30, transport: 'stdio', source: 'artifact', env: [] }],
        })}>Create MCP server</button>
        <button type="button" disabled={value.servers.length >= 24} onClick={() => onChange({ ...value, servers: [...value.servers, newServer()] })}>Add MCP server</button>
      </div>
    </div>}
  </>;
}

type Operation = ArtifactAppMcpRequest['operation'];
type Kind = NonNullable<ArtifactAppMcpRequest['kind']>;
type Entry = { name?: string; uri?: string; uriTemplate?: string; description?: string };

export function ArtifactAppMcpInspector({ id, previewUrl }: { id: string; previewUrl?: string }) {
  const [servers, setServers] = useState<string[]>([]);
  const [server, setServer] = useState('');
  const [operation, setOperation] = useState<Operation>('list');
  const [kind, setKind] = useState<Kind>('tools');
  const [query, setQuery] = useState('');
  const [target, setTarget] = useState('');
  const [args, setArgs] = useState('{}');
  const [approved, setApproved] = useState(false);
  const [result, setResult] = useState<Record<string, unknown>>();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const browsing = operation === 'list' || operation === 'search';
  const takesArguments = operation === 'call' || operation === 'get-prompt';

  useEffect(() => {
    let current = true;
    setServers([]); setServer(''); setResult(undefined); setError(''); setApproved(false);
    if (!previewUrl) return;
    void window.workbench.artifacts.requestAppMcp(id, { operation: 'servers' }).then(response => {
      if (!current) return;
      if (!response.ok || !Array.isArray(response.servers)) throw new Error(String(response.error || 'Could not list artifact MCP servers.'));
      const names = response.servers.map(item => String(item.name));
      setServers(names); setServer(names[0] || '');
    }).catch(cause => { if (current) setError(String(cause)); });
    return () => { current = false; };
  }, [id, previewUrl]);

  async function run(offset = 0) {
    setBusy(true); setError(''); setResult(undefined);
    try {
      const parameters = takesArguments ? JSON.parse(args) : undefined;
      if (takesArguments && (!parameters || typeof parameters !== 'object' || Array.isArray(parameters))) throw new Error('Arguments must be a JSON object.');
      const response = await window.workbench.artifacts.requestAppMcp(id, {
        server, operation, kind, query, offset, arguments: parameters,
        ...(target ? operation === 'read-resource' ? { uri: target } : { name: target } : {}),
        approveMutations: operation === 'call' && approved,
      });
      if (response.ok !== true) throw new Error(String(response.error || JSON.stringify(response.result) || 'MCP request failed.'));
      setResult(response);
    } catch (cause) { setError(String(cause)); }
    finally { setBusy(false); setApproved(false); }
  }

  const entries = Array.isArray(result?.items) ? result.items as Entry[] : undefined;
  return <section className="artifact-mcp-inspector">
    <h3>Run MCP tools and read resources</h3>
    {previewUrl ? <p>MCP endpoint <code>{previewUrl}/_skillz/mcp</code></p> : <p>Start the preview to use its MCP servers.</p>}
    {error && <p className="artifacts-error" role="alert">{error}</p>}
    {previewUrl && !servers.length && !error && <p>Add and enable servers, then restart the preview.</p>}
    {servers.length > 0 && <fieldset disabled={busy}>
      <div className="artifact-field-row">
        <label>Server<select value={server} onChange={event => { setServer(event.target.value); setResult(undefined); setTarget(''); setApproved(false); }}>
          {servers.map(name => <option key={name}>{name}</option>)}
        </select></label>
        <label>Operation<select value={operation} onChange={event => { setOperation(event.target.value as Operation); setResult(undefined); setApproved(false); }}>
          <option value="list">Browse capabilities</option><option value="search">Search capabilities</option>
          <option value="describe">Describe capability</option><option value="call">Call tool</option>
          <option value="read-resource">Read resource</option><option value="get-prompt">Get prompt</option>
        </select></label>
      </div>
      {(browsing || operation === 'describe') && <label>Category<select value={kind} onChange={event => { setKind(event.target.value as Kind); setResult(undefined); }}>
        <option value="tools">Tools</option><option value="resources">Resources</option>
        <option value="templates">Resource templates</option><option value="prompts">Prompts</option>
      </select></label>}
      {operation === 'search' && <label>Search<input value={query} onChange={event => setQuery(event.target.value)} /></label>}
      {!browsing && <label>{operation === 'read-resource' ? 'Resource URI' : 'Capability name'}<input value={target} onChange={event => { setTarget(event.target.value); setApproved(false); }} /></label>}
      {takesArguments && <label>Arguments (JSON)<textarea rows={3} value={args} onChange={event => { setArgs(event.target.value); setApproved(false); }} /></label>}
      {operation === 'call' && <label className="artifact-mcp-approve"><input type="checkbox" checked={approved}
        onChange={event => setApproved(event.target.checked)} />Approve changes for this tool call</label>}
      <button type="button" disabled={!browsing && !target.trim()} onClick={() => void run()}>{busy ? 'Running…' : 'Run MCP request'}</button>
    </fieldset>}
    {entries ? <>
      <ul>{entries.map(entry => {
        const name = kind === 'resources' ? entry.uri : kind === 'templates' ? entry.uriTemplate : entry.name;
        return <li key={name}><button type="button" onClick={() => {
          setTarget(name || ''); setOperation(kind === 'tools' ? 'call' : kind === 'resources' ? 'read-resource' : kind === 'prompts' ? 'get-prompt' : 'describe');
          setArgs('{}'); setApproved(false); setResult(undefined);
        }}><code>{name}</code></button>{entry.description && <p>{entry.description}</p>}</li>;
      })}</ul>
      {typeof result?.next_offset === 'number' && <button type="button" disabled={busy} onClick={() => void run(Number(result.next_offset))}>Next page</button>}
    </> : result && <pre>{JSON.stringify(result.item || result.result || result, null, 2)}</pre>}
  </section>;
}
