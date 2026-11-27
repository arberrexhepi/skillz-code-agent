import { PathChip, PathText } from '../PathText';
import { useEffect, useId, useMemo, useState } from 'react';
import { useAgentWorkspace } from '../../agent/agentWorkspace';
import type { RuntimeSelection } from '../../agent/agentWorkspace';
import type { CodexSubscriptionStatus, GeminiSubscriptionStatus, RuntimeOptionsPayload } from '../../../../shared/agentTypes';
import { RuntimeReasoningSettings } from './RuntimeReasoningSettings';

const CODEX_SUBSCRIPTION_PROVIDER = 'codex-subscription';
const GEMINI_SUBSCRIPTION_PROVIDER = 'gemini-subscription';

export function RuntimeDrawer({ onClose }: { onClose: () => void }): React.JSX.Element {
  const agent = useAgentWorkspace();
  const [selection, setSelection] = useState(agent.runtime);
  const [ready, setReady] = useState(false);
  const [limit, setLimit] = useState(agent.backoff?.token_limit_k || 0);
  const running = agent.state.status === 'running';
  const busy = Boolean(agent.state.pendingAction) || agent.state.status === 'starting';
  const value = running ? selection : agent.runtime;
  const changed = selection.provider !== agent.runtime.provider || selection.model !== agent.runtime.model;
  useEffect(() => { setSelection(agent.runtime); }, [agent.runtime]);
  const selectRuntime = (next: RuntimeSelection): void => {
    setSelection(next);
    if (!running) agent.setRuntime(next);
  };
  return <section className="runtime-drawer">
    <header><div><span>RUNTIME</span><strong>Execution environment</strong></div><button type="button" className="icon-button" aria-label="Close runtime settings" onClick={onClose}>×</button></header>
    <RuntimeSelectionFields value={value} onChange={selectRuntime} options={agent.state.runtimeOptions} disabled={busy} status={agent.state.status} requireRestart={running} onReadyChange={setReady} showBackend={false} reasoningScope={{ artifactId: agent.artifactId }} />
    {running ? <><p className="runtime-provider-note" role="status">Active: {agent.runtime.provider} · {agent.runtime.model}{changed ? '. Apply your selection to change the running agent.' : ''}</p><button type="button" className="primary-button compact" disabled={!ready || busy || !changed} onClick={() => void agent.switchRuntime(selection.provider, selection.model)}>Apply to running agent</button></> : <p className="runtime-provider-note">Start agent or send a message to use this selection.</p>}
    <label>Backend<select value={agent.runtime.backendScript} disabled={running || busy} onChange={(event) => agent.setRuntime({ backendScript: event.target.value })}><option value="main.py">Stable runtime</option><option value="main_v2.py">Beta TreeLoop</option><option value="live_test_loop.py">Live TreeLoop</option></select></label>
    <div className="runtime-backoff"><label>Token backoff (K)<input type="number" min="0" value={limit} onChange={(event) => setLimit(Math.max(0, Number(event.target.value)))} /></label><button type="button" onClick={() => void agent.setBackoff(limit > 0, limit)}>{limit > 0 ? 'Enable' : 'Disable'}</button></div>
    {agent.backoff && <small>{agent.backoff.window_tokens_used.toLocaleString()} tokens used in current window</small>}
    <div className="runtime-actions">{running ? <button type="button" onClick={() => void agent.stop().then(() => { if (changed) agent.setRuntime(selection); })}>Stop agent</button> : <button type="button" className="primary-button" disabled={!ready || busy} onClick={() => void agent.start()}>Start agent</button>}</div>
  </section>;
}

