import { useState } from 'react';
import type { ArtifactDockerCleanupPlan } from '../../../shared/artifacts';

export function ArtifactDockerStorage({ onClose }: { onClose: () => void }): React.JSX.Element {
  const [plan, setPlan] = useState<ArtifactDockerCleanupPlan>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  async function review() {
    setBusy(true); setError(''); setMessage(''); setPlan(undefined);
    try {
      const next = await window.workbench.artifacts.dockerCleanupPlan();
      if (!next.obsoleteImages.length && !next.orphanedVolumes.length) setMessage('No unused Skillz Docker resources were found.');
      else setPlan(next);
    } catch (cause) { setError(clean(cause)); }
    finally { setBusy(false); }
  }
  async function remove() {
    if (!plan) return;
    const summary = `${plan.obsoleteImages.length} obsolete runtime image${plan.obsoleteImages.length === 1 ? '' : 's'} and ${plan.orphanedVolumes.length} orphaned dependency volume${plan.orphanedVolumes.length === 1 ? '' : 's'}`;
    if (!window.confirm(`Remove ${summary}?\n\nDocker resources for installed artifacts and resources attached to containers will be preserved.`)) return;
    setBusy(true); setError(''); setMessage('');
    try {
      const result = await window.workbench.artifacts.cleanDocker();
      setPlan(undefined);
      const removed = `Removed ${result.removedImages.length} runtime image${result.removedImages.length === 1 ? '' : 's'} and ${result.removedVolumes.length} dependency volume${result.removedVolumes.length === 1 ? '' : 's'}.`;
      setMessage(result.failures.length ? `${removed} ${result.failures.length} resource${result.failures.length === 1 ? '' : 's'} could not be removed because Docker state changed.` : removed);
    } catch (cause) { setError(clean(cause)); }
    finally { setBusy(false); }
  }
  return <div className="artifact-storage-backdrop" role="presentation"><section className="artifact-storage-dialog" role="dialog" aria-modal="true" aria-labelledby="artifact-storage-title">
    <header><div><span className="eyebrow">ARTIFACT MAINTENANCE</span><h2 id="artifact-storage-title">Docker storage</h2></div><button type="button" aria-label="Close Docker storage" disabled={busy} onClick={onClose}>×</button></header>
    <p>Artifacts share a versioned agent runtime image. Each installed artifact also has a dependency volume for its container packages.</p>
    <p>Cleanup preserves the current runtime, installed artifact volumes, and resources attached to running or stopped containers.</p>
    {!plan && <button type="button" className="primary-button" disabled={busy} onClick={() => void review()}>{busy ? 'Checking Docker…' : 'Review unused resources'}</button>}
    {plan && <div className="artifact-storage-review">
      <div className="artifact-storage-counts"><div><strong>{plan.obsoleteImages.length}</strong><span>obsolete runtime images</span></div><div><strong>{plan.orphanedVolumes.length}</strong><span>orphaned dependency volumes</span></div></div>
      <p>{plan.preservedImages.length} runtime image{plan.preservedImages.length === 1 ? '' : 's'} and {plan.preservedVolumes.length} dependency volume{plan.preservedVolumes.length === 1 ? '' : 's'} will be preserved.</p>
      <details><summary>Resources to remove</summary><code>{[...plan.obsoleteImages, ...plan.orphanedVolumes].join('\n')}</code></details>
      <div className="artifact-form-actions"><button type="button" className="primary-button" disabled={busy} onClick={() => void remove()}>{busy ? 'Cleaning…' : 'Remove unused resources'}</button><button type="button" disabled={busy} onClick={() => setPlan(undefined)}>Cancel</button></div>
    </div>}
    {message && <p className="artifact-storage-message" role="status">{message}</p>}
    {error && <p className="artifacts-error" role="alert">{error}</p>}
  </section></div>;
}

function clean(error: unknown): string { return String(error).replace(/^Error invoking remote method '[^']+': Error: /, ''); }
