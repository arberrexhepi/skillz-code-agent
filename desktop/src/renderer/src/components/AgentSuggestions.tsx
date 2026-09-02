import { useRef, useState } from 'react';
import type { IssueProposal } from '../../../shared/agentTypes';

export function AgentSuggestions({ proposals, error, busy, onDecide }: {
  proposals: IssueProposal[]; error?: string; busy: boolean;
  onDecide: (id: string, decision: 'accept' | 'ignore') => Promise<void>;
}): React.JSX.Element | null {
  if (!proposals.length && !error) return null;
  return <section className="agent-suggestions" aria-label="Agent suggestions">
    <header>AGENT SUGGESTIONS <span>{proposals.length}</span></header>
    <p>Outside the current goal. Already deferred—not waiting on your decision to continue.</p>
    {error && <p role="alert">{error}</p>}
    {proposals.map((proposal) => <Suggestion key={proposal.proposal_id} proposal={proposal} busy={busy} onDecide={onDecide} />)}
  </section>;
}

function Suggestion({ proposal, busy, onDecide }: { proposal: IssueProposal; busy: boolean; onDecide: (id: string, decision: 'accept' | 'ignore') => Promise<void> }): React.JSX.Element {
  const [pending, setPending] = useState('');
  const [error, setError] = useState('');
  const inFlight = useRef(false);
  const decide = async (decision: 'accept' | 'ignore'): Promise<void> => {
    if (inFlight.current) return;
    inFlight.current = true;
    setPending(decision);
    setError('');
    try { await onDecide(proposal.proposal_id, decision); }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { inFlight.current = false; setPending(''); }
  };
  return <article className="agent-suggestion">
    <details><summary>{proposal.summary}<small>Agent-authored · Deferred</small></summary>
      <h4>Why separate</h4><p>{proposal.reason}</p><h4>Evidence</h4><p>{proposal.evidence}</p>
      {!!proposal.paths.length && <p className="suggestion-paths">{proposal.paths.join('\n')}</p>}
      {proposal.parent_issue_id && <small>Found while working on {proposal.parent_issue_id}</small>}
    </details>
    <div className="suggestion-actions"><button type="button" disabled={!!pending} onClick={() => void decide('accept')}>Accept</button><button type="button" disabled={!!pending} onClick={() => void decide('ignore')}>Ignore</button></div>
    {pending && <p role="status">{busy ? 'Decision queued after the current agent action.' : 'Saving decision…'}</p>}
    {error && <p role="alert">{error}</p>}
  </article>;
}
