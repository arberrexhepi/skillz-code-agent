import { useEffect, useMemo, useState } from 'react';
import { useAgentWorkspace } from '../../agent/agentWorkspace';

export function RuntimeDrawer({ onClose }: { onClose: () => void }): React.JSX.Element {
  const agent = useAgentWorkspace();
  const [provider, setProvider] = useState(agent.runtime.provider);
  const [model, setModel] = useState(agent.runtime.model);
  const [limit, setLimit] = useState(agent.backoff?.token_limit_k || 0);
  const providers = agent.state.runtimeOptions?.providers?.filter((item) => !item.hidden) || [];
  const providerOption = useMemo(() => providers.find((item) => item.key === provider), [provider, providers]);
  const modelOptions = useMemo(() => uniqueModels(providers.length ? [
    providerOption?.selection_model,
    providerOption?.active_model,
    providerOption?.default_model,
    ...(providerOption?.suggested_models || []),
    provider === agent.runtime.provider ? agent.runtime.model : undefined,
  ] : [model, defaultModels[provider]]), [agent.runtime.model, agent.runtime.provider, model, provider, providerOption, providers.length]);
  useEffect(() => { setProvider(agent.runtime.provider); setModel(agent.runtime.model); }, [agent.runtime.model, agent.runtime.provider]);
  const selectProvider = (next: string): void => {
    setProvider(next);
    const option = providers.find((item) => item.key === next);
    const advertised = uniqueModels([option?.selection_model, option?.active_model, option?.default_model, ...(option?.suggested_models || [])]);
    setModel(advertised[0] || defaultModels[next] || '');
  };
  const providerKeys = providers.length ? providers.map((item) => String(item.key || '')) : Object.keys(defaultModels);
  return <section className="runtime-drawer">
    <header><div><span>RUNTIME</span><strong>Execution environment</strong></div><button className="icon-button" onClick={onClose}>×</button></header>
    <label>Provider<select value={provider} disabled={Boolean(agent.state.pendingAction)} onChange={(event) => selectProvider(event.target.value)}>{providerKeys.map((key) => <option value={key} key={key}>{providers.find((item) => item.key === key)?.label || key}</option>)}</select></label>
    <label>Model<select value={model} disabled={!modelOptions.length || Boolean(agent.state.pendingAction)} onChange={(event) => setModel(event.target.value)}>{modelOptions.map((item) => <option value={item} key={item}>{item}</option>)}</select></label>
    <button className="primary-button compact" disabled={!provider || !model || Boolean(agent.state.pendingAction)} onClick={() => void agent.switchRuntime(provider, model)}>Apply runtime</button>
    <label>Backend<select value={agent.runtime.backendScript} disabled={agent.state.status === 'running'} onChange={(event) => agent.setRuntime({ backendScript: event.target.value })}><option value="main.py">Stable runtime</option><option value="main_v2.py">Beta TreeLoop</option><option value="live_test_loop.py">Live TreeLoop</option></select></label>
    <div className="runtime-backoff"><label>Token backoff (K)<input type="number" min="0" value={limit} onChange={(event) => setLimit(Math.max(0, Number(event.target.value)))} /></label><button onClick={() => void agent.setBackoff(limit > 0, limit)}>{limit > 0 ? 'Enable' : 'Disable'}</button></div>
    {agent.backoff && <small>{agent.backoff.window_tokens_used.toLocaleString()} tokens used in current window</small>}
    <div className="runtime-actions">{agent.state.status === 'running' ? <button onClick={() => void agent.stop()}>Stop agent</button> : <button className="primary-button" onClick={() => void agent.start()}>Start agent</button>}</div>
  </section>;
}

const defaultModels: Record<string, string> = { gemini: 'gemini-3-flash-preview', openai: 'gpt-5.4', anthropic: 'claude-sonnet-4-6', meta: 'muse-spark-1.2', local: 'gemma4', 'ollama-local': 'qwen3-coder' };

function uniqueModels(models: Array<string | undefined>): string[] {
  return [...new Set(models.map((item) => String(item || '').trim()).filter(Boolean))];
}
