import { useCallback, useEffect, useRef, useState } from 'react';
import type { ArtifactRecord, PrebuiltUpdateFile, PrebuiltUpdateRequest, PrebuiltUpdateResult, PrebuiltUpdateReview, PrebuiltUpdateStatus } from '../../../shared/artifacts';

const actionLabel: Record<PrebuiltUpdateFile['action'], string> = { replace: 'Update', merge: 'Merge local edits', keep: 'Keep local edits', current: 'Already installed', conflict: 'Choose a version' };
export function ArtifactPrebuiltUpdate({ record, active }: { record: ArtifactRecord; active: boolean }): React.JSX.Element | null {
  const [status, setStatus] = useState<PrebuiltUpdateStatus>();
  const [review, setReview] = useState<PrebuiltUpdateReview>();
  const [resolutions, setResolutions] = useState<PrebuiltUpdateRequest['resolutions']>({});
  const [result, setResult] = useState<PrebuiltUpdateResult>();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const checking = useRef(false);
  const check = useCallback(async () => {
    if (!record.prebuiltId || checking.current) return;
    checking.current = true;
    try { setStatus(await window.workbench.artifacts.prebuiltUpdateStatus(record.id)); }
    catch (error) { setStatus({ available: false, changedFiles: 0, error: clean(error) }); }
    finally { checking.current = false; }
  }, [record.id, record.prebuiltId]);
  useEffect(() => {
    if (!active || !record.prebuiltId || busy || review) return;
    void check();
    const timer = window.setInterval(() => { if (document.visibilityState === 'visible') void check(); }, 15000);
    window.addEventListener('focus', check);
    return () => { window.clearInterval(timer); window.removeEventListener('focus', check); };
  }, [active, record.prebuiltId, busy, review, check]);
  useEffect(() => { if (review && active && !dialog.current?.open) dialog.current?.showModal(); }, [review, active]);
  useEffect(() => { if (!active) { dialog.current?.close(); setReview(undefined); } }, [active]);
  if (!record.prebuiltId) return null;
  async function loadReview() {
    setBusy(true); setError(''); setResult(undefined);
    try { setReview(await window.workbench.artifacts.reviewPrebuiltUpdate(record.id)); setResolutions({}); }
    catch (error) { setError(clean(error)); }
    finally { setBusy(false); }
  }
  async function apply() {
    if (!review) return;
    setBusy(true); setError('');
    try {
      const applied = await window.workbench.artifacts.applyPrebuiltUpdate(record.id, { token: review.token, resolutions });
      setResult(applied); dialog.current?.close(); setReview(undefined);
      await check();
    } catch (error) { setError(clean(error)); }
    finally { setBusy(false); }
  }
  function close() { dialog.current?.close(); setReview(undefined); setError(''); }
  const conflicts = review?.files.filter(file => file.action === 'conflict') || [];
  const unresolved = conflicts.filter(file => !resolutions[file.path]).length;
  return <>
    {status?.available && <div className="artifact-setup-notice artifact-update-notice" role="status"><span>A newer version of this prebuilt is available. <small>{status.changedFiles} source {status.changedFiles === 1 ? 'file has' : 'files have'} changed.</small></span><button disabled={busy} onClick={() => void loadReview()}>{busy ? 'Checking…' : 'Review update'}</button></div>}
    {status?.error && <div className="artifact-setup-notice" role="status"><span>Couldn’t check for prebuilt updates. <small>{status.error}</small></span><button disabled={busy} onClick={() => void check()}>Check again</button></div>}
    {result && <div className="artifact-update-result" role="status"><span>Prebuilt updated. Start preview to load the installed source.</span><details><summary>Retained backup</summary><code>{result.backup}</code></details><button aria-label="Dismiss update result" onClick={() => setResult(undefined)}>×</button></div>}
    {error && !review && <div className="artifacts-error" role="alert">{error}</div>}
    <dialog ref={dialog} className="artifact-update-dialog" aria-labelledby={`prebuilt-update-${record.id}`} onCancel={event => { event.preventDefault(); if (!busy) close(); }}>
      {review && <>
        <header><div><span className="eyebrow">PREBUILT UPDATE</span><h2 id={`prebuilt-update-${record.id}`}>{record.title}</h2></div><button disabled={busy} aria-label="Close update review" onClick={close}>×</button></header>
        <div className="artifact-update-content">
          <p>Update your installed copy from the maintained prebuilt source. Local edits merge where possible. Choose which version to keep for each conflict.</p>
          <p>Applying stops this artifact’s preview and editing agent, retains a backup, and leaves the changes in Repository for review. Saved access, runtime settings, API Blueprints, and app data are preserved.</p>
          {!review.files.length && <p>This prebuilt is already up to date.</p>}
          {conflicts.length > 0 && <p className="artifact-update-conflicts" role="status">{unresolved ? `${unresolved} ${unresolved === 1 ? 'conflict needs' : 'conflicts need'} a choice before applying.` : 'All conflicts have a choice.'}</p>}
          {review.files.map(file => <details className="artifact-update-file" key={file.path}>
            <summary><code>{file.path}</code><span>{file.kind} · {actionLabel[file.action]}</span></summary>
            {file.action === 'conflict' && <label className="artifact-update-resolution">Version to keep<select aria-label={`Version to keep for ${file.path}`} value={resolutions[file.path] || ''} disabled={busy} onChange={event => setResolutions(current => ({ ...current, [file.path]: event.target.value as 'installed' | 'incoming' }))}><option value="" disabled>Choose a version</option><option value="installed">Keep installed version</option><option value="incoming">Use incoming version</option></select></label>}
            {file.binary ? <p>Binary file. Contents cannot be displayed here.</p> : <div className="artifact-update-versions"><section><h3>Installed</h3><pre>{file.installed ?? '(File absent)'}</pre></section><section><h3>Incoming</h3><pre>{file.incoming ?? '(File removed)'}</pre></section>{file.action === 'merge' && <section><h3>Merged result</h3><pre>{file.result}</pre></section>}</div>}
            {file.truncated && <p>Content preview is limited to the first 12,000 characters.</p>}
          </details>)}
          {error && <div className="artifacts-error" role="alert">{error}</div>}
        </div>
        <footer><button disabled={busy} onClick={() => void loadReview()}>Refresh review</button><button disabled={busy} onClick={close}>Later</button><button className="primary-button" disabled={busy || unresolved > 0 || !review.files.length} onClick={() => void apply()}>{busy ? 'Updating…' : 'Apply update'}</button></footer>
      </>}
    </dialog>
  </>;
}
function clean(error: unknown): string { return String(error).replace(/^Error: (?:Error invoking remote method '[^']+': Error: )?/, ''); }
