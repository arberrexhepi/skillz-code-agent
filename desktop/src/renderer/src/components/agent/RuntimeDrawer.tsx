import { useEffect, useMemo, useState } from 'react';
import { useAgentWorkspace } from '../../agent/agentWorkspace';
import type { CodexSubscriptionStatus } from '../../../../shared/agentTypes';

const CODEX_SUBSCRIPTION_PROVIDER = 'codex-subscription';

export function RuntimeDrawer({ onClose }: { onClose: () => void }): React.JSX.Element {
  const agent = useAgentWorkspace();
  const [provider, setProvider] = useState(agent.runtime.provider);
  const [model, setModel] = useState(agent.runtime.model);
  const [limit, setLimit] = useState(agent.backoff?.token_limit_k || 0);
  const [codexStatus, setCodexStatus] = useState<CodexSubscriptionStatus>();
  const [codexStatusBusy, setCodexStatusBusy] = useState(false);
  const [cliPath, setCliPath] = useState('');
  const [cliError, setCliError] = useState('');
  const [cliNotice, setCliNotice] = useState('');
  useEffect(() => { setCliPath(codexStatus?.configured_cli_path || ''); }, [codexStatus?.configured_cli_path]);
  const providers = agent.state.runtimeOptions?.providers?.filter((item) => !item.hidden) || [];
  const providerOption = useMemo(() => providers.find((item) => item.key === provider), [provider, providers]);
  const modelOptions = useMemo(() => uniqueModels(providers.length ? [
    ...(provider === CODEX_SUBSCRIPTION_PROVIDER ? codexStatus?.models || [] : []),
    providerOption?.selection_model,
    providerOption?.active_model,
    providerOption?.default_model,
    ...(providerOption?.suggested_models || []),
    provider === agent.runtime.provider ? agent.runtime.model : undefined,
  ] : [model, defaultModels[provider]]), [agent.runtime.model, agent.runtime.provider, codexStatus?.models, model, provider, providerOption, providers.length]);
  useEffect(() => { setProvider(agent.runtime.provider); setModel(agent.runtime.model); }, [agent.runtime.model, agent.runtime.provider]);
  const refreshCodexStatus = async (): Promise<void> => {
    setCodexStatusBusy(true);
    try {
      setCodexStatus(await window.workbench.agent.codexSubscriptionStatus());
    } catch (cause) {
      setCodexStatus((current) => ({ ...current, available: false, authenticated: false, error: cleanError(cause) }));
    } finally {
      setCodexStatusBusy(false);
    }
  };
  const signInToCodex = async (): Promise<void> => {
    setCodexStatusBusy(true);
    try {
      setCodexStatus(await window.workbench.agent.codexSubscriptionLogin());
    } catch (cause) {
      setCodexStatus((current) => ({ ...current, available: current?.available ?? true, authenticated: false, error: cleanError(cause) }));
    } finally {
      setCodexStatusBusy(false);
    }
  };
  useEffect(() => {
    if (provider === CODEX_SUBSCRIPTION_PROVIDER) void refreshCodexStatus();
  }, [provider, agent.state.status]);
  const chooseCli = async (): Promise<void> => {
    setCodexStatusBusy(true);
    setCliError('');
    setCliNotice('');
    try {
      const selected = await window.workbench.agent.chooseCodexCli();
      if (selected) setCliPath(selected);
    } catch (cause) {
      setCliError(cleanError(cause));
    } finally {
      setCodexStatusBusy(false);
    }
  };
  const saveCli = async (candidate: string | null): Promise<void> => {
    setCodexStatusBusy(true);
    setCliError('');
    setCliNotice('');
    try {
      const status = await window.workbench.agent.setCodexCliPath(candidate);
      setCodexStatus(status);
      setCliPath(status.configured_cli_path || '');
      setCliNotice(candidate ? 'Codex CLI path saved on this computer.' : 'Automatic discovery restored.');
    } catch (cause) {
      setCliError(cleanError(cause));
    } finally {
      setCodexStatusBusy(false);
    }
  };
  const selectProvider = (next: string): void => {
    setProvider(next);
    const option = providers.find((item) => item.key === next);
    const advertised = uniqueModels([option?.selection_model, option?.active_model, option?.default_model, ...(option?.suggested_models || [])]);
    setModel(advertised[0] || defaultModels[next] || '');
  };
  const providerKeys = providers.length ? providers.map((item) => String(item.key || '')) : Object.keys(defaultModels);
  return <section className="runtime-drawer">
    <header><div><span>RUNTIME</span><strong>Execution environment</strong></div><button className="icon-button" aria-label="Close runtime settings" onClick={onClose}>×</button></header>
    <label>Provider<select value={provider} disabled={Boolean(agent.state.pendingAction)} onChange={(event) => selectProvider(event.target.value)}>{providerKeys.map((key) => <option value={key} key={key}>{providers.find((item) => item.key === key)?.label || key}</option>)}</select></label>
    <label>Model<select value={model} disabled={!modelOptions.length || Boolean(agent.state.pendingAction)} onChange={(event) => setModel(event.target.value)}>{modelOptions.map((item) => <option value={item} key={item}>{item}</option>)}</select></label>
    {provider === CODEX_SUBSCRIPTION_PROVIDER && <CodexSubscriptionCard
      status={codexStatus} busy={codexStatusBusy} onRefresh={refreshCodexStatus} onSignIn={signInToCodex}
      cliPath={cliPath} cliError={cliError} cliNotice={cliNotice}
      onPathChange={(value) => { setCliPath(value); setCliError(''); setCliNotice(''); }}
      onChoose={chooseCli} onSave={() => saveCli(cliPath.trim())} onReset={() => saveCli(null)}
    />}
    {providerOption?.notes && provider !== CODEX_SUBSCRIPTION_PROVIDER && <p className="runtime-provider-note">{String(providerOption.notes)}</p>}
    <button className="primary-button compact" disabled={!provider || !model || Boolean(agent.state.pendingAction) || (provider === CODEX_SUBSCRIPTION_PROVIDER && (!codexStatus?.authenticated || codexStatus?.restart_required))} onClick={() => void agent.switchRuntime(provider, model)}>Apply runtime</button>
    <label>Backend<select value={agent.runtime.backendScript} disabled={agent.state.status === 'running'} onChange={(event) => agent.setRuntime({ backendScript: event.target.value })}><option value="main.py">Stable runtime</option><option value="main_v2.py">Beta TreeLoop</option><option value="live_test_loop.py">Live TreeLoop</option></select></label>
    <div className="runtime-backoff"><label>Token backoff (K)<input type="number" min="0" value={limit} onChange={(event) => setLimit(Math.max(0, Number(event.target.value)))} /></label><button onClick={() => void agent.setBackoff(limit > 0, limit)}>{limit > 0 ? 'Enable' : 'Disable'}</button></div>
    {agent.backoff && <small>{agent.backoff.window_tokens_used.toLocaleString()} tokens used in current window</small>}
    <div className="runtime-actions">{agent.state.status === 'running' ? <button onClick={() => void agent.stop()}>Stop agent</button> : <button className="primary-button" onClick={() => void agent.start()}>Start agent</button>}</div>
  </section>;
}