/** Shared provider/model/auth controls; selecting a runtime never starts or reconfigures an agent. */
export function RuntimeSelectionFields({ value, onChange, options, disabled = false, status = 'stopped', requireRestart = false, onReadyChange, showBackend = true, reasoningScope }: {
  value: RuntimeSelection;
  onChange: (value: RuntimeSelection) => void;
  options?: RuntimeOptionsPayload;
  disabled?: boolean;
  status?: string;
  requireRestart?: boolean;
  onReadyChange: (ready: boolean) => void;
  showBackend?: boolean;
  reasoningScope?: { artifactId?: string };
}): React.JSX.Element {
  const { provider, model } = value;
  const [codexStatus, setCodexStatus] = useState<CodexSubscriptionStatus>();
  const [codexStatusBusy, setCodexStatusBusy] = useState(false);
  const [cliPath, setCliPath] = useState('');
  const [cliError, setCliError] = useState('');
  const [cliNotice, setCliNotice] = useState('');
  const [geminiStatus, setGeminiStatus] = useState<GeminiSubscriptionStatus>();
  const [geminiStatusBusy, setGeminiStatusBusy] = useState(false);
  const [geminiCliPath, setGeminiCliPath] = useState('');
  const [geminiCliError, setGeminiCliError] = useState('');
  const [geminiCliNotice, setGeminiCliNotice] = useState('');
  useEffect(() => { setCliPath(codexStatus?.configured_cli_path || ''); }, [codexStatus?.configured_cli_path]);
  useEffect(() => { setGeminiCliPath(geminiStatus?.configured_cli_path || ''); }, [geminiStatus?.configured_cli_path]);
  const providers = options?.providers?.filter((item) => !item.hidden) || [];
  const providerOption = useMemo(() => providers.find((item) => item.key === provider), [provider, providers]);
  const efforts = (provider === CODEX_SUBSCRIPTION_PROVIDER ? codexStatus?.reasoning_efforts?.[model] : undefined)
    ?? providerOption?.reasoning_efforts?.[model];
  const modelOptions = useMemo(() => uniqueModels([
    ...(provider === CODEX_SUBSCRIPTION_PROVIDER ? codexStatus?.models || [] : []),
    ...(provider === GEMINI_SUBSCRIPTION_PROVIDER ? geminiStatus?.models || [] : []),
    providerOption?.selection_model, providerOption?.active_model, providerOption?.default_model,
    ...(providerOption?.suggested_models || []), model, ...(!providers.length ? [defaultModels[provider]] : []),
  ]), [codexStatus?.models, geminiStatus?.models, model, provider, providerOption, providers.length]);
  const ready = Boolean(
    provider && model && !disabled
    && (provider !== CODEX_SUBSCRIPTION_PROVIDER || (codexStatus?.authenticated && !codexStatusBusy && !(requireRestart && codexStatus?.restart_required)))
    && (provider !== GEMINI_SUBSCRIPTION_PROVIDER || (geminiStatus?.authenticated && !geminiStatusBusy && !(requireRestart && geminiStatus?.restart_required))),
  );
  useEffect(() => { onReadyChange(ready); }, [ready, provider, model, onReadyChange]);
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
  const refreshGeminiStatus = async (): Promise<void> => {
    setGeminiStatusBusy(true);
    try {
      setGeminiStatus(await window.workbench.agent.geminiSubscriptionStatus());
    } catch (cause) {
      setGeminiStatus((current) => ({ ...current, available: false, authenticated: false, error: cleanError(cause) }));
    } finally {
      setGeminiStatusBusy(false);
    }
  };
  const signInToGemini = async (): Promise<void> => {
    setGeminiStatusBusy(true);
    try {
      setGeminiStatus(await window.workbench.agent.geminiSubscriptionLogin());
    } catch (cause) {
      setGeminiStatus((current) => ({ ...current, available: current?.available ?? true, authenticated: false, error: cleanError(cause) }));
    } finally {
      setGeminiStatusBusy(false);
    }
  };
  const signOutOfGemini = async (): Promise<void> => {
    setGeminiStatusBusy(true);
    setGeminiCliError('');
    setGeminiCliNotice('');
    try {
      setGeminiStatus(await window.workbench.agent.geminiSubscriptionLogout());
      setGeminiCliNotice('Signed out of Gemini CLI. Click Sign in with Google to authenticate another account.');
    } catch (cause) {
      setGeminiStatus((current) => ({ ...current, available: current?.available ?? true, authenticated: current?.authenticated ?? false, error: cleanError(cause) }));
    } finally {
      setGeminiStatusBusy(false);
    }
  };
  useEffect(() => {
    if (provider === CODEX_SUBSCRIPTION_PROVIDER) void refreshCodexStatus();
    if (provider === GEMINI_SUBSCRIPTION_PROVIDER) void refreshGeminiStatus();
  }, [provider, status]);
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
  const chooseGeminiCli = async (): Promise<void> => {
    setGeminiStatusBusy(true);
    setGeminiCliError('');
    setGeminiCliNotice('');
    try {
      const selected = await window.workbench.agent.chooseGeminiCli();
      if (selected) setGeminiCliPath(selected);
    } catch (cause) {
      setGeminiCliError(cleanError(cause));
    } finally {
      setGeminiStatusBusy(false);
    }
  };
  const saveGeminiCli = async (candidate: string | null): Promise<void> => {
    setGeminiStatusBusy(true);
    setGeminiCliError('');
    setGeminiCliNotice('');
    try {
      const nextStatus = await window.workbench.agent.setGeminiCliPath(candidate);
      setGeminiStatus(nextStatus);
      setGeminiCliPath(nextStatus.configured_cli_path || '');
      setGeminiCliNotice(candidate ? 'Gemini CLI path saved on this computer.' : 'Automatic discovery restored.');
    } catch (cause) {
      setGeminiCliError(cleanError(cause));
    } finally {
      setGeminiStatusBusy(false);
    }
  };
  const selectProvider = (next: string): void => {
    const option = providers.find((item) => item.key === next);
    const advertised = uniqueModels([option?.selection_model, option?.active_model, option?.default_model, ...(option?.suggested_models || [])]);
    onChange({ ...value, provider: next, model: advertised[0] || defaultModels[next] || '' });
  };
  const providerKeys = uniqueModels([...(providers.length ? providers.map((item) => item.key) : Object.keys(defaultModels)), provider]);
  return <div className="runtime-selection-fields">
    <label>Provider<select value={provider} disabled={disabled} onChange={(event) => selectProvider(event.target.value)}>{providerKeys.map((key) => <option value={key} key={key}>{providers.find((item) => item.key === key)?.label || key}</option>)}</select></label>
    <label>Model<select value={model} disabled={!modelOptions.length || disabled} onChange={(event) => onChange({ ...value, model: event.target.value })}>{modelOptions.map((item) => <option value={item} key={item}>{item}</option>)}</select></label>
    {reasoningScope && provider && model && <RuntimeReasoningSettings key={`${provider}:${model}`} provider={provider} model={model}
      artifactId={reasoningScope.artifactId} efforts={efforts} />}
    {provider === CODEX_SUBSCRIPTION_PROVIDER && <CodexSubscriptionCard
      status={requireRestart ? codexStatus : codexStatus && { ...codexStatus, restart_required: false }} busy={codexStatusBusy || disabled} onRefresh={refreshCodexStatus} onSignIn={signInToCodex}
      cliPath={cliPath} cliError={cliError} cliNotice={cliNotice}
      onPathChange={(next) => { setCliPath(next); setCliError(''); setCliNotice(''); }}
      onChoose={chooseCli} onSave={() => saveCli(cliPath.trim())} onReset={() => saveCli(null)}
    />}
    {provider === GEMINI_SUBSCRIPTION_PROVIDER && <GeminiSubscriptionCard
      status={requireRestart ? geminiStatus : geminiStatus && { ...geminiStatus, restart_required: false }} busy={geminiStatusBusy || disabled} onRefresh={refreshGeminiStatus} onSignIn={signInToGemini} onSignOut={signOutOfGemini}
      cliPath={geminiCliPath} cliError={geminiCliError} cliNotice={geminiCliNotice}
      onPathChange={(next) => { setGeminiCliPath(next); setGeminiCliError(''); setGeminiCliNotice(''); }}
      onChoose={chooseGeminiCli} onSave={() => saveGeminiCli(geminiCliPath.trim())} onReset={() => saveGeminiCli(null)}
    />}
    {providerOption?.notes && provider !== CODEX_SUBSCRIPTION_PROVIDER && provider !== GEMINI_SUBSCRIPTION_PROVIDER && <p className="runtime-provider-note"><PathText>{String(providerOption.notes)}</PathText></p>}
    {showBackend && <label>Backend<select value={value.backendScript} disabled={disabled} onChange={(event) => onChange({ ...value, backendScript: event.target.value })}><option value="main.py">Stable runtime</option><option value="main_v2.py">Beta TreeLoop</option><option value="live_test_loop.py">Live TreeLoop</option></select></label>}
  </div>;
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
  const fieldId = useId();
  const identity = [status?.plan_type, status?.email].filter(Boolean).join(' · ');
  const state = busy ? 'Checking local Codex…' : status?.authenticated
    ? `Connected${identity ? ` · ${identity}` : ''}`
    : status?.available === false ? 'Codex CLI unavailable' : 'ChatGPT sign-in required';
  return <div className={`runtime-subscription ${status?.authenticated ? 'connected' : ''}`}>
    <div><span>LOCAL SESSION</span><strong>{state}</strong></div>
    <p>Uses your local Codex ChatGPT subscription allowance. OpenAI API-key invocation remains a separate provider.</p>
    {status?.error && <small className="runtime-cli-message" role="alert"><PathText>{status.error}</PathText></small>}
    {status?.cli_version && <small>{status.cli_version}</small>}
    {status?.cli_path && <small className="runtime-cli-message">Using {status.cli_path_source === 'settings' ? 'saved path' : status.cli_path_source === 'environment' ? 'environment override' : 'detected path'}: <PathChip path={status.cli_path} /></small>}
    <fieldset className="runtime-cli-location" disabled={busy}>
      <legend>Locate Codex CLI</legend>
      <p>Choose an executable if automatic discovery fails. Saved only on this computer.</p>
      <label htmlFor={`${fieldId}-path`}>Executable path</label>
      <input id={`${fieldId}-path`} value={cliPath} placeholder="Automatic discovery" spellCheck={false} onChange={(event) => onPathChange(event.target.value)} aria-describedby={`${fieldId}-help`} />
      <div className="runtime-cli-buttons">
        <button type="button" onClick={() => void onChoose()}>Browse…</button>
        <button type="button" disabled={!cliPath.trim()} onClick={() => void onSave()}>Save and check</button>
        {status?.configured_cli_path && <button type="button" onClick={() => void onReset()}>Use automatic discovery</button>}
      </div>
      <details id={`${fieldId}-help`}>
        <summary>How to find the executable</summary>
        <p>Windows: choose <code>codex.exe</code>. Check <code>%LOCALAPPDATA%\OpenAI\Codex\bin</code> and its runtime folders, or run <code>(Get-Command codex).Source</code> in PowerShell. For an npm installation, choose the native <code>codex.exe</code> inside the installed package, rather than its .cmd or .ps1 launcher.</p>
        <p>macOS / Linux: run <code>command -v codex</code> in Terminal and paste the path. Choose the <code>codex</code> executable, without command arguments.</p>
      </details>
    </fieldset>
    {cliError && <small className="runtime-cli-message runtime-cli-error" role="alert"><PathText>{cliError}</PathText></small>}
    {cliNotice && <small className="runtime-cli-message" role="status"><PathText>{cliNotice}</PathText></small>}
    {status?.restart_required && <p role="status">Stop and start the agent to use this CLI path for model turns. Status and sign-in already use the new setting.</p>}
    <footer>
      <button type="button" disabled={busy} onClick={() => void onRefresh()}>Refresh</button>
      {!status?.authenticated && <button type="button" className="primary-button" disabled={busy || status?.available === false} onClick={() => void onSignIn()}>Sign in with ChatGPT</button>}
    </footer>
  </div>;
}

