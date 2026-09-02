import { PathText } from './PathText';
interface IssueCreateFormProps {
  summary: string;
  creating: boolean;
  executionBusy: boolean;
  error: string;
  onChange: (summary: string) => void;
  onCreate: () => void;
}

export function IssueCreateForm({ summary, creating, executionBusy, error, onChange, onCreate }: IssueCreateFormProps): React.JSX.Element {
  return <div>
    <form className="issue-create" aria-label="Create issue" onSubmit={(event) => {
      event.preventDefault();
      if (summary.trim() && !creating) onCreate();
    }}>
      <input aria-label="Issue details" value={summary} disabled={creating} onChange={(event) => onChange(event.target.value)} placeholder="Add an issue to the backlog…" />
      <button type="submit" className="primary-button" aria-label="Add issue" title="Add issue without switching the active issue" disabled={!summary.trim() || creating}>＋</button>
    </form>
    {creating && <div className="issue-create-feedback" role="status">{executionBusy
      ? 'Queued for creation. Waiting for the current agent action to finish…'
      : 'Creating issue…'}</div>}
    {error && <div className="issue-create-feedback error-text" role="alert"><PathText>{error}</PathText></div>}
  </div>;
}