export function CodexSubscriptionCard({
  status,
  busy,
  onRefresh,
  onSignIn,
  cliPath,
  cliError,
  cliNotice,
  onPathChange,
  onChoose,
  onSave,
  onReset,
}: {
  status?: CodexSubscriptionStatus;
  busy: boolean;
  onRefresh: () => Promise<void>;
  onSignIn: () => Promise<void>;
  cliPath: string;
  cliError: string;
  cliNotice: string;
  onPathChange: (value: string) => void;
  onChoose: () => Promise<void>;
  onSave: () => Promise<void>;
  onReset: () => Promise<void>;
}): React.JSX.Element {
  const identity = [status?.plan_type, status?.email].filter(Boolean).join(' · ');
  const state = busy ? 'Checking local Codex…' : status?.authenticated
    ? `Connected${identity ? ` · ${identity}` : ''}`
    : status?.available === false ? 'Codex CLI unavailable' : 'ChatGPT sign-in required';
  return <div className={`runtime-subscription ${status?.authenticated ? 'connected' : ''}`}>
    <div><span>LOCAL SESSION</span><strong>{state}</strong></div>
    <p>Uses your local Codex ChatGPT subscription allowance. OpenAI API-key invocation remains a separate provider.</p>
    {status?.error && <small className="runtime-cli-message" role="alert">{status.error}</small>}
    {status?.cli_version && <small>{status.cli_version}</small>}
    {status?.cli_path && <small className="runtime-cli-message">Using {status.cli_path_source === 'settings' ? 'saved path' : status.cli_path_source === 'environment' ? 'environment override' : 'detected path'}: {status.cli_path}</small>}
    <fieldset className="runtime-cli-location" disabled={busy}>
      <legend>Locate Codex CLI</legend>
      <p>Choose an executable if automatic discovery fails. Saved only on this computer.</p>
      <label htmlFor="codex-cli-path">Executable path</label>
      <input id="codex-cli-path" value={cliPath} placeholder="Automatic discovery" spellCheck={false} onChange={(event) => onPathChange(event.target.value)} aria-describedby="codex-cli-help" />
      <div className="runtime-cli-buttons">
        <button onClick={() => void onChoose()}>Browse…</button>
        <button disabled={!cliPath.trim()} onClick={() => void onSave()}>Save and check</button>
        {status?.configured_cli_path && <button onClick={() => void onReset()}>Use automatic discovery</button>}
      </div>
      <details id="codex-cli-help">
        <summary>How to find the executable</summary>
        <p>Windows: choose <code>codex.exe</code>. Check <code>%LOCALAPPDATA%\OpenAI\Codex\bin</code> and its runtime folders, or run <code>(Get-Command codex).Source</code> in PowerShell. For an npm installation, choose the native <code>codex.exe</code> inside the installed package, rather than its .cmd or .ps1 launcher.</p>
        <p>macOS / Linux: run <code>command -v codex</code> in Terminal and paste the path. Choose the <code>codex</code> executable, without command arguments.</p>
      </details>
    </fieldset>
    {cliError && <small className="runtime-cli-message runtime-cli-error" role="alert">{cliError}</small>}
    {cliNotice && <small className="runtime-cli-message" role="status">{cliNotice}</small>}
    {status?.restart_required && <p role="status">Stop and start the agent to use this CLI path for model turns. Status and sign-in already use the new setting.</p>}
    <footer>
      <button disabled={busy} onClick={() => void onRefresh()}>Refresh</button>
      {!status?.authenticated && <button className="primary-button" disabled={busy || status?.available === false} onClick={() => void onSignIn()}>Sign in with ChatGPT</button>}
    </footer>
  </div>;
}

const defaultModels: Record<string, string> = { gemini: 'gemini-3-flash-preview', openai: 'gpt-5.4', 'codex-subscription': 'gpt-5.6-terra', anthropic: 'claude-sonnet-4-6', meta: 'muse-spark-1.2', local: 'gemma4', 'ollama-local': 'qwen3-coder' };

function uniqueModels(models: Array<string | undefined>): string[] {
  return [...new Set(models.map((item) => String(item || '').trim()).filter(Boolean))];
}

function cleanError(error: unknown): string {
  return String(error).replace(/^Error invoking remote method '[^']+': Error: /, '').replace(/^Error: /, '');
}
