import { useEffect, useRef, useState } from 'react';
import { sampleRun } from './sample';
import { mergeEvents } from './trace';
import type { Run, RuntimeStatus } from './types';
export async function runtimeRequest<T>(route: string, body?: unknown): Promise<T> {
  const response = await fetch(`/runtime/v1/${route}`, { method: body === undefined ? 'GET' : 'POST', headers: body === undefined ? {} : { 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(35000) });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Runtime returned HTTP ${response.status}`);
  return data;
}
export function useRuntime() {
  const [status, setStatus] = useState<RuntimeStatus>({ available: false, runs: [] });
  const [run, setRun] = useState<Run>(sampleRun);
  const [selected, setSelected] = useState('sample');
  const [repositoryId, selectRepository] = useState('repo');
  const [error, setError] = useState('');
  const [pending, setPending] = useState(false);
  const [pollRevision, setPollRevision] = useState(0);
  const restored = useRef(false);
  const actionPending = useRef(false);
  useEffect(() => {
    let cancelled = false; let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const next = await runtimeRequest<RuntimeStatus>('runtime');
        if (!cancelled) {
          setStatus(next);
          if (!restored.current) { restored.current = true; if (next.activeRunId) { setSelected(next.activeRunId); selectRepository(next.runs.find(item => item.id === next.activeRunId)?.repository?.id || 'repo'); } }
        }
      } catch (error) { if (!cancelled) { setError(String(error)); setStatus(current => ({ ...current, available: false })); } }
      if (!cancelled) timer = setTimeout(() => void poll(), 1500);
    }
    void poll(); return () => { cancelled = true; clearTimeout(timer); };
  }, []);
  useEffect(() => {
    if (selected === 'sample') { setRun(sampleRun); return; }
    let cancelled = false, after = 0; let timer: ReturnType<typeof setTimeout>;
    setRun(current => current.id === selected ? current : { id: selected, prompt: 'Loading recording…', startedAt: new Date().toISOString(), status: 'completed', selection: { provider: '', model: '' }, events: [] });
    async function poll() {
      try {
        const next = await runtimeRequest<Run>(`runs/${selected}?after=${after}`);
        if (cancelled) return;
        after = next.events.at(-1)?.seq || after;
        setRun(current => ({ ...next, events: mergeEvents(current.id === next.id ? current.events : [], next.events) }));
        if (next.status === 'running' || next.status === 'awaiting-input') timer = setTimeout(() => void poll(), 450);
      } catch (error) { if (!cancelled) { setError(String(error)); timer = setTimeout(() => void poll(), 1800); } }
    }
    void poll(); return () => { cancelled = true; clearTimeout(timer); };
  }, [selected, pollRevision]);
  async function act(route: string, body: unknown) {
    if (actionPending.current) return false;
    actionPending.current = true;
    setPending(true); setError('');
    try {
      const result = await runtimeRequest<{ id?: string }>(route, body);
      if (result.id) setSelected(result.id);
      setStatus(await runtimeRequest<RuntimeStatus>('runtime'));
      if (route !== 'configure') setPollRevision(current => current + 1);
      return true;
    } catch (error) { setError(String(error)); return false; }
    finally { actionPending.current = false; setPending(false); }
  }
  return { status, run, error, pending, repositoryId, selectRepository, select: setSelected, configure: () => act('configure', {}), start: (text: string) => act('runs', { text, repositoryId }), stop: () => act('stop', {}), continueRun: (input: { text?: string; action?: string }) => act(`runs/${run.id}/continue`, input) };
}