export function GeminiSubscriptionCard({
  status,
  busy,
  onRefresh,
  onSignIn,
  onSignOut,
  cliPath,
  cliError,
  cliNotice,
  onPathChange,
  onChoose,
  onSave,
  onReset,
}: {
  status?: GeminiSubscriptionStatus;
  busy: boolean;
  onRefresh: () => Promise<void>;
  onSignIn: () => Promise<void>;
  onSignOut: () => Promise<void>;
  cliPath: string;
  cliError: string;
  cliNotice: string;
  onPathChange: (value: string) => void;
  onChoose: () => Promise<void>;
  onSave: () => Promise<void>;
  onReset: () => Promise<void>;
}): React.JSX.Element {
  const fieldId = useId();
  const identity = [status?.plan_type, status?.email].filter(Boolean).join(' · ');
  const state = busy ? 'Checking local Gemini CLI…' : status?.authenticated
    ? `Connected${identity ? ` · ${identity}` : ''}`
    : status?.available === false ? 'Gemini CLI unavailable' : 'Google sign-in required';
  return <div className={`runtime-subscription ${status?.authenticated ? 'connected' : ''}`}>
    <div><span>LOCAL SESSION</span><strong>{state}</strong></div>
    <p>Uses your local Gemini CLI Google OAuth allowance (free account, Google AI Pro, or Google AI Ultra). Gemini API-key invocation remains a separate provider.</p>
    {status?.status_text && !status?.authenticated && !status?.error && <small className="runtime-cli-message"><PathText>{status.status_text}</PathText></small>}
    {status?.error && <small className="runtime-cli-message" role="alert"><PathText>{status.error}</PathText></small>}
    {status?.cli_version && <small>{status.cli_version}</small>}
    {status?.cli_path && <small className="runtime-cli-message">Using {status.cli_path_source === 'settings' ? 'saved path' : status.cli_path_source === 'environment' ? 'environment override' : 'detected path'}: <PathChip path={status.cli_path} /></small>}
    <fieldset className="runtime-cli-location" disabled={busy}>
      <legend>Locate Gemini CLI</legend>
      <p>Choose an executable if automatic discovery fails. Saved only on this computer.</p>
      <label htmlFor={`${fieldId}-path`}>Executable path</label>
      <input id={`${fieldId}-path`} value={cliPath} placeholder="Automatic discovery" spellCheck={false} onChange={(event) => onPathChange(event.target.value)} aria-describedby={`${fieldId}-help`} />
      <div className="runtime-cli-buttons">
        <button type="button" onClick={() => void onChoose()}>Browse…</button>
        <button type="button" disabled={!cliPath.trim()} onClick={() => void onSave()}>Save and check</button>
        {status?.configured_cli_path && <button type="button" onClick={() => void onReset()}>Use automatic discovery</button>}
      </div>
      <details id={`${fieldId}-help`}>
        <summary>How to install or find Gemini CLI</summary>
        <p>Install via npm (<code>npm install -g @google/gemini-cli</code>) or Homebrew (<code>brew install gemini-cli</code>).</p>
        <p>macOS / Linux: run <code>command -v gemini</code> in Terminal and paste the path.</p>
        <p>Windows: run <code>(Get-Command gemini).Source</code> in PowerShell, or select the <code>gemini.cmd</code> / <code>gemini.js</code> entry under <code>%APPDATA%\npm</code>.</p>
      </details>
    </fieldset>
    {cliError && <small className="runtime-cli-message runtime-cli-error" role="alert"><PathText>{cliError}</PathText></small>}
    {cliNotice && <small className="runtime-cli-message" role="status"><PathText>{cliNotice}</PathText></small>}
    {status?.restart_required && <p role="status">Stop and start the agent to use this CLI path for model turns. Status and sign-in already use the new setting.</p>}
    <footer>
      <button type="button" disabled={busy} onClick={() => void onRefresh()}>Refresh</button>
      {status?.authenticated
        ? <button type="button" disabled={busy} onClick={() => void onSignOut()}>Sign out</button>
        : <button type="button" className="primary-button" disabled={busy || status?.available === false} onClick={() => void onSignIn()}>Sign in with Google</button>}
    </footer>
  </div>;
}

const defaultModels: Record<string, string> = { gemini: 'gemini-3-flash-preview', 'gemini-subscription': 'gemini-4-neon', openai: 'gpt-5.4', 'codex-subscription': 'gpt-5.6-terra', anthropic: 'claude-sonnet-4-6', meta: 'muse-spark-1.2', local: 'gemma4', 'ollama-local': 'qwen3-coder' };

function uniqueModels(models: Array<string | undefined>): string[] {
  return [...new Set(models.map((item) => String(item || '').trim()).filter(Boolean))];
}

function cleanError(error: unknown): string {
  return String(error).replace(/^Error invoking remote method '[^']+': Error: /, '').replace(/^Error: /, '');
}
