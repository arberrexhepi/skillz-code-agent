import { useEffect, useState } from 'react';
import type { ReasoningEffort, ReasoningPhase, RepoReasoningPreferences } from '../../../../shared/reasoningPreferences';

const phases: [ReasoningPhase, string][] = [
  ['all', 'All'], ['discovery', 'Discovery'], ['planning', 'Planning'], ['goals', 'Goal workers'], ['verification', 'Verification worker'],
];
const effortLabels: Record<ReasoningEffort, string> = {
  auto: 'Model default', none: 'None', minimal: 'Minimal', low: 'Low', medium: 'Medium', high: 'High', xhigh: 'Extra high', max: 'Max', ultra: 'Ultra',
};

export function RuntimeReasoningSettings({ provider, model, artifactId, efforts }: {
  provider: string;
  model: string;
  artifactId?: string;
  efforts?: ReasoningEffort[];
}): React.JSX.Element {
  const [saved, setSaved] = useState<RepoReasoningPreferences>();
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let current = true;
    const load = async (): Promise<void> => {
      try {
        const value = await window.workbench.agent.reasoningPreferences({ provider, model }, artifactId);
        if (current) setSaved(value);
      } catch (cause) {
        if (current) setError(cleanError(cause));
      }
    };
    void load();
    return () => { current = false; };
  }, [provider, model, artifactId]);

  async function save(phase: ReasoningPhase, effort: ReasoningEffort | null): Promise<void> {
    if (!saved) return;
    setSaving(true);
    setError('');
    try {
      setSaved(await window.workbench.agent.saveReasoningPreference({ provider, model }, phase, effort, saved.root, artifactId));
    } catch (cause) {
      setError(cleanError(cause));
    } finally {
      setSaving(false);
    }
  }

  const choices = efforts ?? ['auto'];
  return <fieldset className="runtime-reasoning" disabled={!saved || saving}>
    <legend>Reasoning effort</legend>
    {phases.map(([phase, label]) => {
      const value = saved?.preferences[phase];
      return <label key={phase}>{label}<select value={value ?? ''} onChange={event => void save(phase, (event.target.value || null) as ReasoningEffort | null)}>
        <option value="">{phase === 'all' ? 'Runtime default' : 'Use All'}</option>
        {choices.map(effort => <option key={effort} value={effort}>{effortLabels[effort]}</option>)}
        {value && !choices.includes(value) && <option value={value} disabled>{effortLabels[value]} (unavailable)</option>}
      </select></label>;
    })}
    {error && <small role="alert">{error}</small>}
  </fieldset>;
}

function cleanError(error: unknown): string {
  return String(error).replace(/^Error invoking remote method '[^']+': Error: /, '');
}
