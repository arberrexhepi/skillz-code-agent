import { useState } from 'react';
import type { ArtifactRuntime } from '../../../shared/artifacts';

export function ArtifactPreview({ title, active, runtime }: { title: string; active: boolean; runtime?: ArtifactRuntime }): React.JSX.Element {
  const [revision, setRevision] = useState(0);
  return <div className="artifact-preview" hidden={!active}>
    {runtime?.status === 'running' && runtime.url ? <>
      <div className="artifact-preview-tools">
        <span title={runtime.url}>{runtime.url}</span>
        <button onClick={() => setRevision(value => value + 1)}>Reload preview</button>
      </div>
      <iframe
        key={`${runtime.url}:${revision}`}
        className="artifact-preview-frame"
        title={`${title} preview`}
        src={runtime.url}
        sandbox="allow-scripts allow-same-origin allow-forms"
        referrerPolicy="no-referrer"
      />
    </> : <div className="artifact-empty">
      <span>◇</span>
      <h2>{runtime?.status === 'installing' ? 'Installing artifact dependencies…' : runtime?.status === 'starting' ? 'Starting the artifact…' : 'Preview your artifact.'}</h2>
      <p>Start preview to run the Express/Vite app on an available local port.</p>
      {runtime?.error && <p role="alert">{runtime.error}</p>}
    </div>}
  </div>;
}
